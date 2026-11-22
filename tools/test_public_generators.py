"""Regression checks for the public same-reader comparison."""
from pathlib import Path
import json
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runner'))
from run_public_bench import rebuild_aggregates
from generators import corpus_rows, paired_row, workflow_pair


class PublicGeneratorTests(unittest.TestCase):
    def test_paired_ratios_are_calculated_before_median(self):
        row = paired_row('q', 'Q', 'java', [1, 10, 100], [2, 8, 5], 1)
        self.assertAlmostEqual(row['saved_pct'], 20)
        self.assertEqual(row['regol_ms'], 5)  # Ratio of medians would incorrectly show 50%.
        with self.assertRaises(ValueError):
            paired_row('q', 'Q', 'java', [1, 2], [1], 1)

    def test_mismatched_workflow_never_publishes_speedup(self):
        def arm(fp):
            return dict(signature=[dict(name='water', fingerprint=fp)], runs=[[dict(elapsed_ns=9)]])
        row = workflow_pair('app', 'App', 'python', arm('a'), arm('b'))
        self.assertEqual(row['differences'], ['water'])
        self.assertIsNone(row['saved_pct'])
        self.assertFalse(row['verified'])

    def test_cli_canonical_content_allows_different_file_sizes(self):
        row = dict(exports={'roads': {'objects': 4, 'fingerprint': 'x'}}, signature=[], runs=[[dict(elapsed_ns=3)]])
        other = dict(row, signature=[{'items': 450}])
        self.assertTrue(workflow_pair('cli', 'Export', 'gol-tool', row, other)['verified'])

    def test_release_history_extends_only_its_original_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'data'
            data.mkdir()
            row = paired_row('uk', 'UK', 'java', [2], [3], 1)
            meta = dict(file='data/base.json', id='v1', commit='c', timestamp='t', short_commit='c', machine_id='m')
            base = dict(generators=[row])
            (data/'base.json').write_text(json.dumps(base))
            meta['release_file'] = 'data/release.json'
            release = dict(run_id='v1', generators=[dict(row, key='cold/java')])
            (data/'release.json').write_text(json.dumps(release))
            rebuild_aggregates(data, dict(runs=[meta]))
            self.assertEqual(json.loads((data/'base.json').read_bytes()), base)
            self.assertEqual(set(json.loads((data/'history.json').read_bytes())['generators']), {'uk/java', 'cold/java'})
            self.assertEqual(len(json.loads((data/'latest.json').read_bytes())['generators']), 2)

    def test_corpus_totals_sum_within_round(self):
        cohorts = {op: dict(cases=[op], prepare_and_scan_ms={'geodesk/regol/1': {'rounds': rounds}})
                   for op, rounds in [('iterate', {'2': 10, '1': 1}), ('tags', {'1': 8, '2': 2}),
                                      ('geometry', {'1': 1, '2': 2})]}
        self.assertEqual(corpus_rows({'cohorts': cohorts}, 3)[-1]['engines']['regol']['rounds_ms'], [10, 14])
