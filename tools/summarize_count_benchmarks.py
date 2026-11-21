#!/usr/bin/env python3
"""Audit scalar outputs independently of feature-iteration workloads."""
import collections
import json
import math
import pathlib
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]


def summarize(records):
    def case_key(row):
        return (row['affinity'], tuple(row.get('cpus', [])), row['name'])
    anchor = {}
    seen = set()
    for row in records:
        arm = row['stack'], case_key(row)
        if arm in seen:
            raise ValueError(f'duplicate scalar measurement: {arm}')
        seen.add(arm)
        if row['stack'] == 'python-current':
            anchor[case_key(row)] = row

    rows = []
    for r in records:
        ref = anchor.get(case_key(r))
        timed = r.get('samples_wall_ns')
        if 'error' not in r and (type(r.get('count')) is not int or r['count'] < 0
                                  or not timed or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in timed)):
            raise ValueError(f"invalid scalar count or timing for {r['stack']}/{r['name']}")
        status = ('execution_error' if 'error' in r else 'reference_unavailable' if not ref or 'error' in ref
                  else 'scalar_match' if r['count'] == ref['count'] else 'scalar_mismatch')
        row = {k: r[k] for k in ('name', 'stack', 'affinity', 'cpus', 'count', 'error', 'exit_status') if k in r}
        row['timing_boundary'] = r.get('timing_boundary', 'persistent')
        row['validation'] = status
        if timed and 'error' not in r:
            row['median_ms'] = statistics.median(timed) / 1e6
            row['min_ms'] = min(timed) / 1e6
            row['max_ms'] = max(timed) / 1e6
        if ref and isinstance(r.get('fingerprint'), dict) and isinstance(ref.get('fingerprint'), dict):
            row['identity_match'] = r['fingerprint'] == ref['fingerprint']
            row['count_matches_own_identity_digest'] = r['count'] == r['fingerprint']['count']
        if status == 'scalar_match' and row['timing_boundary'] == 'persistent' and row['median_ms'] > 0:
            row['python_over_stack'] = statistics.median(ref['samples_wall_ns']) / statistics.median(r['samples_wall_ns'])
        rows.append(row)
    overview = {}
    for affinity, stack in sorted({(r['affinity'], r['stack']) for r in rows}):
        selected = [r for r in rows if r['affinity'] == affinity and r['stack'] == stack]
        overview[affinity + ':' + stack] = dict(collections.Counter(r['validation'] for r in selected))
    return {'consumer': 'scalar_count', 'reference': 'python-current', 'note': 'CLI includes process startup/open/output; persistent APIs exclude startup/open/preparation. Count equality is the output contract; identity checks additionally recorded where available.', 'overview': overview, 'audit': rows}


def report_rows(paths):
    """Recover the actual selector used, including resolved bounds absent in some SDK rows."""
    records, definitions, hosts = [], {}, set()
    for path in paths:
        metadata = json.loads(path.with_suffix('.metadata.json').read_bytes())
        if machine := metadata.get('machine'):
            hosts.add((machine.get('node'), machine.get('machine'), metadata.get('cpu_model')))
        cli = metadata.get('consumer') == 'scalar_count' and 'binary_sha256' in metadata
        consumer = metadata.get('consumer')
        if consumer is None:
            # Old persistent reports lack the consumer field. Only accept a
            # legacy report when its actual command identifies a scalar adapter.
            inputs = [pathlib.Path(name).name for worker in metadata.get('workers', {}).values() for name in worker['sha256']]
            evidence = {'count-reader', 'count_python.py', 'cpp-count', 'CountNet.dll', 'count-corpus'}
            if not evidence.intersection(inputs):
                raise ValueError(f'{path}: missing scalar worker provenance')
        elif consumer != 'scalar_count':
            raise ValueError(f'{path}: feature workloads cannot supply scalar count timings')
        spec = path.with_suffix('.queries.json')
        cases = {q['name']: q for q in json.loads(spec.read_bytes())} if spec.exists() else {}
        if not cli and not cases:
            raise ValueError(f'{path}: missing resolved scalar query specification')
        for line in path.read_text().splitlines():
            row = json.loads(line)
            query = row if cli else cases[row['name']]
            selector = query['query'], tuple(query.get('bounds') or ()), query.get('operation','iterate')
            if selector[2] != 'iterate' or query.get('limit') is not None:
                raise ValueError(f'{path}: not a scalar count selector: {row["name"]}')
            if 'query' in row and row['query'] != selector[0]:
                raise ValueError(f'{path}: worker reported a different query for {row["name"]}')
            if definitions.setdefault(row['name'],selector) != selector:
                raise ValueError(f'incompatible scalar queries named {row["name"]}')
            if not row.get('cpus'):
                raise ValueError(f'{path}: missing CPU set for {row["name"]}')
            row['timing_boundary'] = 'cli' if cli else 'persistent'
            records.append(row)
    if len(hosts) > 1:
        raise ValueError('scalar reports describe different machines')
    return records


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',nargs='+',type=pathlib.Path)
    parser.add_argument('--output',type=pathlib.Path,required=True)
    args=parser.parse_args()
    from summarize_stack_benchmarks import same_input
    digest=same_input(args.reports)
    records=report_rows(args.reports)
    result=summarize(records)
    result['gol_sha256']=digest
    with args.output.open('x') as out:
        json.dump(result,out,indent=2)
        out.write('\n')
    print(json.dumps(result['overview'], indent=2))
    for stack in dict.fromkeys(r['stack'] for r in result['audit']):
        print(stack, [(r['name'], r['affinity'], round(r.get('median_ms', 0), 2), r['validation'])
                      for r in result['audit'] if r['stack'] == stack and r['name'] in ('count_all', 'count_roads', 'count_buildings', 'count_cafes')])
