#!/usr/bin/env python3
"""Measure throughput for independent fresh-process histories on one immutable GOL."""
from provenance import harness_source
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
from itertools import product
import json
import math
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace

from benchmark_cache import FileCache
from benchmark_producer_corpus import command, williams_order
from benchmark_stacks import ROOT, REF, WORKER, cpu_model, resolve, tsv, worker_command
from campaign import ENGINES
from input_identity import identify, unchanged
from summarize_producer_corpus import MATCH, records, verify_worker
from summarize_stack_benchmarks import compare
from worker_identity import identities


def load_json(path):
    return json.loads(path.read_bytes())


def history_groups(cases, views):
    """Keep complete city histories together and retain any trailing global step."""
    if not views:
        return {'fixture': cases}
    groups = {}
    remaining = list(cases)
    for city in views:
        city_cases = [case for case in remaining if case['name'].startswith(f'{city}_')]
        if not city_cases or remaining[:len(city_cases)] != city_cases:
            raise ValueError(f'noncontiguous or missing history for {city}')
        groups[city] = city_cases
        del remaining[:len(city_cases)]
    if remaining:
        groups['global'] = remaining
    return groups


def verified_counts(report, spec, digest, width, stack):
    metadata = load_json(report.with_suffix('.metadata.json'))
    if metadata['gol_sha256'] != digest or len(metadata['cpu_affinity']) != width:
        raise ValueError(f'{report}: different GOL or CPU width')
    if load_json(report.with_suffix('.queries.json')) != spec:
        raise ValueError(f'{report}: verification workload differs')
    observations = [row for row in records(report) if row['stack'] == stack]
    if [row['name'] for row in observations] != [case['name'] for case in spec]:
        raise ValueError(f'{report}: incomplete or reordered {stack} verification')
    return metadata, observations


def reference_run(args, identity, cpus):
    run = args.scale_run
    dataset = args.dataset
    manifest = load_json(run/'run.json')
    if manifest['status'] != 'complete' or dataset not in manifest['completed']:
        raise ValueError(f'{run}: expected a completed scale run for {dataset}')
    index = load_json(run/'index.json')[dataset]
    if index['gol_sha256'] != identity['sha256']:
        raise ValueError('GOL SHA-256 differs from the verified scale input')
    spec_bytes = (run/f'{dataset}.queries.json').read_bytes()
    cases = resolve(json.loads(spec_bytes))
    directory = run/dataset
    with closing(records(directory/'timings.jsonl.gz')) as raw:
        timed = next(raw)
    if (timed['spec_sha256'] != hashlib.sha256(spec_bytes).hexdigest()
            or timed['inputs'][dataset]['sha256'] != identity['sha256']
            or timed['cases'] != [case['name'] for case in cases]):
        raise ValueError('verified scale input or workload differs from timing provenance')
    if len(cpus) not in timed['core_counts'] or set(args.engines) - set(timed['engines']):
        raise ValueError('scale evidence must cover the requested CPU width and every reader')
    for name in ('rust_workers', 'pooled_rust_workers'):
        setattr(args, name, timed[name])
    args.pool_workers = args.pooled_rust_workers

    base = directory/f'{dataset}-{len(cpus)}.jsonl'
    _, reference = verified_counts(base, cases, identity['sha256'], len(cpus), 'python-current')
    recorded_workers = {}
    for engine in args.engines:
        stack = ENGINES[engine]
        report = base.with_name(f'{base.stem}-pooled.jsonl') if engine == 'pooled' else base
        metadata, observations = verified_counts(report, cases, identity['sha256'], len(cpus), stack)
        verify_worker(timed, metadata, engine, len(cpus), report)
        for case, actual, expected in zip(cases, observations, reference, strict=True):
            if compare(actual, expected, case['operation']) not in MATCH:
                raise ValueError(f'{engine}/{case["name"]}: archived verification failed')
        recorded_workers[engine] = metadata['workers'][stack]
    groups = history_groups(cases, index['dataset'].get('views'))
    return groups, {row['name']: row for row in reference}, timed, recorded_workers


def live_workers(args, cpus, spec, tab, timed, archived):
    """Check adapter and SDK digests using the same dependencies as the scale pass."""
    workers = {}
    for engine in args.engines:
        stack = ENGINES[engine]
        verification_args = SimpleNamespace(file=args.file, samples=1, target_ms=0,
                                            engines=stack, rust_worker=args.regol,
                                            python_worker=args.python,
                                            rust_workers=args.pool_workers if engine == 'pooled' else args.rust_workers)
        cmd = worker_command(stack, verification_args, spec, tab, cpus)
        if engine == 'java':
            # Use the classpath that the timed first-pass command will actually load.
            cmd[cmd.index('-cp') + 1] = args.java_classpath
        current = identities({stack: cmd})
        verify_worker(timed, {'workers': current, 'args': vars(verification_args)},
                      engine, len(cpus), spec)
        old = archived[engine]['sha256']
        live = current[stack]['sha256']
        old_digests = set(old.values())
        if engine == 'python':
            # Earlier stack sidecars omitted this digest, but the timing header retained it.
            old_digests.add(timed['python_geodesk_sha256'])
        live_digests = set(live.values())
        # The persistent Java verification classpath may add unrelated Clojure artifacts.
        if (engine == 'java' and not live_digests <= old_digests) or (
                engine != 'java' and live_digests != old_digests):
            raise ValueError(f'{engine}: local worker dependencies differ from verified scale run')
        workers[engine] = current[stack]
    return workers


def timed_batch(commands, clients, env, timeout):
    def invoke(cmd):
        started = time.monotonic_ns()
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
        return time.monotonic_ns() - started, proc

    # A slot starts another client as soon as its previous process exits.
    with ThreadPoolExecutor(max_workers=clients) as pool:
        started = time.monotonic_ns()
        results = list(pool.map(invoke, commands))
        return time.monotonic_ns() - started, results


def validate_observation(proc, cases, reference):
    if proc.returncode:
        message = proc.stderr[-1500:].decode(errors='replace')
        raise ValueError(f'client exited {proc.returncode}: {message}')
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    if [row['name'] for row in rows] != [case['name'] for case in cases]:
        raise ValueError('client returned an incomplete or reordered history')
    for row in rows:
        expected = reference[row['name']]
        if ('error' in row or row.get('count') != expected['count']
                or row.get('work_items') != expected.get('work_items', expected.get('items'))):
            raise ValueError(f'{row["name"]}: client output differs from the verified history')


def summarize(trials):
    result = {}
    for trial in trials:
        key = f"{trial['engine']}/{trial['clients']}"
        arm = result.setdefault(key, {'throughput': [], 'latencies': []})
        arm['throughput'].append(trial['histories_per_second'])
        arm['latencies'].extend(client['latency_ns'] for client in trial['observations'])
    summaries = {}
    for key, arm in result.items():
        latencies = sorted(arm['latencies'])
        summaries[key] = {'median_histories_per_second': median(arm['throughput']),
                          'client_p50_ns': median(latencies),
                          'client_p95_ns': latencies[math.ceil(.95 * len(latencies)) - 1]}
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale-run', required=True, type=Path)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--engines', nargs='+', choices=ENGINES, default=['regol', 'python'])
    parser.add_argument('--clients', nargs='+', type=int, default=[1, 2, 4])
    parser.add_argument('--jobs', type=int, default=24, help='total fresh histories per arm and round')
    parser.add_argument('--cores', type=int, default=1, help='shared reader CPU budget')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--cache', choices=['warm', 'inherit'], default='warm')
    parser.add_argument('--rust', dest='regol', type=Path, default=WORKER)
    parser.add_argument('--python', type=Path, default=REF/'python-current313/bin/python')
    parser.add_argument('--timeout', type=int, default=300, help='seconds per client process')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.dataset.isascii() or not args.dataset.replace('-', '').isalnum():
        parser.error('invalid dataset label')
    cpus = sorted(os.sched_getaffinity(0))[:args.cores]
    if (min(args.jobs, args.rounds, args.cores, args.timeout, *args.clients) < 1
            or len(cpus) != args.cores or max(args.clients) > args.jobs):
        parser.error('positive job, round, CPU, timeout and client counts required within available budgets')
    if len(set(args.clients)) != len(args.clients) or len(set(args.engines)) != len(args.engines):
        parser.error('client counts and engines must be unique')
    if args.output.exists():
        parser.error('output already exists')
    args.file = args.file.resolve(strict=True)
    args.regol = args.regol.resolve()
    args.python = args.python.absolute()
    args.cpp = REF/'cpp-worker'
    args.java_classpath = f'{REF}/classes:{REF}/java-current/*'
    identity = identify(args.file)
    groups, reference, timed, archived = reference_run(args, identity, cpus)
    if args.jobs < len(groups):
        parser.error('--jobs must cover every history')
    names = list(groups)
    sequence = [names[i % len(names)] for i in range(args.jobs)]
    arms = list(product(args.engines, args.clients))
    cache = FileCache(args.file) if args.cache == 'warm' else None
    env = {key: value for key, value in os.environ.items() if not key.startswith('GOL_READER_BENCH_')}
    env['GOL_READER_BENCH_AUTO_INDEX_BYTES'] = '0'
    env['LD_LIBRARY_PATH'] = os.pathsep.join(filter(None, (str(REF/'geos-install/lib'), env.get('LD_LIBRARY_PATH'))))
    trials = []
    with TemporaryDirectory() as tmp:
        specs = {}
        for i, (name, cases) in enumerate(groups.items()):
            spec = Path(tmp)/f'{i}.json'
            tab = spec.with_suffix('.tsv')
            spec.write_text(json.dumps(cases))
            tab.write_text(tsv(cases))
            specs[name] = spec, tab
        workers = live_workers(args, cpus, *next(iter(specs.values())), timed, archived)
        taskset = ['taskset', '-c', ','.join(map(str, cpus))]
        commands = {engine: [taskset + command(engine, args, args.file, *specs[name], len(cpus))
                             for name in sequence] for engine in args.engines}
        for round_index in range(args.rounds):
            for position, (engine, clients) in enumerate(williams_order(arms, round_index), 1):
                with cache.warm_mapping() if cache else nullcontext(None) as resident:
                    elapsed, results = timed_batch(commands[engine], clients, env, args.timeout)
                observations = []
                for name, (latency, proc) in zip(sequence, results, strict=True):
                    validate_observation(proc, groups[name], reference)
                    observations.append({'history': name, 'latency_ns': latency})
                trials.append({'round': round_index + 1, 'position': position, 'engine': engine,
                               'clients': clients, 'batch_wall_ns': elapsed,
                               'histories_per_second': args.jobs * 1e9 / elapsed,
                               'resident_bytes_before': resident['resident_bytes'] if resident else None,
                               'observations': observations})
                print(f'round {round_index + 1} {engine} {clients} client(s)', flush=True)
    unchanged(args.file, identity)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        'format': 1, 'consumer': 'independent_fresh_process_histories', 'dataset': args.dataset,
        'gol_sha256': identity['sha256'], 'scale_run': str(args.scale_run.resolve()),
        'scale_spec_sha256': timed['spec_sha256'], 'cpu_affinity': cpus, 'cpu_model': cpu_model(),
        'benchmark_revision': subprocess.check_output(
            ['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip(),
        'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'machine': platform.uname()._asdict(), 'workers': workers,
        'regol_workers': args.rust_workers, 'pooled_regol_workers': args.pool_workers,
        'cache': args.cache, 'input_digest_preread': True, 'jobs_per_round': args.jobs,
        'client_latency_boundary': 'process launch through exit and output collection, excluding queue time and decoding',
        'batch_wall_boundary': 'dispatch through collection of the last process result, excluding cache preparation and decoding',
        'crossover_cycle_rounds': len(arms) * (2 if len(arms) > 1 and len(arms) % 2 else 1),
        'histories': {name: [case['name'] for case in cases] for name, cases in groups.items()},
        'rounds': trials, 'summary': summarize(trials)}
    report['harness_source'] = harness_source()
    with args.output.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()
