#!/usr/bin/env python3
"""Replay a corpus once per process in a balanced crossover order."""
from provenance import harness_source
import argparse
from contextlib import nullcontext
import gzip
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import tempfile
import time

from input_identity import identify, unchanged
from benchmark_cache import FileCache, parse_snapshot
from benchmark_stacks import REF, ROOT, WORKER, cpu_model, resolve, tsv


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def williams_order(arms, round_index):
    """Return one Williams square row, with reversed rows for odd arm counts."""
    n = len(arms)
    block, offset = divmod(round_index, n)
    row = [arms[offset]]
    for position in range(1, n):
        distance = (position + 1) // 2
        index = offset + distance if position % 2 else offset - distance
        row.append(arms[index % n])
    return row[::-1] if n % 2 and block % 2 else row


def sync_inputs(inputs):
    # Flush only the benchmark files before relying on DONTNEED.
    for path in inputs.values():
        with path.open('rb') as stream:
            os.fsync(stream.fileno())


def evict_inputs(inputs, page_size):
    for attempt in range(FileCache.MAX_ATTEMPTS):
        for path in inputs.values():
            with path.open('rb') as stream:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        proc = subprocess.run(
            ['fincore', '--json', '--bytes', '--output', 'FILE,SIZE,PAGES,RES', *inputs.values()],
            capture_output=True, text=True, check=True,
        )
        if proc.stderr:
            raise ValueError('fincore emitted diagnostics: ' + proc.stderr)
        records = json.loads(proc.stdout)['fincore']
        observed = {Path(row['file']): row for row in records}
        if len(records) != len(inputs) or set(observed) != set(inputs.values()):
            raise ValueError('fincore did not report all benchmark files')
        resident = {
            label: parse_snapshot({'fincore': [observed[path]]}, path, path.stat().st_size,
                                  page_size)['resident_bytes']
            for label, path in inputs.items()
        }
        if all(count <= (page_size if inputs[k].stat().st_size >= 100 * page_size else 0)
               for k, count in resident.items()):
            return resident
        if attempt + 1 < FileCache.MAX_ATTEMPTS:
            time.sleep(FileCache.RETRY_SECONDS)
    raise ValueError(f'benchmark input residency exceeds one OS page ({page_size} bytes): {resident}')


def worker_usage():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return dict(minor_faults=usage.ru_minflt, major_faults=usage.ru_majflt,
                block_inputs=usage.ru_inblock, user_cpu_s=usage.ru_utime,
                system_cpu_s=usage.ru_stime)


def worker_path(engine, args):
    if engine == 'pooled':
        return getattr(args, 'pooled_worker', None) or args.regol
    return getattr(args, engine)


def command(engine, args, path, spec, tab, workers):
    if engine in ('regol', 'baseline', 'pooled'):
        override = getattr(args, 'rust_workers', None)
        threads = override if override is not None else (workers if workers > 1 else 0)
        if engine == 'baseline' and getattr(args, 'baseline_workers', None) is not None:
            threads = args.baseline_workers
        if engine == 'pooled':
            threads = args.pool_workers
        return [worker_path(engine, args), path, spec, 'mmap-immutable', '0', '0', '-', '0',
                'skip', str(threads)]
    if engine == 'python':
        return [args.python, ROOT / 'tools/query_benchmark_python.py', path, spec,
                '--first-pass', '--samples', '0', '--target-ms', '0']
    if engine == 'cpp':
        return [args.cpp, path, tab, '0', '0']
    return ['java', '-Xms128m', '-Xmx2g', '-cp', args.java_classpath,
            'CorpusJava', path, tab, '0', '0']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', action='append', metavar='LABEL=PATH', required=True)
    parser.add_argument('--input-lock', type=Path, help='validate the catalogue during the digest pass')
    parser.add_argument('--spec', type=Path, default=ROOT / 'benchmarks/uk-corpus-queries.json')
    parser.add_argument('--engines', nargs='+', choices=['regol', 'baseline', 'pooled', 'cpp', 'python', 'java'],
                        default=['regol', 'cpp', 'python', 'java'])
    parser.add_argument('--regol', type=Path, default=WORKER)
    parser.add_argument('--pooled-worker', type=Path, help='alternate executable for the pooled Rust arm')
    parser.add_argument('--baseline', type=Path, help='earlier regol worker for interleaved A/B measurements')
    parser.add_argument('--cpp', type=Path, default=REF / 'cpp-worker')
    parser.add_argument('--python', type=Path, default=REF / 'python-current313/bin/python')
    parser.add_argument('--java-classpath', default=f'{REF}/classes:{REF}/java-current/*')
    parser.add_argument('--cores', type=int, nargs='+', default=[1, 8])
    parser.add_argument('--rust-workers', type=int,
                        help='Fix the Rust executor width independently of CPU affinity (0 uses the ordered iterator).')
    parser.add_argument('--baseline-workers', type=int, help='override the executor width for the baseline Rust worker')
    parser.add_argument('--pool-workers', type=int, default=32,
                        help='Worker count for the separate pooled Rust arm, from the same executable.')
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--timeout',type=int,default=300,help='seconds per worker session')
    parser.add_argument('--file-cache', choices=['inherit', 'evict', 'warm'], default='inherit',
                        help='evict verifies private input eviction; warm pre-reads and verifies the measured file before every process.')
    parser.add_argument('--output', type=Path, required=True, help='new gzip JSON Lines file')
    args = parser.parse_args()
    if args.baseline_workers is not None and (args.baseline_workers < 0 or 'baseline' not in args.engines):
        parser.error('--baseline-workers needs a nonnegative width and the baseline engine')
    if args.pool_workers <= 0:
        parser.error('--pool-workers must be positive')
    if 'baseline' in args.engines and args.baseline is None:
        parser.error('the baseline engine requires --baseline /path/to/worker')
    if args.rust_workers is not None and args.rust_workers < 0:
        parser.error('--rust-workers requires a nonnegative count')
    inputs = {}
    for value in args.library:
        label, sep, path = value.partition('=')
        if not (sep and label and path) or label in inputs:
            parser.error('each --library needs a unique LABEL=PATH')
        inputs[label] = Path(path).resolve(strict=True)
    cpus = sorted(os.sched_getaffinity(0))
    if args.rounds <= 0 or any(n < 1 or n > len(cpus) for n in args.cores):
        parser.error('positive rounds and sufficient CPUs in the affinity mask are required')
    if len(set(args.cores)) != len(args.cores) or len(set(args.engines)) != len(args.engines):
        parser.error('duplicate CPU counts or engines')
    warm_caches = {k: FileCache(v) for k, v in inputs.items()} if args.file_cache == 'warm' else {}
    if args.file_cache == 'evict':
        identities = set()
        for path in inputs.values():
            cache = FileCache(path)
            file_id = cache.identity[:2]
            if file_id in identities:
                parser.error('eviction requires a separate private file for every producer')
            identities.add(file_id)
    spec_bytes = args.spec.read_bytes()
    data = json.loads(spec_bytes)
    cases = resolve(data if isinstance(data, list) else data['proposed_kernel_cases'])
    names = [case['name'] for case in cases]
    if not names or len(names) != len(set(names)):
        parser.error('a nonempty corpus with unique case names is required')

    env = {k: v for k, v in os.environ.items() if not k.startswith('GOL_READER_BENCH_')}
    env['GOL_READER_BENCH_AUTO_INDEX_BYTES'] = '0'
    libraries = [str(REF / 'geos-install/lib')]
    if env.get('LD_LIBRARY_PATH'):
        libraries.append(env['LD_LIBRARY_PATH'])
    env['LD_LIBRARY_PATH'] = os.pathsep.join(libraries)
    with args.output.open('xb') as raw, gzip.open(raw, 'wt') as output, tempfile.TemporaryDirectory() as temp:
        spec = Path(temp) / 'corpus.json'
        tab = spec.with_suffix('.tsv')
        spec.write_text(json.dumps(cases))
        tab.write_text(tsv(cases))
        page_size = os.sysconf('SC_PAGE_SIZE')
        if args.input_lock:
            from datasets import verify
            locks = json.loads(args.input_lock.read_bytes())
            if set(locks) != set(inputs):
                parser.error('input lock must describe the selected libraries exactly')
            file_ids = {key: identify(path, lambda p: verify(p, locks[key])) for key, path in inputs.items()}
        else:
            file_ids = {key: identify(path) for key, path in inputs.items()}
        arms = list(product(args.cores, args.engines, inputs))
        crossover_cycle = len(arms) * (2 if len(arms) > 1 and len(arms) % 2 else 1)
        meta = {'file_identities':file_ids,'format': 1, 'spec_sha256': hashlib.sha256(spec_bytes).hexdigest(), 'cases': names,
                'inputs': {k: {'bytes': i['stat'][2], 'sha256': i['sha256']} for k,i in file_ids.items()},
                'cpu_affinity': cpus, 'load_average_at_start':os.getloadavg(),
                'cgroup':{name:(Path('/sys/fs/cgroup')/name).read_text().strip() for name in
                          ('cpu.max','memory.max','memory.swap.max') if (Path('/sys/fs/cgroup')/name).is_file()},
                'host_label': os.getenv('GOL_BENCH_HOST') or os.getenv('RUNNER_NAME') or platform.node(),
                'machine': platform.uname()._asdict(), 'rounds': args.rounds,
                'engines': args.engines, 'core_counts': args.cores,
                'baseline_workers': args.baseline_workers, 'rust_workers': args.rust_workers, 'pooled_rust_workers': args.pool_workers if 'pooled' in args.engines else None,
                'benchmark_revision':subprocess.check_output(['git','-C',ROOT,'rev-parse','HEAD'],text=True).strip(),
                'cpu_model':cpu_model(),
                'warm_method': 'fault every mapped page, verify with mincore and hold mapping through worker' if warm_caches else None,
                'ordering': 'williams', 'crossover_cycle_rounds': crossover_cycle,
                'input_digest_preread': True, 'file_cache': args.file_cache, 'page_size': page_size,
                'worker_sha256': {k: digest(worker_path(k, args)) for k in args.engines if k != 'java'},
                'java_classpath': args.java_classpath if 'java' in args.engines else None}
        meta["harness_source"] = harness_source()
        if 'java' in args.engines:
            meta['java_sha256'] = {}
            for entry in args.java_classpath.split(os.pathsep):
                p = Path(entry)
                if p.name == '*':
                    files = sorted(p.parent.glob('*.jar'))
                else:
                    files = [p / 'CorpusJava.class' if p.is_dir() else p]
                meta['java_sha256'].update({p.name: digest(p) for p in files})
        if 'python' in args.engines:
            extension = subprocess.check_output(
                [args.python, '-c', 'from geodesk import _geodesk;print(_geodesk.__file__)'], text=True).strip()
            meta['python_geodesk_sha256'] = digest(Path(extension))
        output.write(json.dumps(meta) + '\n')
        if args.file_cache == 'evict':
            sync_inputs(inputs)
        for rep in range(args.rounds):
            for position, (n, engine, label) in enumerate(williams_order(arms, rep), start=1):
                cmd = command(engine, args, inputs[label], spec, tab, n)
                affinity = ','.join(map(str, cpus[:n]))
                resident = evict_inputs(inputs, page_size) if args.file_cache == 'evict' else None
                context = warm_caches[label].warm_mapping() if warm_caches else nullcontext(None)
                with context as warm:
                    if warm is not None:
                        resident = {label: warm['resident_bytes']}
                    load_before=os.getloadavg()
                    before = worker_usage()
                    started = time.monotonic_ns()
                    run = subprocess.run(['taskset', '-c', affinity, *cmd], env=env,
                                         capture_output=True, timeout=args.timeout)
                    wall_ns = time.monotonic_ns() - started
                    usage = {name: count - before[name] for name, count in worker_usage().items()}
                if run.returncode:
                    raise RuntimeError(f'{engine}/{label}: {run.stderr[-1500:].decode(errors="replace")}')
                rows = [json.loads(line) for line in run.stdout.splitlines()]
                if [r['name'] for r in rows] != names:
                    raise ValueError(f'incomplete or reordered output from {engine}/{label}')
                for row in rows:
                    if 'error' not in row and not {'count', 'work_items', 'first_execution_ns', 'setup_ns'} <= row.keys():
                        raise ValueError(f'{engine} worker lacks first-pass support: {row["name"]}')
                    if engine in ('regol','baseline','pooled') and row.get('retained_index_bytes') != 0:
                        raise ValueError('unexpected retained index')
                observation = dict(round=rep + 1, position=position, cores=n, reader=engine,
                                   producer=label, resident_bytes_before=resident, worker_usage=usage, load_average_before=load_before,
                                   process_wall_ns=wall_ns,
                                   rows=rows)
                output.write(json.dumps(observation, separators=(',', ':')) + '\n')
                output.flush()
                print(f'round {rep + 1}   position {position}   {n} core(s)   {engine}   {label}', flush=True)
        for label,path in inputs.items():
            unchanged(path,file_ids[label])


if __name__ == '__main__':
    main()
