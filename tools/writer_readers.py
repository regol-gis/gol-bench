"""Replay reader workloads against the verified GOLs retained by a writer run."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from build_compare import ENGINES as WRITERS
from campaign import ENGINES as READERS
from paths import ROOT, SDK, WORKER


def select_outputs(source, build_cores):
    path = source / 'build-summary.json'
    contents = path.read_bytes()
    build = json.loads(contents)
    if build.get('schema_version') != 1:
        raise ValueError(f'unsupported writer result schema: {path}')
    groups = build['groups']
    widths = {group['cores'] for group in groups}
    if build_cores not in widths:
        raise ValueError(f'writer results do not include {build_cores} CPU(s)')
    files, locks, excluded = {}, {}, {}
    for group in groups:
        if group['cores'] != build_cores:
            continue
        producer = group['producer']
        if producer not in WRITERS or producer in files or producer in excluded:
            raise ValueError(f'unexpected or repeated writer label: {producer}')
        rounds = build['rounds']
        if not group['comparable'] or group['completed'] != rounds or group['verified'] != rounds:
            excluded[producer] = 'incomplete writer group or verification mismatch'
            continue
        final = max(group['samples'], key=lambda sample: sample['round'])
        if final['round'] != rounds or final.get('status') != 'completed' or final.get('verification') != 'matched':
            raise ValueError(f'{producer}: missing final verified sample')
        filename = producer if len(widths) == 1 else f'{producer}-{build_cores}cpu'
        gol = source / (filename + '.gol')
        if not gol.is_file():
            raise ValueError(f'{gol}: retained GOL required. Run writers without --discard-outputs.')
        files[producer] = gol
        locks[producer] = {'bytes': final['bytes'], 'sha256': final['sha256']}
    provenance = dict(
        writer_directory=str(source), writer_summary_sha256=hashlib.sha256(contents).hexdigest(),
        dataset=build['dataset'], build_cores=build_cores, included=locks, excluded=excluded,
    )
    return files, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('writer_run', type=Path, help='writer output directory with retained GOLs')
    parser.add_argument('--build-cores', type=int, default=1, help='select outputs built at this CPU affinity width')
    parser.add_argument('--cores', type=int, nargs='+', default=[1], help='CPU widths for reader processes')
    parser.add_argument('--engines', nargs='+', choices=READERS, default=['regol', 'python'])
    parser.add_argument('--pool-workers', type=int, default=32)
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--timeout', type=int, default=300, help='seconds per timed history or verified case')
    parser.add_argument('--cache', choices=['inherit', 'warm', 'evict'], default='inherit')
    parser.add_argument('--spec', type=Path, default=ROOT / 'benchmarks/fixture.json')
    parser.add_argument('--rust', type=Path, default=WORKER)
    parser.add_argument('--python', type=Path, default=SDK / 'python-current313/bin/python')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        libraries, provenance = select_outputs(args.writer_run.resolve(), args.build_cores)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for producer, reason in provenance['excluded'].items():
        print(f'Excluded {producer}: {reason}', file=sys.stderr)
    if len(libraries) < 2:
        parser.error('downstream comparison requires at least two verified writer outputs at this width')
    dest = args.output.resolve()
    if dest.exists():
        parser.error(f'{dest}: use a new reader result directory')
    with tempfile.TemporaryDirectory() as scratch:
        lock = Path(scratch) / 'writer-output-lock.json'
        lock.write_text(json.dumps(provenance['included']) + '\n')
        command = [sys.executable, ROOT / 'tools/campaign.py', '--input-lock', lock,
                   '--engines', *args.engines, '--cores', *map(str, args.cores),
                   '--pool-workers', str(args.pool_workers), '--rounds', str(args.rounds),
                   '--timeout', str(args.timeout), '--cache', args.cache, '--spec', args.spec,
                   '--rust', args.rust.absolute(), '--python', args.python.absolute(), '--output', dest]
        for label, path in libraries.items():
            command.extend(['--library', f'{label}={path}'])
        completed = subprocess.run(command, check=False)
    if completed.returncode == 0:
        with (dest / 'writer-inputs.json').open('x') as stream:
            json.dump(provenance, stream, indent=2)
            stream.write('\n')
    return completed.returncode


if __name__ == '__main__':
    sys.exit(main())
