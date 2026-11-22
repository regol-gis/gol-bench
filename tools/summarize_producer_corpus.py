#!/usr/bin/env python3
"""Summarize first-pass timings on nonempty cases with matching verified payloads."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
from itertools import product
import json
from pathlib import Path
from statistics import median

from benchmark_stacks import ROOT, resolve
from summarize_stack_benchmarks import compare

WORKERS = {'rust-mmap': 'regol', 'python-current': 'python', 'java-current': 'java', 'cpp': 'cpp'}
MATCH = {'content_match', 'identities_match'}
ALTERNATE_READERS = {'baseline', 'pooled'}
EVIDENCE = ('error', 'count', 'work_items', 'items', 'fingerprint', 'work_fingerprint')


def records(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as src:
        for line in src:
            yield json.loads(line)


def verify_worker(timed, verified, engine, width, path):
    if not timed.get('worker_sha256') or not verified.get('workers'):
        return
    stack = next((stack for stack, reader in WORKERS.items() if reader == engine), 'rust-mmap')
    digests = verified['workers'][stack]['sha256']
    if engine in timed['worker_sha256'] and timed['worker_sha256'][engine] not in digests.values():
        raise ValueError(f'{path}: {engine} verification uses a different worker')
    if engine == 'python':
        extensions = [value for filename, value in digests.items() if Path(filename).name.startswith('_geodesk.')]
        if extensions and timed.get('python_geodesk_sha256') not in extensions:
            raise ValueError(f'{path}: Python verification uses a different GeoDesk extension')
    if engine == 'java':
        timed_files = timed.get('java_sha256', {})
        verified_files = {Path(filename).name: value for filename, value in digests.items()}
        if any(verified_files.get(filename) != value for filename, value in timed_files.items()):
            raise ValueError(f'{path}: Java verification uses a different classpath')
    if stack == 'rust-mmap':
        default = width if width > 1 else 0
        expected = timed['pooled_rust_workers'] if engine == 'pooled' else timed['rust_workers']
        if engine == 'baseline' and timed.get('baseline_workers') is not None:
            expected = timed['baseline_workers']
        actual = verified['args'].get('rust_workers')
        if (actual if actual is not None else default) != (expected if expected is not None else default):
            raise ValueError(f'{path}: {engine} verification uses a different executor width')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('timings', type=Path, nargs='+')
    parser.add_argument('--checks', metavar='PRODUCER[/READER]=JSONL', action='append', required=True,
                        help='verified benchmark_stacks output; qualify alternate Rust reader reports with /pooled or /baseline')
    parser.add_argument('--reference', default='geodesk', help='producer used as the Python comparison anchor')
    parser.add_argument('--same-cases-as', type=Path, metavar='SUMMARY',
                        help='use the exact matched cases from another run after verifying them locally')
    parser.add_argument('--include-open', action='store_true',
                        help='include library and executor startup in a single-query benchmark')
    parser.add_argument('--spec', type=Path, default=ROOT / 'benchmarks/uk-corpus-queries.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-summary', type=Path, help='also summarize the reference file on its own eligible cases')
    args = parser.parse_args()
    previous = json.loads(args.same_cases_as.read_bytes()) if args.same_cases_as else None
    spec_bytes = args.spec.read_bytes()
    spec_hash = hashlib.sha256(spec_bytes).hexdigest()
    if previous is not None and previous.get('spec_sha256', spec_hash) != spec_hash:
        raise ValueError('locked cohorts were created from a different workload')
    data = json.loads(spec_bytes)
    specs = data if isinstance(data, list) else data['proposed_kernel_cases']
    cases = {c['name']: c for c in specs}
    resolved = resolve(specs)
    if len(cases) != len(specs) or not cases:
        parser.error('expected a nonempty corpus with unique case names')
    if args.include_open and len(cases) != 1:
        parser.error('--include-open requires exactly one query per process')
    checks = {}
    check_metadata = {}
    verified_files = {}
    seen = set()
    disagreements = set()
    for argument in args.checks:
        group, sep, filename = argument.partition('=')
        label, _, mode = group.partition('/')
        if not label or not sep or (mode and mode not in ALTERNATE_READERS):
            parser.error('--checks requires PRODUCER[/READER]=JSONL')
        path = Path(filename)
        sidecar = Path(str(path).removesuffix('.gz'))
        metadata = json.loads(sidecar.with_suffix('.metadata.json').read_bytes())
        file_hash = metadata['gol_sha256']
        if verified_files.setdefault(label, file_hash) != file_hash:
            raise ValueError(f'different files supplied for {label} verification')
        queries = sidecar.with_suffix('.queries.json')
        if queries.exists() and json.loads(queries.read_bytes()) != resolved:
            raise ValueError(f'{path}: verification workload differs from spec')
        for r in records(path):
            engine = WORKERS[r['stack']]
            if mode and engine != 'regol':
                raise ValueError(f'{path}: {mode} verification requires the Regol stack')
            key = label, mode or engine, r['name']
            cpus = r.get('cpus') or metadata.get('cpu_affinity')
            width = len(cpus) if cpus else None
            run_key = (*key, width)
            if run_key in seen:
                raise ValueError(f'duplicate verification row {run_key}')
            seen.add(run_key)
            check_metadata[run_key] = metadata
            first = checks.setdefault(key, r)
            if any(first.get(field) != r.get(field) for field in EVIDENCE):
                disagreements.add(key)

    sessions = []
    inputs = {}
    expected_checks = set()
    for path in args.timings:
        runs = records(path)
        meta = next(runs)
        if 'spec_sha256' in meta and meta['spec_sha256'] != spec_hash:
            raise ValueError(f'{path}: timing workload differs from spec')
        if list(cases) != meta['cases']:
            raise ValueError(f'{path}: query order differs from spec')
        for label, fingerprint in meta['inputs'].items():
            if inputs.setdefault(label, fingerprint) != fingerprint:
                raise ValueError(f'{label} identifies two distinct files')
            if verified_files.get(label) != fingerprint['sha256']:
                raise ValueError(f'verification and timing use different {label} files')
        actual = set()
        verified_arms = set()
        for r in runs:
            combination = (r['round'], r['cores'], r['reader'], r['producer'])
            if combination in actual:
                raise ValueError(f'{path}: duplicate session {combination}')
            actual.add(combination)
            engine = r['reader']
            if [row['name'] for row in r['rows']] != list(cases):
                raise ValueError(f'{path}: incomplete session')
            producer = r['producer']
            width = r['cores']
            arm = producer, engine, width
            if arm not in verified_arms:
                for name in cases:
                    key = producer, engine, name
                    run_key = (*key, width) if (*key, width) in seen else (*key, None)
                    if run_key not in seen:
                        raise ValueError(f'incomplete verification data for {producer}/{engine}/{width}/{name}')
                    verify_worker(meta, check_metadata[run_key], engine, width, path)
                verified_arms.add(arm)
            expected_checks.add((producer, engine))
            sessions.append(r)
        # The first archived campaigns recorded cores and engines in each session.
        cores = meta.get('core_counts', {n for _, n, _, _ in actual})
        engines = meta.get('engines', {e for _, _, e, _ in actual})
        planned = set(product(range(1, meta['rounds'] + 1), cores, engines, meta['inputs']))
        if not actual or actual != planned:
            raise ValueError(f'{path}: incomplete or unexpected producer/reader rounds')
    reference = {n: checks[args.reference, 'python', n] for n in cases}
    status = {k: ('affinity_disagreement' if k in disagreements else
                  compare(v, reference[k[2]], cases[k[2]]['operation'])) for k, v in checks.items()}
    def summarize(selected):
        return cohort_summary(cases, reference, checks, status, disagreements, selected,
                              [s for s in sessions if (s['producer'], s['reader']) in selected],
                              {p: inputs[p] for p, _ in selected}, spec_hash, args.include_open, previous)

    args.output.write_text(json.dumps(summarize(expected_checks), indent=2) + '\n')
    if args.reference_summary:
        selected = {pair for pair in expected_checks if pair[0] == args.reference}
        args.reference_summary.write_text(json.dumps(summarize(selected), indent=2) + '\n')


def cohort_summary(cases, reference, checks, status, disagreements, expected_checks, sessions,
                   inputs, spec_hash, include_open, previous):
    alternate_disagreements = {}
    for producer, engine in sorted(expected_checks):
        if engine in ALTERNATE_READERS and (producer, 'regol') in expected_checks:
            alternate_disagreements[f'{producer}/{engine}'] = [
                name for name in cases if any(
                    checks[producer, engine, name].get(field) != checks[producer, 'regol', name].get(field)
                    for field in EVIDENCE)]
    summary = {'inputs': inputs, 'spec_sha256': spec_hash, 'verification': {}, 'cohorts': {},
               'affinity_disagreements': sorted(disagreements),
               'alternate_reader_disagreements': alternate_disagreements, 'verification_cases': {}}
    for producer, engine in sorted(expected_checks):
        arm = f'{producer}/{engine}'
        summary['verification'][arm] = dict(Counter(status[producer, engine, n] for n in cases))
        summary['verification_cases'][arm] = [
            {'name': n, 'operation': cases[n]['operation'], 'role': cases[n]['role'],
             'reference_count': reference[n].get('count'), 'status': status[producer, engine, n],
             **{key: checks[producer, engine, n][key] for key in ('error', 'exit_status')
                if key in checks[producer, engine, n]}}
            for n in cases]
    for operation in ('iterate', 'tags', 'geometry'):
        pairs = {(p, e) for p, e in expected_checks if operation != 'tags' or e != 'cpp'}
        keep = {n for n, c in cases.items()
                if c['operation'] == operation and c['role'] == 'candidate_selector'
                and reference[n].get('count', 0) > 0
                and all(status[p, e, n] in MATCH for p, e in pairs)}
        if previous is not None:
            requested = set(previous['cohorts'][operation]['cases'])
            if missing := requested - keep:
                raise ValueError(f'{operation} cases failed local eligibility: {sorted(missing)}')
            keep = requested
        samples = defaultdict(dict)
        for r in sessions:
            engine = r['reader']
            producer = r['producer']
            if (producer, engine) not in pairs:
                continue
            total = 0
            for row in r['rows']:
                name = row['name']
                if name in keep:
                    chk = checks[producer, engine, name]
                    if 'error' in row or row.get('count') != chk['count'] or row.get('work_items') != chk.get('work_items', chk.get('items')):
                        raise ValueError(f'timed payload mismatch: {producer}/{engine}/{name}')
                    total += row['setup_ns'] + row['first_execution_ns']
                    if include_open:
                        total += row['open_ns'] + row.get('executor_setup_ns', 0)
            key = f"{producer}/{r['reader']}/{r['cores']}"
            if r['round'] in samples[key]:
                raise ValueError(f'duplicate {key} round {r["round"]}')
            samples[key][r['round']] = total / 1e6
        interval = 'open_prepare_and_scan_ms' if include_open else 'prepare_and_scan_ms'
        summary['cohorts'][operation] = {
            'cases': sorted(keep),
            'excluded_arms': [
                {'arm': f'{p}/{e}', 'reason': 'C++ tag content fingerprint unavailable'}
                for p, e in sorted(expected_checks - pairs)],
            interval: {
                k: {'rounds': v, 'median': median(v.values())} for k, v in sorted(samples.items())}}
    return summary


if __name__ == '__main__':
    main()
