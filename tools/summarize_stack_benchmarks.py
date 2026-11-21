#!/usr/bin/env python3
"""Audit every attempted case before computing reader-stack timing comparisons."""
from benchmark_stacks import ROOT, resolve
import argparse,collections,json,pathlib,statistics

def compare(actual, reference, operation):
    if 'error' in actual:
        return 'execution_error'
    if reference is None or 'error' in reference:
        return 'reference_unavailable'
    identity = actual.get('fingerprint')
    ref_identity = reference.get('fingerprint')
    if not all(isinstance(value, dict) and {'count', 'sum', 'xor'} <= value.keys()
               for value in (identity, ref_identity)):
        return 'identity_validation_unavailable'
    if identity != ref_identity:
        return 'identity_mismatch'
    for row, digest in ((actual, identity), (reference, ref_identity)):
        limit = row.get('limit')
        expected = digest['count']
        if limit is not None:
            if not row.get('membership_verified'):
                return 'limited_membership_unavailable'
            expected = min(limit, expected)
        if row.get('count') != expected:
            return 'identity_count_mismatch'
    if actual.get('limit') != reference.get('limit'):
        return 'limit_mismatch'
    actual_items = actual.get('work_items', actual.get('items'))
    ref_items = reference.get('work_items', reference.get('items'))
    if type(actual_items) is not int or type(ref_items) is not int:
        return 'consumption_validation_unavailable'
    if actual_items != ref_items:
        return 'consumption_count_mismatch'
    if operation == 'iterate':
        return 'identities_match'
    content = actual.get('work_fingerprint')
    ref_content = reference.get('work_fingerprint')
    if not all(isinstance(value, dict) and {'features', 'items', 'sum', 'xor'} <= value.keys()
               for value in (content, ref_content)):
        return 'identities_and_counts_only'
    if (content['features'] != actual['count'] or ref_content['features'] != reference['count']
            or content['items'] != actual_items or ref_content['items'] != ref_items):
        return 'content_count_mismatch'
    return 'content_match' if content == ref_content else 'content_mismatch'


def summarize(paths,corpus,reference="python"):
    specs={c['name']:c for c in (corpus if isinstance(corpus,list) else corpus['proposed_kernel_cases'])};records=[]
    for path in paths:
        for line in pathlib.Path(path).read_text().splitlines():
            r=json.loads(line);r['input_report']=str(path);records.append(r)
    groups=collections.defaultdict(dict)
    for r in records:
        key=(r['affinity'],tuple(r.get('cpus', ())),r['name']);stack=r['stack']
        if stack in groups[key]:raise ValueError(f'duplicate result {key} {stack}')
        groups[key][stack]=r
    audits=[];overview={};comparisons=collections.defaultdict(list)
    for (affinity,cpus,name),group in groups.items():
        c=specs[name];ref=group.get(reference)
        for stack,r in group.items():
            status=compare(r,ref,c['operation']);a={'affinity':affinity,'name':name,'stack':stack,'operation':c['operation'],'selector_id':c.get('selector_id',name),'role':c.get('role'),'validation':status,'count':r.get('count')}
            if cpus:
                a['cpus'] = list(cpus)
            if 'error' in r:
                a['error']=r['error'];a['exit_status']=r.get('exit_status')
                if 'stderr' in r:
                    p=pathlib.Path(r['stderr']);a['stderr_tail']=p.read_text(errors='replace')[-2500:] if p.exists() else 'missing log'
            if r.get('samples_wall_ns'):
                med=statistics.median(r['samples_wall_ns']);a['median_ms']=med/1e6
                a['sample_min_ms']=min(r['samples_wall_ns'])/1e6;a['sample_max_ms']=max(r['samples_wall_ns'])/1e6
                first=r.get('first_ns',r.get('first_execution_ns'))
                if first is not None:a['first_request_ms']=(first+r.get('setup_ns',0))/1e6
            eligible = (status in ('identities_match', 'content_match') and c.get('role') == 'candidate_selector'
                        and r['count'] > 0 and a.get('median_ms', 0) > 0 and ref.get('samples_wall_ns')
                        and statistics.median(ref['samples_wall_ns']) > 0)
            if eligible:
                ratio=statistics.median(ref['samples_wall_ns'])/statistics.median(r['samples_wall_ns']);a['python_over_stack']=ratio;comparisons[(affinity,cpus,stack,c['operation'])].append(ratio)
            a['included_in_ratio_summary']=bool(eligible);audits.append(a)
    for affinity,stack in sorted({(r['affinity'],r['stack']) for r in records}):
        rows=[a for a in audits if a['affinity']==affinity and a['stack']==stack]
        overview[affinity+':'+stack]={'attempted':len(rows),'timed':sum('median_ms' in r for r in rows),'validation':dict(collections.Counter(a['validation'] for a in rows)),'successful_empty_cases':sum(r.get('count')==0 and 'median_ms' in r for r in rows)}
    ratios=[]
    for (affinity,cpus,stack,operation),values in sorted(comparisons.items()):
        ratios.append({'affinity':affinity,'cpus':list(cpus),'stack':stack,'operation':operation,'eligible_nonempty_cases':len(values),'median_python_over_stack':statistics.median(values),'stack_faster_cases':sum(v>1 for v in values),'python_faster_cases':sum(v<1 for v in values),'interpretation':'descriptive, unweighted median of per-case ratios; greater than 1 favours this stack; case sets may differ'})
    return {'reference':reference,'reference_note':'Comparison anchor, not an unquestioned correctness oracle','overview':overview,'ratios':ratios,'audit':audits}

def same_input(reports):
    digests={json.loads(path.with_suffix('.metadata.json').read_bytes())['gol_sha256'] for path in reports}
    if len(digests)!=1:
        raise ValueError('the reports describe different GOL inputs')
    return digests.pop()


def verify_comparison_inputs(reports, corpus):
    specs = corpus if isinstance(corpus, list) else corpus['proposed_kernel_cases']
    expected = {c['name']: c for c in resolve(specs)}
    environments = {}
    for path in reports:
        archived = path.with_suffix('.queries.json')
        if archived.exists():
            for case in json.loads(archived.read_bytes()):
                if expected.get(case['name']) != case:
                    raise ValueError(f"{path}: workload differs from corpus for {case['name']}")
        metadata = json.loads(path.with_suffix('.metadata.json').read_bytes())
        affinity = metadata.get('args', {}).get('affinity')
        if affinity is None:
            continue
        for field in ('cpu_affinity', 'machine', 'cpu_model', 'warm_file_preread'):
            value = metadata.get(field)
            if value is not None and environments.setdefault((affinity, field), value) != value:
                raise ValueError(f'{path}: different {field} for {affinity} stack comparison')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',nargs='+',type=pathlib.Path)
    parser.add_argument('--corpus',type=pathlib.Path,default=ROOT/'benchmarks/query-corpus-research.json')
    parser.add_argument('--output',type=pathlib.Path,required=True)
    parser.add_argument('--reference',default='python-current')
    args=parser.parse_args()
    digest=same_input(args.reports)
    corpus = json.loads(args.corpus.read_bytes())
    verify_comparison_inputs(args.reports, corpus)
    report=summarize(args.reports,corpus,args.reference)
    report['gol_sha256']=digest
    with args.output.open('x') as out:
        json.dump(report,out,indent=2)
        out.write('\n')
    print(json.dumps({'overview':report['overview'],'ratios':report['ratios']},indent=2))


if __name__=='__main__':
    main()
