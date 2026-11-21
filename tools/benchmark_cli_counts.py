#!/usr/bin/env python3
"""Fresh-process scalar counts: startup/open/query/output are all included."""
from paths import SDK
from benchmark_producer_corpus import williams_order
from input_identity import identify, unchanged
from provenance import harness_source
import argparse
import hashlib
import json
import os
import pathlib
import platform
import random
import statistics
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('file', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--legacy-gol', type=pathlib.Path, default=SDK/'gol-legacy/gol')
    parser.add_argument('--gol-tool', type=pathlib.Path, default=SDK/'gol-current/gol')
    parser.add_argument('--regol',type=pathlib.Path,default=ROOT/'target/release/regol')
    parser.add_argument('--engines',nargs='+',choices=['regol','gol-2.0.0','gol-2.5.3'],default=['regol','gol-2.5.3'])
    parser.add_argument('--spec',type=pathlib.Path,default=ROOT/'benchmarks/uk-corpus-counts.json')
    parser.add_argument('--samples', type=int, default=5)
    args = parser.parse_args()
    if args.samples < 1 or len(set(args.engines)) != len(args.engines):
        parser.error('select distinct engines and at least one sample')
    if args.output.exists():
        parser.error('output already exists')
    args.output.parent.mkdir(exist_ok=True,parents=True)
    spec_bytes = args.spec.read_bytes()
    cases = json.loads(spec_bytes)
    if any(c.get('bounds') or c.get('bbox_lonlat') or c.get('limit') is not None or c.get('operation','iterate')!='iterate' for c in cases):
        parser.error('CLI counts require unlimited whole-file scalar selectors')
    random.Random(20261117).shuffle(cases)
    binaries={'regol':args.regol,'gol-2.0.0':args.legacy_gol,'gol-2.5.3':args.gol_tool}
    engines=[(name,binaries[name].resolve(strict=True)) for name in args.engines]
    if not cases or len({case['name'] for case in cases})!=len(cases):
        parser.error('supply a nonempty list with unique case names')
    path=args.file.resolve(strict=True)
    identity=identify(path)
    available=tuple(sorted(os.sched_getaffinity(0)))
    arms=[(budget,cpus,name,binary) for budget,cpus in [('one',available[:1]),('all',available)]
          for name,binary in engines]
    metadata = {'gol_sha256': identity['sha256'], 'ordering': 'williams', 'case_order':[c['name'] for c in cases], 'started_unix': time.time(),
                'machine': platform.uname()._asdict(),
                'cpu_model': next((line.split(':',1)[1].strip() for line in pathlib.Path('/proc/cpuinfo').read_text().splitlines()
                                   if line.startswith(('model name','Hardware'))),platform.machine()),
                'samples': args.samples, 'consumer': 'scalar_count',
                'timing': 'fresh process including startup, open, query, output; full input pre-read before campaign',
                'spec_sha256': hashlib.sha256(spec_bytes).hexdigest(),
                'source_sha256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
                'binary_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in engines}}
    metadata["harness_source"] = harness_source()
    args.output.with_suffix('.metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    with args.output.open('x') as out:
        for ordinal,case in enumerate(cases):
            results={}
            for sample in range(args.samples+1):
                for arm in williams_order(arms,ordinal+sample):
                    affinity,cpus,stack,binary=arm
                    row=results.setdefault(arm,dict(stack=stack,name=case['name'],query=case['query'],
                        consumer='scalar_count',affinity=affinity,cpus=cpus,samples_wall_ns=[]))
                    if 'error' in row:
                        continue
                    fmt=['-f','count'] if stack=='regol' else ['-f=count']
                    cmd=['taskset','-c',','.join(map(str,cpus)),binary,'query',path,case['query'],*fmt]
                    try:
                        start=time.perf_counter_ns()
                        proc=subprocess.run(cmd,text=True,capture_output=True,timeout=120)
                        elapsed=time.perf_counter_ns()-start
                        if proc.returncode:
                            raise RuntimeError(f'exit={proc.returncode}: {proc.stderr[-1500:]}')
                        count=int(proc.stdout.strip())
                        if sample==0:
                            row.update(first_ns=elapsed,count=count)
                        else:
                            if row['count']!=count:
                                raise ValueError(f'count changed from {row["count"]} to {count}')
                            row['samples_wall_ns'].append(elapsed)
                    except (ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
                        row['error']=str(exc)
            for row in results.values():
                if 'error' in row:
                    row.pop('samples_wall_ns')
                else:
                    row['median_wall_ns']=statistics.median(row['samples_wall_ns'])
                print(json.dumps(row),file=out,flush=True)
                print(row['stack'],row['affinity'],row['name'],row.get('error',row.get('count')),flush=True)
    unchanged(path,identity)


if __name__ == '__main__':
    main()
