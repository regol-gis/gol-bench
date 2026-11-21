"""Compare PBF-to-GOL writers across affinity widths and verify every output."""
from provenance import harness_source
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from benchmark_cache import CachePreparationError, FileCache
from benchmark_producer_corpus import sync_inputs, williams_order
from benchmark_stacks import cpu_model
from datasets import catalogue, verify
from fetch_application_pbf import PROVENANCE, fetch as fetch_uk
from input_identity import identify, unchanged
from paths import ROOT, SDK
from verify_writer_output import CONTENTS


ENGINES = {
    'regol': ('writer', ()),
    'geodesk': ('gol_tool', ()),
    'geodesk-current': ('gol_tool_current', ()),
    'regol-compact': ('writer', ('--compact-dictionary',)),
    'regol-even-zooms': ('writer', ('--even-zooms',)),
}
DEFAULT_SPEC = ROOT / 'benchmarks/fixture.json'


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def source_pbf(dataset, pbf, directory):
    if dataset is None:
        return pbf.resolve(strict=True), None
    if dataset == 'uk':
        path = pbf or directory / 'united-kingdom-261115.osm.pbf'
        if pbf is None and not path.exists():
            fetch_uk(path)
        provenance = json.loads(PROVENANCE.read_bytes())
        return path.resolve(strict=True), dict(bytes=provenance['pbf_bytes'], sha256=provenance['pbf_sha256'])
    item = catalogue()[dataset]
    lock = item['pbf']
    path = pbf or directory / (dataset + '-' + lock['sha256'][:12] + '.osm.pbf')
    if pbf is None and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
            staged = Path(scratch) / path.name
            request = Request(item['pbf_url'], headers={'User-Agent': 'gol-bench/1.0'})
            with urlopen(request, timeout=180) as response, staged.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            verify(staged, lock)
            try:
                os.link(staged, path)
            except FileExistsError:
                # The campaign checks the winning immutable file against its lock.
                pass
    return path.resolve(strict=True), lock


def writer_command(engine, binary, flags, pbf, gol):
    if engine.startswith('geodesk'):
        return [binary, 'build', gol, pbf, '-w']
    if Path(str(binary)).name == 'regol':
        return [binary, 'build', *flags, pbf, gol]
    return [binary, *flags, pbf, gol]


def measure(command, cpus, log, timeout):
    timed_out = threading.Event()
    with log.open('wb') as stream:
        start = time.monotonic_ns()
        with subprocess.Popen(
            ['taskset', '-c', ','.join(map(str, cpus)), *command],
            stdout=stream, stderr=stream, start_new_session=True,
        ) as child:
            def stop_writer():
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return
                timed_out.set()

            timer = threading.Timer(timeout, stop_writer)
            timer.start()
            try:
                _, status, usage = os.wait4(child.pid, 0)
                elapsed = time.monotonic_ns() - start
                child.returncode = os.waitstatus_to_exitcode(status)
            except BaseException:
                stop_writer()
                raise
            finally:
                timer.cancel()
                timer.join()
    return dict(wall_ns=elapsed, user_s=usage.ru_utime, system_s=usage.ru_stime,
                peak_rss_kib=usage.ru_maxrss, minor_faults=usage.ru_minflt,
                major_faults=usage.ru_majflt, input_blocks=usage.ru_inblock, output_blocks=usage.ru_oublock,
                exit_code=child.returncode,
                status='timeout' if timed_out.is_set() else 'failed' if child.returncode else 'completed')


def check_output(path, dest, args, cpus, spec):
    with dest.open('wb') as output, dest.with_suffix('.verify.log').open('wb') as stderr:
        subprocess.run(['taskset', '-c', str(cpus[0]), args.python.absolute(),
                        ROOT / 'tools/verify_writer_output.py', path, spec, args.verification],
                       stdout=output, stderr=stderr, timeout=args.timeout, check=True)
    return json.loads(dest.read_bytes())


def group_summary(samples, producer, width, rounds):
    completed = [sample for sample in samples if sample['status'] == 'completed']
    verified = [sample for sample in completed if sample['verification'] == 'matched']
    result = dict(producer=producer, cores=width, completed=len(completed), verified=len(verified),
                  comparable=len(verified) == rounds, samples=samples)
    if verified:
        for key in ('user_s', 'system_s', 'peak_rss_kib', 'bytes', 'allocated_bytes'):
            result[key + '_median'] = statistics.median(row[key] for row in verified)
        result['wall_ms_median'] = statistics.median(row['wall_ns'] for row in verified) / 1e6
    return result


def validate_spec(path):
    queries = json.loads(path.read_bytes())
    if not isinstance(queries, list) or not queries:
        raise ValueError('supply a nonempty list of feature-iteration queries')
    for query in queries:
        if (not isinstance(query, dict) or not isinstance(query.get('name'), str)
                or not isinstance(query.get('query'), str) or query.get('operation', 'iterate') != 'iterate'
                or not set(query) <= {'name', 'query', 'operation', 'role', 'bounds'}):
            raise ValueError('writer verification requires named, unlimited GOQL iteration selectors')
    names = [query['name'] for query in queries]
    if len(set(names)) != len(names):
        raise ValueError('writer verification selectors must have unique names')
    return queries


def campaign(args, pbf, lock, spec, cpus, producers):
    source = identify(pbf)
    if lock and (source['stat'][2] != lock['bytes'] or source['sha256'] != lock['sha256']):
        raise ValueError(f'{pbf}: source PBF differs from the dataset lock')
    cache = FileCache(pbf) if args.cache != 'inherit' else None
    if args.cache == 'evict':
        sync_inputs({'pbf': pbf})
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    # Hand the verifier an immutable copy for the entire run.
    verifier_spec = dest / 'verification-spec.json'
    shutil.copyfile(spec, verifier_spec)
    queries = validate_spec(verifier_spec)
    reference = args.reference_gol.resolve(strict=True) if args.reference_gol else None
    reference_info = identify(reference) if reference else None
    reference_kind = 'independent-gol' if reference else 'first-successful-output'
    write_json(dest / 'metadata.json', dict(harness_source=harness_source(),
        schema_version=1, dataset=args.dataset or pbf.name, pbf_bytes=source['stat'][2],
        pbf_sha256=source['sha256'], spec_sha256=identify(verifier_spec)['sha256'],
        verification=args.verification, reference=reference_kind,
        reference_gol_sha256=reference_info['sha256'] if reference_info else None,
        workers={name: dict(sha256=identify(binary)['sha256'], flags=list(flags))
                 for name, (binary, flags) in producers.items()},
        verification_python=str(args.python.absolute()), cpu_widths=args.cores, cpus=cpus,
        machine=platform.uname()._asdict(), cpu_model=cpu_model(), rounds=args.rounds, cache=args.cache,
        cache_observation=cache.metadata if cache else None, schedule='Williams crossover over writer and CPU width',
        interval='launch through writer exit; no additional file synchronization',
        output_device=dest.stat().st_dev, keep_outputs=not args.discard_outputs,
        timeout_s=args.timeout, build_timeout_s=args.build_timeout,
    ))
    expected = check_output(reference, dest / 'reference.json', args, cpus, verifier_spec) if reference else None
    if reference:
        unchanged(reference, reference_info)
    baseline = reference_kind if reference else None
    groups = {(name, width): [] for width in args.cores for name in producers}
    schedule = list(groups)
    with (dest / 'builds.jsonl').open('x') as raw:
        for index in range(args.rounds):
            for position, (producer, width) in enumerate(williams_order(schedule, index)):
                stem = f'{index + 1}-{width}cpu-{producer}'
                binary, flags = producers[producer]
                unchanged(pbf, source)
                with tempfile.TemporaryDirectory(dir=dest) as tmp:
                    gol = Path(tmp) / 'output.gol'
                    command = writer_command(producer, binary, flags, pbf, gol)
                    record = dict(producer=producer, cores=width, cpus=cpus[:width],
                                  round=index + 1, position=position + 1, verification='unavailable',
                                  command=[str(arg) for arg in command])
                    cache_state = 'cold' if args.cache == 'evict' else 'warm'
                    try:
                        preparation = cache.sample(cache_state) if cache else nullcontext()
                        with preparation as observation:
                            record['cache_observation'] = observation
                            record.update(measure(command, cpus[:width], dest / (stem + '.log'), args.build_timeout))
                    except CachePreparationError as error:
                        record.update(status='cache-unavailable', cache_error=str(error), cache_attempts=error.attempts)
                    unchanged(pbf, source)
                    if record['status'] == 'completed':
                        if not gol.is_file() or not gol.stat().st_size:
                            record['status'] = 'missing-output'
                        else:
                            output = identify(gol)
                            record.update(bytes=output['stat'][2], sha256=output['sha256'],
                                          allocated_bytes=gol.stat().st_blocks * 512)
                            try:
                                signatures = check_output(gol, dest / (stem + '.json'), args, cpus, verifier_spec)
                            except (subprocess.SubprocessError, ValueError) as error:
                                record.update(verification='error', verification_error=str(error))
                            else:
                                if expected is None:
                                    expected = signatures
                                    baseline = stem
                                record['verification'] = 'matched' if signatures == expected else 'mismatch'
                                if record['verification'] == 'matched' and not args.discard_outputs:
                                    name = producer if len(args.cores) == 1 else f'{producer}-{width}cpu'
                                    gol.replace(dest / (name + '.gol'))
                    groups[(producer, width)].append(record)
                    print(json.dumps(record), file=raw, flush=True)
    summary = [group_summary(rows, name, width, args.rounds) for (name, width), rows in groups.items()]
    write_json(dest / 'build-summary.json', dict(
        schema_version=1, dataset=args.dataset or pbf.name, rounds=args.rounds, cache=args.cache,
        verification=args.verification, reference=reference_kind, baseline=baseline, groups=summary,
    ))
    write_json(dest / 'verified.json', dict(
        queries=[query['name'] for query in queries], signatures=expected, reference=reference_kind,
        baseline=baseline, all_matched=all(group['comparable'] for group in summary),
    ))
    return 0 if all(group['comparable'] for group in summary) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pbf', type=Path, nargs='?', help='PBF to build, or local PBF matching --dataset')
    parser.add_argument('--dataset', choices=[name for name, item in catalogue().items() if 'pbf' in item] + ['uk'],
                        help='check a named source PBF against the catalog, fetching it if needed')
    parser.add_argument('--directory', type=Path, default=ROOT / '.reference/datasets', help='download directory')
    default_writer = ROOT / 'target/release/regol'
    if not default_writer.is_file() and (ROOT / 'target/release/gol-build').is_file():
        default_writer = ROOT / 'target/release/gol-build'
    parser.add_argument('--writer', type=Path, default=default_writer)
    parser.add_argument('--gol-tool', type=Path, default=SDK / 'gol-legacy/gol')
    parser.add_argument('--gol-tool-current', type=Path, default=SDK / 'gol-current/gol')
    parser.add_argument('--python', type=Path, default=SDK / 'python-current313/bin/python')
    parser.add_argument('--engines', nargs='+', choices=ENGINES, default=['geodesk', 'regol'])
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--cores', type=int, nargs='+', default=[1], help='CPU affinity widths')
    parser.add_argument('--cache', choices=['inherit', 'warm', 'evict'], default='inherit',
                        help='verify the source PBF is mapped and warm, evict its pages, or inherit cache state')
    parser.add_argument('--timeout', type=int, default=600, help='seconds per output verification')
    parser.add_argument('--build-timeout', type=int, default=3600, help='seconds per writer invocation')
    parser.add_argument('--spec', type=Path, default=DEFAULT_SPEC,
                        help='named GOQL iteration selectors; default checks all feature identities by type')
    parser.add_argument('--verification', choices=CONTENTS, default='identities',
                        help='compare identities, text or typed tags, or exact normalized geometries')
    parser.add_argument('--reference-gol', type=Path, help='use these feature signatures as the expected result')
    parser.add_argument('--discard-outputs', action='store_true', help='retain evidence without keeping the last verified GOL per arm')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cpus = sorted(os.sched_getaffinity(0))
    if not args.pbf and args.dataset is None:
        parser.error('provide a PBF path or --dataset')
    if any(not 0 < width <= len(cpus) for width in args.cores) or min(args.rounds, args.timeout, args.build_timeout) < 1:
        parser.error('positive rounds, timeouts and available core counts required')
    if len(set(args.engines)) != len(args.engines) or len(set(args.cores)) != len(args.cores):
        parser.error('engine names and core widths must be unique')
    producers = {name: (getattr(args, ENGINES[name][0]).resolve(strict=True), ENGINES[name][1])
                 for name in args.engines}
    validate_spec(args.spec)
    pbf, lock = source_pbf(args.dataset, args.pbf, args.directory)
    return campaign(args, pbf, lock, args.spec, cpus[:max(args.cores)], producers)


if __name__ == '__main__':
    sys.exit(main())
