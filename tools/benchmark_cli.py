"""Compare fresh CLI counts and exports on identical selections and CPU budgets."""
from contextlib import nullcontext
from itertools import product
import argparse
import csv
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile

from application_export import export_signature, record_hash, write_cli_output
from application_reference import file_digest
from benchmark_producer_corpus import williams_order
from benchmark_cache import FileCache
from input_identity import identify, unchanged
from paths import ROOT, SDK
from provenance import harness_source


FORMATS = ('count', 'csv', 'geojson', 'geojsonl', 'xml', 'pbf')


def features(rows):
    for row in rows:
        if row['type'] != 'Feature' or not isinstance(row['properties'], dict):
            raise ValueError('expected a GeoJSON Feature with properties')
        yield [row['id'], row['geometry'], row['properties']]


def signature(path, fmt, keys):
    if fmt == 'count':
        value = path.read_text().strip()
        if not value.isascii() or not value.isdecimal():
            raise ValueError('expected one nonnegative count')
        return {'selected': int(value)}
    if fmt in ('xml', 'pbf'):
        counts, fingerprint = export_signature(path, allow_empty=True)
        return {'objects': counts, 'fingerprint': fingerprint}
    with path.open(encoding='utf-8', newline='') as stream:
        if fmt == 'csv':
            csv.field_size_limit(sys.maxsize)
            rows = csv.reader(stream, strict=True)
            if next(rows, None) != keys:
                raise ValueError('CSV columns differ from the requested fields')
        elif fmt == 'geojsonl':
            rows = features(json.loads(line) for line in stream)
        else:
            collection = json.load(stream)
            if collection['type'] != 'FeatureCollection' or not isinstance(collection['features'], list):
                raise ValueError('expected a GeoJSON FeatureCollection')
            rows = features(collection['features'])
        count, total = 0, 0
        for row in rows:
            if fmt == 'csv' and len(row) != len(keys):
                raise ValueError('CSV row has the wrong number of columns')
            total += record_hash(row)
            count += 1
        return {'selected': count, 'fingerprint': f'{total % (1 << 256):064x}'}


def compare(rows, samples):
    """Qualify a format only when every round from both CLIs agrees."""
    errors = [row['error'] for row in rows if 'error' in row]
    if errors:
        return '; '.join(errors)
    if len(rows) != 2 or any(len(row['runs']) != samples for row in rows):
        return 'incomplete rounds'
    if rows[0]['signature'] != rows[1]['signature']:
        return 'decoded output differs between CLIs'
    return None


def campaign(args):
    paths = {'geodesk': args.file}
    if args.native_file:
        paths['regol'] = args.native_file
    inputs = {label: identify(path) for label, path in paths.items()}
    warm = {label: FileCache(path) for label, path in paths.items()} if args.native_file else {}
    binaries = {'Regol': args.regol.resolve(strict=True), 'GOL Tool': args.gol_tool.resolve(strict=True)}
    available = sorted(os.sched_getaffinity(0))
    if args.cores > len(available):
        raise ValueError('CPU budget exceeds available affinity')
    cpus = available[:args.cores]
    report = dict(suite='cli-outputs', host=platform.node(), input=inputs['geodesk'], cpus=cpus, samples=args.samples,
                  harness_source=harness_source(), bbox=args.bbox, csv_keys=args.keys, requested_threads=len(cpus),
                  cache='inherited after full input digest', scratch=str(args.scratch.resolve()),
                  timing='fresh process through exit writing stdout to a regular file, no fsync; decoding after timing',
                  binaries={name:{'path':str(p),'sha256':file_digest(p)} for name,p in binaries.items()},
                  results=[])
    if args.native_file:
        report.update(inputs=inputs, cache='warm', generator_results=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    # Affinity is inherited by each child without timing an extra taskset process.
    os.sched_setaffinity(0, cpus)
    try:
        with tempfile.TemporaryDirectory(dir=args.scratch) as scratch:
            for index, query in enumerate(args.query):
                groups = {}
                arms = list(product(binaries, paths))
                for fmt in args.formats:
                    for producer, path in paths.items():
                        group = dict(query=query, format=fmt, arms={})
                        for name, binary in binaries.items():
                            cmd = [str(binary), 'query', str(path.resolve()), query, '-f', fmt,
                                   '--threads', str(len(cpus))]
                            if args.bbox:
                                cmd.append('-b='+args.bbox)
                            if fmt == 'csv':
                                cmd.extend(['--keys', ','.join(args.keys)])
                            group['arms'][name] = dict(command=cmd, runs=[])
                        groups[fmt, producer] = group
                for sample in range(args.samples):
                    for position in williams_order(list(range(len(args.formats))), index+sample):
                        fmt = args.formats[position]
                        for name, producer in williams_order(arms, index+sample+position):
                            row = groups[fmt, producer]['arms'][name]
                            if 'error' in row:
                                continue
                            out = Path(scratch)/('output.'+fmt)
                            try:
                                with warm[producer].warm_mapping() if warm else nullcontext():
                                    elapsed, size = write_cli_output(row['command'], out, timeout=args.timeout)
                                checked = signature(out, fmt, args.keys)
                                if row.setdefault('signature', checked) != checked:
                                    raise ValueError('decoded output changed between rounds')
                                row['runs'].append(dict(wall_ns=elapsed, bytes=size))
                            except (ValueError, RuntimeError, OSError, EOFError, KeyError, TypeError, IndexError, StopIteration,
                                    csv.Error, ET.ParseError, zlib.error, struct.error,
                                    subprocess.TimeoutExpired) as err:
                                row['error'] = str(err)
                            finally:
                                out.unlink(missing_ok=True)
                for group in groups.values():
                    rows = list(group['arms'].values())
                    reason = compare(rows, args.samples)
                    group['verified'] = reason is None
                    if reason:
                        group['reason'] = reason
                for producer in paths:
                    same_file = [groups[fmt, producer] for fmt in args.formats]
                    count = next((g for g in same_file if g['format']=='count' and g['verified']), None)
                    if count:
                        selected = count['arms']['GOL Tool']['signature']['selected']
                        for group in same_file:
                            if not group['verified'] or group['format']=='count':
                                continue
                            sig = group['arms']['GOL Tool']['signature']
                            objects = sum(sig.get('objects', {}).values())
                            if ('selected' in sig and sig['selected'] != selected or
                                    'objects' in sig and (objects < selected or selected == 0 and objects != 0)):
                                group.update(verified=False, reason='output cardinality conflicts with scalar count')
                report['results'].extend(groups[fmt, 'geodesk'] for fmt in args.formats)
                if args.native_file:
                    report['generator_results'].extend(groups[fmt, 'regol'] for fmt in args.formats)
                for (fmt, producer), group in groups.items():
                    detail = group.get('reason') or ', '.join(
                        f"{n} {statistics.median(r['wall_ns'] for r in a['runs'])/1e6:.2f} ms"
                        for n,a in group['arms'].items())
                    print(producer, query, fmt, detail, flush=True)
    finally:
        os.sched_setaffinity(0, available)
    for label, path in paths.items():
        unchanged(path, inputs[label])
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return all(group['verified'] for group in report['results'] + report.get('generator_results', [])) and all(
        base['arms'][engine].get('signature') == native['arms'][engine].get('signature')
        for base, native in zip(report['results'], report.get('generator_results', []))
        for engine in binaries)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('file', type=Path)
    p.add_argument('--native-file', type=Path, help='pair both CLIs on a second file in balanced warm rounds')
    p.add_argument('--query', action='append', required=True, help='GOQL selector, repeat for more workloads')
    p.add_argument('--bbox', help='optional west,south,east,north in degrees, applied to every selector')
    p.add_argument('--formats', choices=FORMATS, nargs='+', default=FORMATS)
    p.add_argument('--keys', default='id,name', help='identical CSV columns; include id')
    p.add_argument('--samples', type=int, default=4)
    p.add_argument('--cores', type=int, default=1)
    p.add_argument('--timeout', type=float, default=120, help='seconds per CLI invocation')
    p.add_argument('--scratch', type=Path, default=Path(tempfile.gettempdir()), help='filesystem for actual export files')
    p.add_argument('--regol', type=Path, default=ROOT/'target/release/regol')
    p.add_argument('--gol-tool', type=Path, default=SDK/'gol-current/gol')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.keys = args.keys.split(',')
    if args.samples < 1 or args.cores < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        p.error('sample, CPU and time budgets must be positive')
    if len(set(args.query)) != len(args.query) or len(set(args.formats)) != len(args.formats):
        p.error('select distinct queries and formats')
    if 'id' not in args.keys or not all(args.keys) or len(set(args.keys)) != len(args.keys):
        p.error('CSV columns must be distinct, nonempty and include id')
    if not campaign(args):
        print("At least one format failed verification. See the report for diagnostics.", file=sys.stderr)
        raise SystemExit(3)


if __name__ == '__main__':
    main()
