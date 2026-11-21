"""Measure first match, early return and tile-stage faults on the private UK input."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from benchmark_application_sessions import verified_inputs, file_digest
from benchmark_cache import FileCache
from benchmark_producer_corpus import sync_inputs, williams_order
from benchmark_reader_cache import HANDOFF_ARMS, handoff_mode, selective_arms
from prepare_prefetch_control import KEY

CASES = {'boundary': 'a[boundary=administrative][admin_level=8][name=Cambridge]',
         'cafe': 'n[amenity=cafe]'}
ARMS = ['ordered-demand', 'pool-1-demand', 'pool-32-demand', 'pool-32-hints', 'roots-32', 'leaf-32']


def run(args):
    if args.output.exists() or args.rounds < 1:
        raise ValueError('use a new output and positive rounds')
    identity = verified_inputs(args.file, None, None)['geodesk']
    if b'GOL_BENCH_FIRST_MATCH' not in args.rust.read_bytes():
        raise ValueError('worker is missing the first-match probe')
    available = sorted(os.sched_getaffinity(0))
    if not 1 <= args.cores <= len(available):
        raise ValueError('requested CPU count exceeds the affinity mask')
    cpus = available[:args.cores]
    os.sched_setaffinity(0, cpus)
    cache = FileCache(args.file)
    sync_inputs({'uk': args.file})
    rows, faults, windows = [], [], []
    selective = args.policy != "tile-handoff"
    arms = selective_arms(args.policy) if selective else HANDOFF_ARMS
    with tempfile.TemporaryDirectory() as temp:
        spec = Path(temp) / 'case.json'
        def worker(arm, *flags, ids=False):
            width, hints = (HANDOFF_ARMS | arms)[arm]
            env = {k:v for k,v in os.environ.items() if not k.startswith(('GOL_BENCH_', 'GOL_READER_BENCH_'))}
            env.update({KEY:hints, 'GOL_BENCH_TILE_FETCH':handoff_mode(arm), 'GOL_READER_BENCH_AUTO_INDEX_BYTES':'0'})
            env.update({key:'1' for key in flags})
            cmd = [args.rust, args.file, spec, 'mmap-immutable', '1' if ids else '0', '0', '-', '0', 'skip', str(width)]
            if ids:
                cmd.append('ids-json')
            r = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env, timeout=120)
            return json.loads(r.stdout), r.stderr.splitlines()
        def cold():
            cache.prepare('cold')
            if cache.snapshot()['resident_bytes']:
                raise ValueError('expected zero resident input pages')
        allowed = {}
        for name, selector in CASES.items():
            spec.write_text(json.dumps([dict(name=name, query=selector, operation='iterate')]))
            ids, _ = worker('ordered-demand', ids=True)
            allowed[name] = set(ids)
            if len(allowed[name]) != {'boundary':1, 'cafe':28331}[name]:
                raise ValueError(f'{name}: unexpected full result')
        for rep in range(args.rounds):
            for name in williams_order(list(CASES), rep):
                spec.write_text(json.dumps([dict(name=name, query=CASES[name], operation='iterate', limit=1)]))
                for arm in williams_order(list(arms) if selective else ARMS, rep):
                    cold()
                    for state in ('cold', 'warm'):
                        row, _ = worker(arm, 'GOL_BENCH_FIRST_MATCH')
                        if row['id'].lower() not in allowed[name] or not 0 < row['first_ns'] <= row['release_ns']:
                            raise ValueError(f'{name}/{arm}: invalid first match {row}')
                        rows.append(dict(case=name, arm=arm, round=rep+1, state=state, **row))
        spec.write_text(json.dumps([dict(name='boundary', query=CASES['boundary'], operation='iterate')]))
        for arm in (() if selective else ('roots-4','roots-16','roots-32','leaf-32')):
            cold()
            row, stderr = worker(arm, 'GOL_BENCH_HANDOFF_TRACE')
            entry, = [s for s in stderr if s.startswith('HANDOFF ')]
            tiles, sent, fetch, evaluate = map(int, entry.split()[1:])
            if row['count'] != 1 or not 0 < sent <= tiles:
                raise ValueError(f'{arm}: inactive handoff')
            faults.append(dict(arm=arm, tiles=tiles, transferred=sent, fetch_major_faults=fetch,
                               evaluator_major_faults=evaluate, resident_bytes=cache.snapshot()['resident_bytes']))
        if selective:
            gate = args.policy == 'root-gate'
            marker = 'ROOT_GATE ' if gate else 'LEAF_WINDOW '
            fields = ('sampled_tiles','dense_tiles','admitted_tiles') if gate else (
                'observed','adjacent','requests','requested_pages','used_pages')
            for case, selector in {**CASES, 'roads':'w[highway]', 'hiking':'r[route=hiking]'}.items():
                spec.write_text(json.dumps([dict(name=case, query=selector, operation='iterate')]))
                cold()
                result, stderr = worker(args.policy, 'GOL_BENCH_GATE_TRACE' if gate else 'GOL_BENCH_WINDOW_TRACE')
                stats = [list(map(int, line.split()[1:])) for line in stderr if line.startswith(marker)]
                totals = [sum(values) for values in zip(*stats, strict=True)] if stats else [0]*len(fields)
                if (len(totals) != len(fields) or totals[1] > totals[0]
                        or (gate and totals[2] > totals[0]) or (not gate and totals[4] > totals[3])):
                    raise ValueError(f'inconsistent counters for {case}')
                cold()
                ref, _ = worker('pool-32-demand')
                if result['count'] != ref['count']:
                    raise ValueError(f'{case}: diagnostic count differs')
                windows.append(dict(case=case, selector=selector, count=result['count'], groups=len(stats),
                                    **dict(zip(fields, totals, strict=True))))
            if not any(row['admitted_tiles' if gate else 'requests'] for row in windows):
                raise ValueError('selective advice never issued a request across the diagnostic queries')
    args.output.write_text(json.dumps(dict(input=identity, worker_sha256=file_digest(args.rust),
                                           rounds=args.rounds, cpus=cpus, selectors=CASES, latency=rows, fault_probes=faults, window_probes=windows))+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('file', type=Path)
    p.add_argument('--rust', type=Path, required=True)
    p.add_argument('--cores', type=int, default=1, help='logical CPUs available to both stages')
    p.add_argument('--policy', choices=('tile-handoff','leaf-window','root-gate'), default='tile-handoff')
    p.add_argument('--rounds', type=int, default=6)
    p.add_argument('--output', type=Path, required=True)
    run(p.parse_args())
