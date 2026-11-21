#!/usr/bin/env python3
"""Compare bounded node selectors over concentric viewports on one locked GOL."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

from campaign import ENGINES
from datasets import catalogue, dataset_path, prepare
from paths import ROOT, SDK, WORKER
from scale import save_json, validate
from summarize_producer_corpus import records

EARTH_RADIUS_M = 6_371_008.8
SELECTORS = {
    'all_nodes': 'n',
    'crossings': 'n[highway=crossing]',
    'cafes': 'n[amenity=cafe]',
    'amenities': 'n[amenity]',
}


def viewports(center, widths):
    lon, lat = center
    cases, descriptions = [], {}
    for width in sorted(widths):
        lat_delta = math.degrees(width / (2 * EARTH_RADIUS_M))
        lon_delta = lat_delta / math.cos(math.radians(lat))
        bounds = [lon-lon_delta, lat-lat_delta, lon+lon_delta, lat+lat_delta]
        w, s, e, n = map(math.radians, bounds)
        if not (-180 <= bounds[0] < bounds[2] <= 180 and -85 <= bounds[1] < bounds[3] <= 85):
            raise ValueError('viewport must stay within the supported projection')
        area = EARTH_RADIUS_M**2 * (e-w) * (math.sin(n)-math.sin(s)) / 1e6
        for selector, query in SELECTORS.items():
            name = f'{width}m_{selector}'
            cases.append(dict(name=name, query=query, operation='iterate',
                              role='candidate_selector', bbox_lonlat=bounds))
            descriptions[name] = dict(name=name, selector=selector, query=query, width_m=width,
                                      area_km2=area, bbox_lonlat=bounds)
    return cases, descriptions


def collect(raw, descriptions):
    """Summarize timings already validated against the independent typed results."""
    runs = iter(records(raw))
    header = next(runs)
    counts = {}
    cases = {name: dict(description, arms={}) for name, description in descriptions.items()}
    for run in runs:
        arm = f"{run['reader']}/{run['cores']}"
        for row in run['rows']:
            name = row['name']
            if counts.setdefault(name, row['count']) != row['count']:
                raise ValueError(f'{name}: selection count changed across arms')
            samples = cases[name]['arms'].setdefault(arm, {'rounds': []})['rounds']
            samples.append(dict(round=run['round'], setup_ns=row['setup_ns'],
                                first_execution_ns=row['first_execution_ns'],
                                wall_ns=row['setup_ns'] + row['first_execution_ns']))
    for case in cases.values():
        candidate = counts[f"{case['width_m']}m_all_nodes"]
        selected = counts[case['name']]
        if selected > candidate:
            raise ValueError(f"{case['name']}: tag selector exceeds its viewport node baseline")
        case.update(candidate_count=candidate, selected_count=selected,
                    hit_ratio=selected/candidate if candidate else None,
                    selected_per_km2=selected/case['area_km2'])
    return header, list(cases.values())


def main():
    items = {name: item for name, item in catalogue().items() if item.get('views') and name != 'planet'}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=items, required=True)
    parser.add_argument('--city', help='catalogue city (defaults to the first listed city)')
    parser.add_argument('--file', type=Path, help='existing copy of the locked GOL')
    parser.add_argument('--directory', type=Path, default=ROOT/'.reference/datasets')
    parser.add_argument('--widths-m', nargs='+', type=int, default=[50, 200, 800, 3200])
    parser.add_argument('--engines', nargs='+', choices=ENGINES, default=['regol', 'python'])
    parser.add_argument('--cores', nargs='+', type=int, default=[1])
    parser.add_argument('--pool-workers', type=int, default=8)
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--cache', choices=['warm', 'evict', 'inherit'], default='warm')
    parser.add_argument('--rust', type=Path, default=WORKER)
    parser.add_argument('--python', type=Path, default=SDK/'python-current313/bin/python')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if min(args.widths_m) < 1 or len(set(args.widths_m)) != len(args.widths_m):
        parser.error('viewport widths must be positive and distinct')
    item = items[args.dataset]
    city = args.city or next(iter(item['views']))
    if city not in item['views']:
        parser.error(f'{args.dataset} cities: {", ".join(item["views"])}')
    center = item['views'][city]
    try:
        cases, descriptions = viewports(center, args.widths_m)
    except ValueError as error:
        parser.error(str(error))
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    source = args.file or dataset_path(args.dataset, item, args.directory)
    if args.file is None and not source.exists():
        source = prepare(args.dataset, item, args.directory)
    source = source.resolve(strict=True)
    lock, spec = dest/'input.lock.json', dest/'queries.json'
    save_json(lock, {args.dataset: item})
    save_json(spec, cases)
    directory = dest/'campaign'
    subprocess.run([
        sys.executable, ROOT/'tools/campaign.py', '--library', f'{args.dataset}={source}',
        '--input-lock', lock, '--spec', spec, '--engines', *args.engines,
        '--cores', *map(str, args.cores), '--pool-workers', str(args.pool_workers),
        '--rounds', str(args.rounds), '--timeout', str(args.timeout), '--cache', args.cache,
        '--rust', args.rust.absolute(), '--python', args.python.absolute(), '--output', directory,
    ], check=True)
    raw = directory/'timings.jsonl.gz'
    checks = [directory/f'{args.dataset}-{width}.jsonl' for width in args.cores]
    if 'pooled' in args.engines:
        checks.extend(directory/f'{args.dataset}-{width}-pooled.jsonl' for width in args.cores)
    validate(raw, checks, cases)  # Includes zero-hit cases omitted from matched corpus totals.
    header, results = collect(raw, descriptions)
    output = dest/'selectivity.json'
    save_json(output, dict(
        format=1, consumer='spatial_selectivity', dataset=args.dataset, city=city,
        center_lonlat=center, gol_sha256=header['inputs'][args.dataset]['sha256'],
        spec_sha256=header['spec_sha256'], harness_source=header['harness_source'], cpu_affinity=header['cpu_affinity'],
        cpu_model=header['cpu_model'], cache=args.cache, rounds=args.rounds,
        core_counts=args.cores, engines=args.engines, pool_workers=header['pooled_rust_workers'],
        crossover_cycle_rounds=header['crossover_cycle_rounds'],
        width_definition='approximate square side length in metres at the catalogue centre',
        candidate_definition='all nodes returned by the broad selector within the same viewport',
        density_definition='selected nodes divided by the full viewport area in square kilometres',
        timing_definition='query preparation plus first execution within a fresh-process ordered history',
        cases=results))
    print(output)


if __name__ == '__main__':
    main()
