import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from charts import Figure, compared_scales, application_figures, audit_figures, render, scale_figures, writer_figures
from summarize_count_benchmarks import report_rows, summarize


class ComparisonTests(unittest.TestCase):
    def test_incomplete_scale_never_charts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'run.json').write_text(json.dumps({'status':'incomplete'}))
            (root/'index.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                render(root)
            self.assertFalse((root/'charts').exists())

    def test_writer_failure_is_reported_and_never_becomes_a_zero(self):
        bad = dict(producer='bad', cores=1, comparable=False, completed=3, verified=0, samples=[])
        good = dict(producer='ok', cores=1, comparable=True, completed=1, verified=1, samples=[
            dict(wall_ns=2000000000, user_s=.5, system_s=.25, peak_rss_kib=1024, bytes=2048)])
        figures, notes = writer_figures({'dataset':'region','groups':[good,bad]})
        self.assertEqual(figures[0].values, {('1 CPU(s)','ok'):(2.0,)})
        self.assertEqual(figures[1].values[('1 CPU(s)','ok')],(.75,))
        self.assertIn('bad',notes[0])

    def test_applications_preserve_round_and_measurement_boundaries(self):
        result = dict(complete=True, digest='hash', signature=[1],runs=[[{'elapsed_ns':100},{'elapsed_ns':200}]])
        figures, _ = application_figures({'samples':1,'cpu':'example','affinity':[1],
            'results':{'native/case/rust-cli':result,'native/case/rust-reader':result}})
        self.assertEqual(len(figures),2)
        self.assertEqual(figures[0].values[('case','rust-cli')],(.0003,))

    def test_scalar_chart_uses_same_cases_and_separate_process_boundaries(self):
        def row(stack,name,ms,**extra):
            return dict(stack=stack, name=name, affinity='one', median_ms=ms, validation='scalar_match',**extra)
        data = dict(consumer='scalar_count',audit=[row('python-current','a',1),row('python-current','b',80),
            row('cpp','a',3),row('regol','a',21,timing_boundary='cli'),
            dict(row('cpp','b',900),validation='scalar_mismatch')])
        figures, notes = audit_figures(data)
        self.assertEqual(figures[0].values,{('Matched cases','python-current'):(1,),('Matched cases','cpp'):(3,)})
        self.assertEqual(len(figures),2)
        self.assertIn('scalar_mismatch',notes[0])

    def test_svg_and_csv_keep_real_rounds_and_input_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample = [1.0, 8.0, 3.0]
            (root/'index.json').write_text(json.dumps({'tiny':dict(cache='warm',cases=4,
                prepare_and_scan={'regol/1':{'rounds_ms':sample}})}))
            (root/'run.json').write_text(json.dumps(dict(status='complete', datasets=['tiny'], completed=['tiny'],
                cores=[1], engines=['regol'],rounds=3, cache='warm')))
            report = render(root)
            self.assertTrue(report.is_file())
            self.assertIn('Reader histories',(report.parent/'comparison-01.svg').read_text())
            with (report.parent/'measurements.csv').open() as stream:
                self.assertEqual([float(r['value']) for r in csv.DictReader(stream)], sample)
            with self.assertRaises(FileExistsError):
                render(root)

    def test_cross_host_overlay_refuses_changed_input_and_cache_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b = [Path(tmp)/name for name in ('a','b')]
            for run in (a,b):
                (run/'tiny').mkdir(parents=True)
                (run/'run.json').write_text(json.dumps(dict(status='complete',datasets=['tiny'],completed=['tiny'],
                    cores=[1],engines=['regol'],rounds=1,cache='warm',revision='test')))
                index = {'tiny':dict(cases=1,cache='warm',gol_sha256='abc',prepare_and_scan={'regol/1':{'rounds_ms':[3]}})}
                (run/'index.json').write_text(json.dumps(index))
                meta = dict(cpu_model='test',cases=['test'],spec_sha256='123',file_cache='warm',machine={'node':run.name})
                with gzip.open(run/'tiny/timings.jsonl.gz','wt') as out:
                    out.write(json.dumps(meta))
            self.assertEqual(len(compared_scales(a,b)[1]),1)
            with gzip.open(b/'tiny/timings.jsonl.gz','wt') as out:
                out.write(json.dumps(dict(meta,warm_method='different')))
            with self.assertRaisesRegex(ValueError,'warm_method'):
                compared_scales(a,b)
            index['tiny']['gol_sha256']='def'
            (b/'index.json').write_text(json.dumps(index))
            with self.assertRaisesRegex(ValueError,'GOL'):
                compared_scales(a,b)

    def test_missing_and_invalid_samples_are_rejected(self):
        for values in ([],[float('nan')],[-1]):
            with self.assertRaises(ValueError):
                Figure('test','ms').add('x','y',values)


class ScalarJoinTests(unittest.TestCase):
    def test_duplicate_measurements_fail_and_different_cpu_sets_do_not_match(self):
        row=dict(stack='python-current',name='ways',count=1,affinity='all',cpus=[0,1],samples_wall_ns=[2])
        with self.assertRaisesRegex(ValueError,'duplicate'):
            summarize([row,row])
        results=summarize([row,dict(row,stack='cpp',cpus=[2,3])])['audit']
        self.assertEqual(results[-1]['validation'],'reference_unavailable')
        results=summarize([row,dict(row,stack='regol',timing_boundary='cli')])['audit']
        self.assertNotIn('python_over_stack',results[-1])
        nulls = summarize([dict(row, fingerprint=None), dict(row,stack='php-ffi',fingerprint=None)])['audit']
        self.assertEqual(nulls[-1]['validation'],'scalar_match')
        self.assertNotIn('identity_match',nulls[-1])

    def test_name_alone_cannot_join_other_selectors(self):
        with tempfile.TemporaryDirectory() as temp:
            paths=[]
            for i,query in enumerate(('w','n')):
                path=Path(temp)/f'{i}.jsonl'
                path.write_text(json.dumps(dict(name='features',query=query,cpus=[0])))
                path.with_suffix('.metadata.json').write_text('{"consumer":"scalar_count","binary_sha256":{}}')
                paths.append(path)
            with self.assertRaisesRegex(ValueError,'incompatible scalar'):
                report_rows(paths)
