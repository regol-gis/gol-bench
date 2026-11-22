"""Run first-pass timings and independently verify the same workloads and reader modes."""
import gzip
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from paths import ROOT, SDK, WORKER

ENGINES = {'regol': 'rust-mmap', 'pooled': 'rust-mmap', 'python': 'python-current',
           'cpp': 'cpp', 'java': 'java-current'}


def write_input_identities(raw, directory):
    with gzip.open(raw, 'rt') as stream:
        identities = json.loads(next(stream))['file_identities']
    paths = {}
    for label, identity in identities.items():
        path = directory / f'{label}-input.json'
        path.write_text(json.dumps(identity) + '\n')
        paths[label] = path
    return paths


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library', action='append', required=True, metavar='LABEL=GOL')
    p.add_argument('--engines', nargs='+', choices=ENGINES, default=['regol','pooled','python'])
    p.add_argument('--cores', type=int, nargs='+', default=[1])
    p.add_argument('--pool-workers', type=int, default=32)
    p.add_argument('--timeout',type=int,default=300,help='seconds per timed history or per verified case')
    p.add_argument('--rounds', type=int, default=6)
    p.add_argument('--cache', choices=['inherit','warm','evict'], default='inherit')
    p.add_argument('--input-lock', type=Path, help='JSON catalogue entries keyed by the selected library labels')
    p.add_argument('--spec', type=Path, default=ROOT/'benchmarks/uk-corpus-queries.json')
    p.add_argument('--reference-summary', type=Path, help='also report reference-only cohorts from the same timings')
    p.add_argument('--same-cases-as', type=Path)
    p.add_argument('--output', type=Path, required=True, help='new result directory')
    p.add_argument('--rust', type=Path, default=WORKER)
    p.add_argument('--python', type=Path, default=SDK/'python-current313/bin/python')
    a = p.parse_args()
    a.rust=a.rust.absolute()
    a.python=a.python.absolute()
    libraries = {}
    for value in a.library:
        label, sep, path = value.partition('=')
        if not sep or not label.isascii() or not label.replace('-','').replace('_','').isalnum() or label in libraries:
            p.error('each library requires a unique alphanumeric label (hyphens and underscores allowed)')
        libraries[label] = Path(path).resolve(strict=True)
    if min(a.timeout, a.rounds, a.pool_workers, *a.cores) < 1 or max(a.cores) > len(os.sched_getaffinity(0)):
        p.error('positive rounds, pool size and available CPU counts required')
    if len(set(a.engines)) != len(a.engines) or len(set(a.cores)) != len(a.cores):
        p.error('duplicate reader or CPU count')
    d = a.output.resolve()
    d.mkdir(parents=True, exist_ok=False)
    common = [sys.executable, ROOT/'tools/benchmark_producer_corpus.py']
    raw = d/'timings.jsonl.gz'
    subprocess.run([*common, '--engines', *a.engines, '--cores', *map(str,a.cores),
                    '--rust-workers','0','--pool-workers',str(a.pool_workers),'--rounds',str(a.rounds),
                    '--timeout',str(a.timeout),'--file-cache',a.cache,'--regol',a.rust,'--python',a.python,
                    '--spec',a.spec,'--output',raw,
                    *(['--input-lock',a.input_lock] if a.input_lock else []),
                    *(x for k,v in libraries.items() for x in ('--library',f'{k}={v}'))],check=True)
    checks = []
    # Python supplies the typed reference, even in a Rust-only timing run.
    stacks = list(dict.fromkeys([ENGINES[e] for e in a.engines if e != 'pooled']+['python-current']))
    identities = write_input_identities(raw, d)
    for label,file in libraries.items():
        checked = identities[label]
        for n in a.cores:
            affinity = ','.join(map(str,sorted(os.sched_getaffinity(0))[:n]))
            cmd = ['taskset','-c',affinity,sys.executable,ROOT/'tools/benchmark_stacks.py',file,
                   '--verified-input',checked,'--preserve-order','--spec',a.spec,'--affinity','all' if n>1 else 'one',
                   '--timeout',str(a.timeout),'--rust-worker',a.rust,'--python-worker',a.python,'--samples','1','--target-ms','0']
            output = d/f'{label}-{n}.jsonl'
            # Verify each timed reader and executor width against the same reference.
            subprocess.run([*cmd,'--engines',','.join(stacks),'--rust-workers','0','--output',output],check=True)
            checks += ['--checks',f'{label}={output}']
            if 'pooled' in a.engines:
                pool = d/f'{label}-{n}-pooled.jsonl'
                subprocess.run([*cmd,'--engines','rust-mmap','--rust-workers',str(a.pool_workers),'--output',pool],check=True)
                checks += ['--checks',f'{label}/pooled={pool}']
    subprocess.run([sys.executable,ROOT/'tools/summarize_producer_corpus.py',raw,'--spec',a.spec,
                    '--reference','geodesk' if 'geodesk' in libraries else next(iter(libraries)),
                    *checks,*(['--reference-summary', a.reference_summary] if a.reference_summary else []),
                    *(['--same-cases-as',a.same_cases_as] if a.same_cases_as else []),
                    '--output',d/'summary.json'],check=True)
    print(d/'summary.json')


if __name__=='__main__':
    main()
