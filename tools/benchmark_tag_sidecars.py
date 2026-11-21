#!/usr/bin/env python3
"""Run verified first-pass tag sidecar trials with controlled Linux residency."""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time

from benchmark_cache import FileCache, require_state
from provenance import harness_source
from benchmark_producer_corpus import sync_inputs, williams_order
from benchmark_stacks import cpu_model
from input_identity import identify, unchanged

MODES = ('gol', 'exact', 'words', 'scalar', 'selective', 'leaves')
PLANS = {
    'gol': {'gol'}, 'exact': {'gol', 'direct', 'postings'}, 'words': {'gol', 'words'},
    'scalar': {'gol', 'words'}, 'selective': {'gol', 'direct', 'postings', 'words'},
    'leaves': {'gol', 'postings'},
}


def parse_cases(spec):
    cases = json.loads(spec)
    if not isinstance(cases, list) or not cases:
        raise ValueError('expected a nonempty list of cases')
    allowed = {'name', 'query', 'operation', 'bounds', 'role', 'selector_id'}
    for case in cases:
        if (not isinstance(case, dict) or not set(case) <= allowed
                or not all(isinstance(case.get(key), str) and case[key] for key in ('name', 'query'))
                or case.get('operation') not in ('iterate', 'tags', 'geometry')):
            raise ValueError('use named iterate, tags or geometry cases with optional projected bounds')
        if 'bounds' in case:
            bounds = case['bounds']
            if (not isinstance(bounds, list) or len(bounds) != 4
                    or any(type(v) is not int or not -2**31 <= v < 2**31 for v in bounds)
                    or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
                raise ValueError(f"{case['name']}: invalid projected bounds")
    if len({case['name'] for case in cases}) != len(cases):
        raise ValueError('case names must be unique')
    return cases


def args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gol", type=Path, help="Use private input copies for cold trials.")
    parser.add_argument("--exact", type=Path)
    parser.add_argument("--words", type=Path)
    parser.add_argument("--selective", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--standalone", action="store_true",
                        help="Start a fresh worker and reset residency for each case.")
    parser.add_argument("--worker", type=Path, default=Path("target/release/examples/tag_sidecar_trial"))
    parser.add_argument("--cpu", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per verification or timed worker")
    parser.add_argument("--cache", choices=["cold", "resident"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", default=["gol", "exact", "words", "scalar"], choices=MODES)
    cfg = parser.parse_args()
    if min(cfg.rounds, cfg.timeout) < 1 or cfg.cpu not in os.sched_getaffinity(0):
        parser.error('positive rounds, timeout and an available CPU required')
    if len(set(cfg.modes)) != len(cfg.modes):
        parser.error('modes must be unique')
    return cfg


def invoke(cfg, command, sidecar, spec):
    args = ['taskset', '-c', str(cfg.cpu), cfg.worker, command, cfg.gol, sidecar or '-', spec]
    started = time.monotonic_ns()
    run = subprocess.run(args, capture_output=True, timeout=cfg.timeout)
    elapsed = time.monotonic_ns() - started
    if run.returncode:
        details = run.stderr[-1800:].decode(errors='replace').strip()
        if command.startswith('verify-') and 'expected build GOL' in details:
            raise ValueError(f'{cfg.worker} does not support {command}; rebuild with gol-bench setup sidecars')
        raise ValueError(f'{command} failed: {details}')
    return [json.loads(line) for line in run.stdout.splitlines()], elapsed


def observations(rows, cases, mode, verification, expected=None):
    if (len(rows) != len(cases) + 2 or type(rows[0].get('open_ns')) is not int
            or not isinstance(rows[-1].get('faults'), list)):
        raise ValueError(f'{mode}: incomplete worker output')
    found = {}
    for row, case in zip(rows[1:-1], cases, strict=True):
        name = case['name']
        if (row.get('name') != name or row.get('plan') not in PLANS[mode]
                or 'error' in row or type(row.get('count')) is not int or row['count'] < 0):
            raise ValueError(f'{mode}/{name}: invalid query result or sidecar plan: {row}')
        if verification:
            if type(row.get('payload_errors')) is not int or row['payload_errors'] != 0:
                raise ValueError(f'{mode}/{name}: verification found payload errors')
            # Earlier standalone workers returned count alone for iteration and geometry.
            items = row.get('items', row['count'] if case['operation'] != 'tags' else None)
        else:
            items = row.get('items')
            if (row.get('operation') != case['operation'] or type(row.get('elapsed_ns')) is not int
                    or row['elapsed_ns'] < 0):
                raise ValueError(f'{mode}/{name}: invalid timing or operation')
        if type(items) is not int or items < 0:
            raise ValueError(f'{mode}/{name}: missing verified item count. Rebuild the sidecar worker.')
        count_items = row['count'], items
        if expected is not None and count_items != expected[name][:2]:
            raise ValueError(f'{mode}/{name}: count or consumption differs from verification')
        if not verification and row['plan'] != expected[name][2]:
            raise ValueError(f'{mode}/{name}: timed plan differs from verification')
        found[name] = (*count_items, row['plan'])
    return found


def cache_record(path, row):
    return dict(file=str(path), size=row['file_bytes'], res=row['resident_bytes'])


def cache_rows(files, caches):
    return [cache_record(path, caches[path].snapshot()) for path in files]


@contextmanager
def ready_files(files, caches, state):
    with ExitStack() as stack:
        mappings = {}
        for path in files:
            if state == 'warm':
                mappings[path] = stack.enter_context(caches[path].faulted_mapping())
            else:
                stack.enter_context(caches[path].sample(state))
        # Check every input after preparing the last. ZFS can report a
        # different residency for an observer that opens its own mapping.
        before = []
        for path in files:
            cache = caches[path]
            row = cache.mapping_snapshot(mappings[path]) if state == 'warm' else cache.snapshot()
            require_state(row, state)
            before.append(cache_record(path, row))
        yield before


def save_json(path, report):
    with path.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')


def main():
    cfg = args()
    cfg.gol = cfg.gol.resolve(strict=True)
    cfg.worker = cfg.worker.resolve(strict=True)
    indexes = {"gol": None, "exact": cfg.exact, "words": cfg.words,
               "scalar": cfg.words, "selective": cfg.selective, "leaves": cfg.exact}
    for mode in cfg.modes:
        if mode != 'gol':
            if indexes[mode] is None:
                raise SystemExit(f'missing sidecar path for {mode}')
            indexes[mode] = indexes[mode].resolve(strict=True)
    files = list(dict.fromkeys([cfg.gol, *(indexes[mode] for mode in cfg.modes if indexes[mode])]))
    if any(path.stat().st_nlink != 1 for path in files) and cfg.cache == 'cold':
        raise SystemExit('cold trials require private copies of the GOL and sidecars')
    if any(indexes[mode] and indexes[mode].samefile(cfg.gol) for mode in cfg.modes):
        raise SystemExit('the GOL cannot also be used as a sidecar')
    metadata_path = cfg.output.with_suffix('.metadata.json')
    summary_path = cfg.output.with_suffix('.summary.json')
    if any(path.exists() for path in (cfg.output, metadata_path, summary_path)):
        raise SystemExit('choose a new output path')
    spec_bytes = cfg.spec.read_bytes()
    cases = parse_cases(spec_bytes)
    inputs = {path: identify(path) for path in files}
    worker = identify(cfg.worker)
    caches = {path: FileCache(path) for path in files}
    state = 'cold' if cfg.cache == 'cold' else 'warm'
    cycle = len(cfg.modes) * (2 if len(cfg.modes) > 1 and len(cfg.modes) % 2 else 1)
    metadata = dict(
        format=1, consumer='tag_sidecar_trial', harness_source=harness_source(), cache=cfg.cache, standalone=cfg.standalone,
        cpu=cfg.cpu, cpu_model=cpu_model(), machine=platform.uname()._asdict(),
        rounds=cfg.rounds, modes=cfg.modes, crossover_cycle_rounds=cycle,
        gol_sha256=inputs[cfg.gol]['sha256'], gol_bytes=inputs[cfg.gol]['stat'][2],
        worker_sha256=worker['sha256'],
        spec_sha256=hashlib.sha256(spec_bytes).hexdigest(),
        sidecars={mode: inputs[indexes[mode]]['sha256'] for mode in cfg.modes if indexes[mode]},
        sidecar_bytes={mode: inputs[indexes[mode]]['stat'][2] for mode in cfg.modes if indexes[mode]},
        queries=cases,
        file_cache=[caches[path].metadata for path in files],
    )
    results = {case['name']: dict(name=case['name'], operation=case['operation'], arms={}) for case in cases}
    with tempfile.TemporaryDirectory(prefix="regol-tag-trial-") as temporary:
        whole = Path(temporary)/'cases.json'
        whole.write_bytes(spec_bytes)
        reference_rows, _ = invoke(cfg, 'verify', None, whole)
        reference = observations(reference_rows, cases, 'gol', verification=True)
        verified = {'gol': reference}
        for mode in cfg.modes:
            if mode == 'gol':
                continue
            command = f'verify-{mode}' if mode in ('scalar', 'leaves') else 'verify'
            rows, _ = invoke(cfg, command, indexes[mode], whole)
            verified[mode] = observations(rows, cases, mode, verification=True, expected=reference)
        for name, (count, items, _) in reference.items():
            results[name].update(count=count, items=items)
        workloads = [('corpus', whole, cases)]
        if cfg.standalone:
            workloads = []
            for i, case in enumerate(cases):
                spec = Path(temporary)/f'{i}.json'
                spec.write_text(json.dumps([case]))
                workloads.append((case['name'], spec, [case]))
        if cfg.cache == 'cold':
            sync_inputs(dict(enumerate(files)))
        save_json(metadata_path, metadata)
        with cfg.output.open('x') as output:
            for round_number in range(cfg.rounds):
                modes = williams_order(cfg.modes, round_number)
                for workload, spec, selected in workloads:
                    for position, mode in enumerate(modes, 1):
                        sidecar = indexes[mode]
                        active = [cfg.gol, sidecar] if sidecar else [cfg.gol]
                        command = mode if mode in ('scalar', 'leaves') else 'run'
                        with ready_files(active, caches, state) as before:
                            rows, elapsed = invoke(cfg, command, sidecar, spec)
                            after = cache_rows(active, caches)
                        observations(rows, selected, mode, verification=False, expected=verified[mode])
                        record = dict(cache=cfg.cache, workload=workload, round=round_number, mode=mode,
                                      cpu=cfg.cpu, cache_before=before, cache_after=after, position=position,
                                      process_wall_ns=elapsed, verified=True,
                                      **rows[0], **rows[-1], cases=rows[1:-1])
                        print(json.dumps(record, separators=(",", ":")), file=output, flush=True)
                        for row in record['cases']:
                            arm = results[row['name']]['arms'].setdefault(mode, dict(plan=row['plan'], rounds=[]))
                            arm['rounds'].append(dict(round=round_number, elapsed_ns=row['elapsed_ns'],
                                                      open_ns=rows[0]['open_ns'], process_wall_ns=elapsed))
    for path, identity in inputs.items():
        unchanged(path, identity)
    unchanged(cfg.worker, worker)
    save_json(summary_path, dict(metadata, verified=True, cases=list(results.values())))
    print(summary_path)


if __name__ == "__main__":
    main()
