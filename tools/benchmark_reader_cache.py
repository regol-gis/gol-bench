#!/usr/bin/env python3
"""Measure cold and predecessor-warmed reader histories on a private UK GOL copy."""
import argparse
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import time

from benchmark_application_sessions import CASES, ROOT, SDK, WORKER, digest, file_digest, run_worker, signature, verified_inputs
from benchmark_cache import FileCache
from benchmark_producer_corpus import sync_inputs, williams_order, worker_usage
from provenance import harness_source
from prepare_prefetch_control import KEY

CASES_TO_CHECK = ('next-higher', 'varna', 'campsbourne')
READERS = ('rust-reader', 'python-reader')
OVERLAP_CASES = (*CASES_TO_CHECK, 'world2pixel')
# Policy labels distinguish the public executor width and explicit page hints.
OVERLAP_ARMS = {
    'default': (0, 'default'), 'neither': (0, 'neither'), 'charged-io': (0, 'charged-io'),
    'pool-4-demand': (4, 'neither'), 'pool-16-demand': (16, 'neither'),
    'pool-32-demand': (32, 'neither'), 'pool-32-hints': (32, 'default'),
}


HANDOFF_ARMS = {
    'ordered-demand': (0, 'neither'), 'pool-1-demand': (1, 'neither'),
    'pool-32-demand': (32, 'neither'), 'pool-32-hints': (32, 'default'),
    'roots-4': (1, 'neither'), 'roots-16': (1, 'neither'),
    'roots-32': (1, 'neither'), 'leaf-32': (1, 'neither'),
}


def selective_arms(mode):
    return {'pool-32-demand': (32, 'neither'), 'pool-32-hints': (32, 'default'), mode: (32, mode)}


def parallel_experiment(args):
    return args.query_overlap or args.tile_handoff or args.selective_policy


def handoff_mode(label):
    return label.replace('-', '/') if label.startswith(('roots-', 'leaf-')) else ''


def current_cgroup():
    for row in Path('/proc/self/cgroup').read_text().splitlines():
        if row.startswith('0::/'):
            relative = Path(row[4:])
            if not relative.is_absolute() and '..' not in relative.parts:
                directory = Path('/sys/fs/cgroup') / relative
                if directory.is_dir():
                    return directory
    return None


def accounting(cgroup):
    # Linux charges the waits and I/O of reaped descendants to the parent. No
    # other children run between these snapshots; fincore runs outside them.
    counters = worker_usage()
    io = dict(line.split(': ') for line in Path('/proc/self/io').read_text().splitlines())
    counters['read_bytes'] = int(io['read_bytes'])
    cpu_stat = cgroup / 'cpu.stat' if cgroup else None
    if cpu_stat and cpu_stat.is_file():
        fields = dict(row.split() for row in cpu_stat.read_text().splitlines())
        if 'throttled_usec' in fields:
            counters['cgroup_throttled_us'] = int(fields['throttled_usec'])
    return counters


def measure(args, case, engine, expected, env, cgroup, workers=0):
    before = accounting(cgroup)
    start = time.perf_counter_ns()
    result = run_worker(case, engine, args, env=env, workers=workers, reader_stats=parallel_experiment(args))
    elapsed = time.perf_counter_ns() - start
    after = accounting(cgroup)
    if parallel_experiment(args):
        steps = result.pop('steps')
        stats = result
    else:
        steps, stats = result, {}
    actual = signature(steps)
    if len(actual) != len(expected):
        raise ValueError('unexpected number of measured steps')
    for row, ref in zip(actual, expected, strict=True):
        if row['fingerprint'] is None:
            row = dict(row, fingerprint=ref['fingerprint'])
        if row != ref:
            raise ValueError(f"{case['id']}/{engine}: measured work differs from verified results")
    return dict(engine=engine, subprocess_wall_ns=elapsed, steps=steps,
                session_ns=sum(step['elapsed_ns'] for step in steps), **stats,
                usage={k: after[k] - value for k, value in before.items()})


def run(args):
    if args.output.exists() or args.rounds < 1:
        raise ValueError('use a new output path and a positive number of rounds')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    parallel = parallel_experiment(args)
    identity = verified_inputs(args.file, None, None)
    available = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(available)})
    try:
        cache = FileCache(args.file)
        sync_inputs({'uk':args.file})
        cases = list(dict.fromkeys(args.case or (OVERLAP_CASES if parallel else CASES_TO_CHECK)))
        verified = {}
        pins = json.loads((ROOT/'benchmarks/application-session-signatures.json').read_text())
        for name in cases:
            for engine in READERS:
                key = f'{name}/{engine}'
                verified[key] = signature(run_worker(CASES[name], engine, args, verify=True))
                if digest(verified[key]) != pins[key]:
                    raise ValueError(f'{key}: reference signature differs')
        environments = {None: None}
        thread_probe = {}
        widths = {}
        if args.prefetch_control or parallel:
            if KEY.encode() not in args.rust.read_bytes():
                raise ValueError('expected the benchmark-instrumented Rust worker')
            experiments = []
            policies = OVERLAP_ARMS if args.query_overlap else {
                p: (0, p) for p in ('default', 'no-sequential', 'no-helpers', 'neither', 'charged-io')}
            if args.tile_handoff:
                if b'GOL_BENCH_TILE_FETCH' not in args.rust.read_bytes():
                    raise ValueError('expected the instrumented tile handoff reader')
                policies = HANDOFF_ARMS
            if args.selective_policy:
                marker = b'GOL_BENCH_GATE_TRACE' if args.selective_policy == 'root-gate' else b'GOL_BENCH_WINDOW_TRACE'
                if marker not in args.rust.read_bytes():
                    raise ValueError(f'expected the {args.selective_policy} experiment worker')
                policies = selective_arms(args.selective_policy)
            for policy, (width, hint_policy) in policies.items():
                widths[policy] = width
                environments[policy] = {**{k:v for k,v in os.environ.items() if k not in ('GOL_BENCH_WINDOW_TRACE','GOL_BENCH_GATE_TRACE')}, KEY: hint_policy}
                if args.tile_handoff:
                    environments[policy]['GOL_BENCH_TILE_FETCH'] = handoff_mode(policy)
                experiments.append(((READERS[0], READERS[0]), policy))
            if not (args.tile_handoff or args.selective_policy):
                experiments.append(((READERS[1], READERS[1]), None))
            if args.python_worker_control and parallel:
                raise ValueError('--python-worker-control requires --prefetch-control')
            if args.python_worker_control:
                control = 'python-one-worker'
                environments[control] = {**os.environ, 'LD_PRELOAD': str(args.python_worker_control.resolve(strict=True))}
                experiments.append(((READERS[1], READERS[1]), control))
                probe = ('import geodesk, json, os, sys; initial=len(os.listdir("/proc/self/task")); '
                         'f=geodesk.Features(sys.argv[1]); print(json.dumps([initial,len(os.listdir("/proc/self/task"))]))')
                for label, env in [('default', None), (control, environments[control])]:
                    thread_probe[label] = json.loads(subprocess.check_output(
                        [args.python, '-c', probe, str(args.file.resolve())], env=env))
                before, after = thread_probe[control]
                if after - before != 1 or thread_probe['default'][1] <= after:
                    raise ValueError(f'could not verify one native worker: {thread_probe}')
            for name in cases:
                for (engine, _), policy in experiments:
                    if policy is None:
                        continue
                    ref = signature(run_worker(CASES[name], engine, args, verify=True,
                                               env=environments[policy], workers=widths.get(policy, 0)))
                    if ref != verified[f'{name}/{engine}']:
                        raise ValueError(f'{name}/{policy}: control changed query results')
        else:
            if args.python_worker_control:
                raise ValueError('--python-worker-control requires --prefetch-control')
            experiments = [(order, None) for order in product(READERS, repeat=2)]
        cgroup = current_cgroup()
        memory = cgroup / 'memory.max' if cgroup else None
        report = dict(input=identity['geodesk'], harness_source=harness_source(),
                      cpu=min(available), cgroup=str(cgroup) if cgroup else None,
                      memory_limit=memory.read_text().strip() if memory and memory.is_file() else None,
                      rust_worker_sha256=file_digest(args.rust), python=str(args.python),
                      geodesk=subprocess.check_output([args.python,'-c','from importlib.metadata import version;print(version("geodesk"))'],text=True).strip(),
                      filesystem=subprocess.check_output(['stat','-f','-c','%T',args.file],text=True).strip(),
                      rounds=args.rounds, signatures=verified, cache=cache.metadata,
                      python_thread_probe=thread_probe, pairs=[])
        if args.selective_policy:
            report[args.selective_policy.replace('-', '_')] = policies
        if args.query_overlap:
            report['query_overlap'] = {label: dict(workers=n, hints=p) for label, (n, p) in policies.items()}
        if args.tile_handoff:
            report['tile_handoff'] = {label: dict(evaluators=n, hints=p, fetch=handoff_mode(label))
                                      for label, (n, p) in policies.items()}
        for i in range(args.rounds):
            for name in williams_order(cases, i):
                for order, policy in williams_order(experiments,i):
                    prep = cache.prepare('cold')
                    runs = []
                    for engine in order:
                        before = cache.snapshot()['resident_bytes']
                        obs = measure(args, CASES[name], engine, verified[f'{name}/{engine}'],
                                      environments[policy], cgroup, widths.get(policy, 0))
                        obs.update(resident_before=before, resident_after=cache.snapshot()['resident_bytes'])
                        runs.append(obs)
                    report['pairs'].append(dict(case=name, round=i+1, policy=policy, preparation=prep, runs=runs))
                    print(name, policy or 'ordinary', ' → '.join(order), *(f"{r['session_ns']/1e6:.1f}ms" for r in runs), flush=True)
        with args.output.open('x') as f:
            json.dump(report,f,indent=2,allow_nan=False)
            f.write('\n')
    finally:
        os.sched_setaffinity(0,available)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('file',type=Path,help='dedicated benchmark file; its Linux file cache will be evicted repeatedly')
    p.add_argument('--python-worker-control', type=Path, help='shared object to test with a single native Python worker')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--root-gate', dest='selective_policy', action='store_const', const='root-gate', help='only admit ordinary spans after a dense tile majority')
    group.add_argument('--leaf-window', dest='selective_policy', action='store_const', const='leaf-window', help='compare demand, ordinary hints and selective leaf windows at 32 workers')
    group.add_argument('--tile-handoff', action='store_true', help='compare bounded tile preparation with demand and hinted workers')
    group.add_argument('--query-overlap', action='store_true', help='compare demand-only Rust tile workers with ordered and hinted readers; needs parallel instrumented worker')
    group.add_argument('--prefetch-control',action='store_true',help='compare independent Regol prefetch switches; requires the private instrumented worker')
    p.add_argument('--rust',type=Path,default=WORKER)
    p.add_argument('--python',type=Path,default=SDK/'python-current313/bin/python')
    p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--case',choices=OVERLAP_CASES,action='append')
    p.add_argument('--output',type=Path,required=True)
    p.set_defaults(scratch=None, gol_tool='gol', rust_cli='regol')
    run(p.parse_args())
