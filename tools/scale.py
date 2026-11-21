"""Replay geographically anchored query histories independently on each dataset."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from datasets import catalogue, dataset_path, prepare
from paths import ROOT, SDK, WORKER
from summarize_producer_corpus import records, WORKERS
from summarize_stack_benchmarks import compare


def history(item, global_scan=False):
    if item['profile'] == 'fixture':
        return json.loads((ROOT/'benchmarks/fixture.json').read_bytes())
    cases = []
    def add(name, query, bounds=None):
        row = dict(name=name, query=query, operation='iterate', role='candidate_selector')
        if bounds is not None:
            row['bbox_lonlat'] = [round(v, 6) for v in bounds]
        cases.append(row)
    # A map editor opens a city viewport, inspects amenities, pans east, and
    # zooms into a junction to review paths and crossings. One open per history.
    for city, (x, y) in item['views'].items():
        view = [x-.012, y-.008, x+.012, y+.008]
        pan = [x, y-.008, x+.024, y+.008]
        zoom = [x+.003, y-.003, x+.009, y+.003]
        add(city+'_streets', 'w[highway]', view)
        add(city+'_amenities', 'n[amenity]', view)
        add(city+'_cafes', '*[amenity=cafe]', view)
        add(city+'_pan_streets', 'w[highway]', pan)
        add(city+'_crossings', 'n[highway=crossing]', zoom)
    if global_scan:
        add('world_country_boundaries', 'r[boundary=administrative][admin_level=2]')
    return cases


def validate(raw, checks, cases):
    """Require a verified identity for every timed case, including empty hits."""
    names = [c['name'] for c in cases]
    by_width = {}
    for path in checks:
        alternate = path.stem.endswith('-pooled')
        width = int(path.stem.removesuffix('-pooled').rsplit('-', 1)[1])
        results = by_width.setdefault(width, {})
        for row in records(path):
            engine = 'pooled' if alternate else WORKERS[row['stack']]
            key = engine, row['name']
            if key in results:
                raise ValueError(f'duplicate verification {key}')
            results[key] = row
    operations = {c['name']: c['operation'] for c in cases}
    for results in by_width.values():
        for (engine,name), row in results.items():
            reference = results['python',name]
            if row.get('error') or reference.get('error') or compare(row,reference,operations[name]) not in ('content_match','identities_match'):
                raise ValueError(f'{engine}/{name} failed identity verification')
            if name.endswith('_streets') and reference['count'] == 0:
                raise ValueError(f'{name}: empty coverage anchor')
    runs = iter(records(raw))
    next(runs)
    from statistics import median
    samples = {}
    for run in runs:
        key = run['reader']
        results = by_width[run['cores']]
        if [r['name'] for r in run['rows']] != names:
            raise ValueError('incomplete query history')
        elapsed = 0
        for row in run['rows']:
            correct = results[key,row['name']]
            if row.get('error') or row['count'] != correct['count'] or row['work_items'] != correct.get('work_items',correct.get('items')):
                raise ValueError(f'{key}/{row["name"]}: incorrect timed result')
            elapsed += row['setup_ns'] + row['first_execution_ns']
        samples.setdefault(f"{key}/{run['cores']}", []).append(elapsed/1e6)
    return {k: {'rounds_ms': values,'median_ms': median(values)} for k,values in samples.items()}


STACKS = ('rust-paged','rust-safe','rust-mmap','python','python-current','cpp','java',
          'java-current','clojure','clojure-current','dotnet','rust-ffi','php','php-ffi')


def stack_replays(args,path,spec,cases,directory):
    from summarize_stack_benchmarks import summarize
    chosen=list(dict.fromkeys([*args.stacks,'python-current']))
    summaries={}
    for n in args.cores:
        cpus=sorted(os.sched_getaffinity(0))[:n]
        report=directory/f'stacks-{n}.jsonl'
        subprocess.run(['taskset','-c',','.join(map(str,cpus)),sys.executable,
                        ROOT/'tools/benchmark_stacks.py',path,'--spec',spec,'--preserve-order',
                        '--engines',','.join(chosen),'--samples',str(args.stack_samples),
                        '--target-ms',str(args.stack_target_ms),'--timeout',str(args.timeout),
                        '--affinity','one' if n==1 else 'all','--rust-worker',args.rust.absolute(),
                        '--python-worker',args.python.absolute(),'--output',report],check=True)
        audit=summarize([report],cases,'python-current')
        (directory/f'stacks-{n}.audit.json').write_text(json.dumps(audit,indent=2)+'\n')
        summaries[n]=audit['overview']
    return summaries


def save_json(path,data):
    draft=path.with_suffix('.tmp')
    draft.write_text(json.dumps(data,indent=2)+'\n')
    draft.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', action='append', choices=catalogue(), required=True)
    p.add_argument('--file', action='append', default=[], metavar='DATASET=GOL', help='existing copy of a locked dataset')
    p.add_argument('--planet-lock', type=Path)
    p.add_argument('--planet-url',help='HTTPS mirror of the exact locked planet GOL')
    p.add_argument('--directory', type=Path, default=ROOT/'.reference/datasets')
    p.add_argument('--world-scan', action='store_true', help='also run one unbounded selective relation search on the full planet')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stacks',nargs='+',choices=STACKS,
                   help='also run calibrated binding APIs on the same histories and audit against Python')
    p.add_argument('--stack-samples',type=int,default=5)
    p.add_argument('--stack-target-ms',type=int,default=25)
    p.add_argument('--engines',nargs='+',choices=['regol','pooled','cpp','python','java'],default=['regol','python'])
    p.add_argument('--cores',type=int,nargs='+',default=[1])
    p.add_argument('--pool-workers',type=int,default=32)
    p.add_argument('--rounds',type=int,default=6)
    p.add_argument('--cache',choices=['warm','inherit','evict'],default='inherit')
    p.add_argument('--timeout',type=int,default=1800,help='seconds per timed history or per verified case')
    p.add_argument('--rust',type=Path,default=WORKER)
    p.add_argument('--python',type=Path,default=SDK/'python-current313/bin/python')
    a = p.parse_args()
    if a.stack_samples < 1 or a.stack_target_ms < 0:
        p.error('stack samples must be positive and target time nonnegative')
    if len(set(a.dataset)) != len(a.dataset) or a.world_scan and 'planet' not in a.dataset:
        p.error('select each dataset once; --world-scan requires planet')
    existing = {}
    for label in a.file:
        key,sep,path = label.partition('=')
        if not sep or key not in a.dataset or key in existing:
            p.error('each --file needs a unique selected dataset name')
        existing[key] = Path(path).resolve(strict=True)
    items = catalogue(a.planet_lock)
    dest = a.output.resolve()
    dest.mkdir(parents=True,exist_ok=False)
    index = {}
    run=dict(status='incomplete',started=datetime.now(timezone.utc).isoformat(),datasets=a.dataset,
             engines=a.engines,cores=a.cores,rounds=a.rounds,cache=a.cache,pool_workers=a.pool_workers,
             stacks=a.stacks,stack_samples=a.stack_samples,stack_target_ms=a.stack_target_ms,
             world_scan=a.world_scan,revision=subprocess.check_output(
                ['git','-C',ROOT,'rev-parse','HEAD'],text=True).strip(),completed=[])
    save_json(dest/'run.json',run)
    for key in a.dataset:
        item = items[key]
        path = existing.get(key) or dataset_path(key, item, a.directory.resolve())
        if not path.exists():
            path = prepare(key, item, a.directory.resolve(), planet_url=a.planet_url if key == 'planet' else None)
        lock = dest / (key + '.lock.json')
        save_json(lock, {key:item})
        cases = history(item, a.world_scan and key=='planet')
        spec = dest/(key+'.queries.json')
        spec.write_text(json.dumps(cases,indent=2)+'\n')
        d = dest/key
        subprocess.run([sys.executable, ROOT/'tools/campaign.py','--library',f'{key}={path}',
                        '--input-lock',lock,'--engines',*a.engines,'--cores',*map(str,a.cores),'--rounds',str(a.rounds),
                        '--cache',a.cache,'--pool-workers',str(a.pool_workers),'--timeout',str(a.timeout),
                        '--rust',a.rust.absolute(),'--python',a.python.absolute(),'--spec',spec,'--output',d],check=True)
        checks = [d/f'{key}-{n}.jsonl' for n in a.cores]
        if 'pooled' in a.engines:
            checks.extend(d/f'{key}-{n}-pooled.jsonl' for n in a.cores)
        timings = validate(d/'timings.jsonl.gz', checks, cases)
        measured=json.loads((d/'summary.json').read_bytes())['inputs'][key]
        with closing(records(d/'timings.jsonl.gz')) as raw:
            header = next(raw)
        cycle = header['crossover_cycle_rounds']
        index[key] = dict(dataset=item, gol_sha256=measured['sha256'], cases=len(cases),
                          cache=a.cache, prepare_and_scan=timings,
                          crossover_cycle_rounds=cycle, full_crossover_cycles=a.rounds//cycle)
        if a.stacks:
            index[key]['calibrated_stack_audit']=stack_replays(a,path,spec,cases,d)
        save_json(dest/'index.json',index)
        run['completed'].append(key)
        save_json(dest/'run.json',run)
    run.update(status='complete',finished=datetime.now(timezone.utc).isoformat())
    save_json(dest/'run.json',run)


if __name__=='__main__':
    main()
