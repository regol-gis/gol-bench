import argparse,json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import benchmark_stacks as b

class CorpusTests(unittest.TestCase):
    def test_resolution_and_tsv_preserve_query(self):
        case={'name':'a','query':'n[name="City of Edinburgh"]','bbox_lonlat':[-3.3,55.9,-3.1,55.99]}
        got=b.resolve([case])[0]
        self.assertNotIn('bounds',case)
        self.assertEqual(got['bounds'],[-39370534,807928741,-36984441,809846178])
        self.assertEqual(b.tsv([got]).split('\t')[6],case['query'])
    def test_proposal_unique_references(self):
        d=json.loads((b.ROOT/'benchmarks/query-corpus-research.json').read_text());cases=d['proposed_kernel_cases'];ids={q['id']:q['query'] for q in d['selectors']}
        self.assertEqual(len(cases),524);self.assertEqual(len({c['name'] for c in cases}),524)
        for c in cases:self.assertEqual(c['query'],ids[c['selector_id']])
    def run_fake(self,code,timeout=2):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);args=argparse.Namespace(file=root/'unused.gol',samples=1,target_ms=1,timeout=timeout,affinity='one',output=root/'records.jsonl')
            cases=[{'name':n,'query':'n'} for n in ['a','b']];records=[]
            with patch.object(b,'command',return_value=[sys.executable,'-u','-c',code]):
                b.run_engine('fake',cases,args,[min(b.os.sched_getaffinity(0))],root,records)
            self.assertEqual(len(records),2)
            return records
    def test_buffered_rows_and_explicit_errors(self):
        code='import json; print(json.dumps(dict(name="a",error="unsupported"))); print(json.dumps(dict(name="b",samples_wall_ns=[2,4],count=0)))'
        r=self.run_fake(code);self.assertEqual(r[0]['error'],'unsupported');self.assertEqual(r[1]['median_wall_ns'],3)
    def test_crash_retained_and_remaining_case_attempted(self):
        r=self.run_fake('raise SystemExit(7)');self.assertEqual([x['exit_status'] for x in r],[7,7])
    def test_timeout_kills_worker_and_continues(self):
        r=self.run_fake('import time;time.sleep(10)',timeout=.05);self.assertEqual([x['error'] for x in r],['case_timeout','case_timeout'])

    def test_timeout_after_worker_closes_stdout(self):
        r = self.run_fake('import os, time; os.close(1); time.sleep(10)', timeout=.05)
        self.assertEqual([x['error'] for x in r], ['case_timeout', 'case_timeout'])

class AuditTests(unittest.TestCase):
    def test_mismatch_and_missing_verification_never_pass(self):
        from summarize_stack_benchmarks import compare
        base={'count':1,'fingerprint':{'count':1,'sum':'2','xor':'2'},'work_items':2,
              'work_fingerprint':{'features':1,'items':2,'sum':'3','xor':'3'}}
        self.assertEqual(compare(base,base,'tags'),'content_match')
        changed=dict(base,work_fingerprint={**base['work_fingerprint'], 'sum':'4'})
        self.assertEqual(compare(changed,base,'tags'),'content_mismatch')
        self.assertEqual(compare(dict(base,work_fingerprint=None),base,'tags'),'identities_and_counts_only')
        self.assertEqual(compare(dict(base,error='crashed'),base,'iterate'),'execution_error')
        self.assertEqual(compare(base,{'error':'crashed'},'iterate'),'reference_unavailable')
        self.assertEqual(compare(dict(base,fingerprint={**base['fingerprint'], 'count':2}),base,'iterate'),'identity_mismatch')
        self.assertEqual(compare(dict(base,count=2),base,'iterate'),'identity_count_mismatch')
        self.assertEqual(compare(dict(base,work_items=None),base,'iterate'),'consumption_validation_unavailable')
        self.assertEqual(compare(base,dict(base,fingerprint=None),'iterate'),'identity_validation_unavailable')
    def test_case_identity_keeps_exact_cpu_sets(self):
        from summarize_stack_benchmarks import summarize
        case = dict(name='roads', query='w[highway]', operation='iterate', role='candidate_selector')
        base = dict(name='roads', affinity='all', count=1, work_items=1, samples_wall_ns=[100],
                    fingerprint={'count': 1, 'sum': '1', 'xor': '1'})
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)/'report.jsonl'
            rows = [dict(base, stack='python-current', cpus=[1]),
                    dict(base, stack='cpp', cpus=[2])]
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            result = summarize([path], [case], 'python-current')
            self.assertEqual(result['audit'][1]['cpus'], [2])
            self.assertEqual(result['audit'][1]['validation'], 'reference_unavailable')
            self.assertEqual([ratio['cpus'] for ratio in result['ratios']], [[1]])

    def test_comparison_requires_common_affinity_and_workload(self):
        from summarize_stack_benchmarks import verify_comparison_inputs
        case = dict(name='roads', query='w[highway]', operation='iterate')
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            reports = [path/'python.jsonl', path/'regol.jsonl']
            for report, cpus in zip(reports, ([1, 2], [1, 2, 3, 4]), strict=True):
                report.with_suffix('.metadata.json').write_text(json.dumps(
                    {'cpu_affinity': cpus, 'args': {'affinity': 'all'}}))
                report.with_suffix('.queries.json').write_text(json.dumps([case]))
            with self.assertRaisesRegex(ValueError, 'different cpu_affinity'):
                verify_comparison_inputs(reports, [case])
            with self.assertRaisesRegex(ValueError, 'workload differs'):
                verify_comparison_inputs(reports, [{**case, 'query': 'n'}])

class CountAuditTests(unittest.TestCase):
    def test_scalar_contract_and_identity_evidence_are_separate(self):
        from summarize_count_benchmarks import summarize
        base = dict(name='count', affinity='one', count=2,
                    samples_wall_ns=[10, 20, 30], fingerprint={'count': 2, 'sum': '3'})
        rows = [dict(base, stack='python-current'),
                dict(base, stack='same_count', fingerprint={'count': 2, 'sum': '4'}),
                dict(base, stack='wrong_count', count=1),
                dict(base, stack='crashed', error='worker_exit')]
        audit = {r['stack']: r for r in summarize(rows)['audit']}
        self.assertEqual(audit['same_count']['validation'], 'scalar_match')
        self.assertFalse(audit['same_count']['identity_match'])
        self.assertEqual(audit['wrong_count']['validation'], 'scalar_mismatch')
        self.assertNotIn('python_over_stack', audit['wrong_count'])
        self.assertEqual(audit['crashed']['validation'], 'execution_error')

if __name__=='__main__':unittest.main()
