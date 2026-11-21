import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from selectivity import collect, viewports


class SelectivityTests(unittest.TestCase):
    def test_selector_and_box_axes_are_crossed_at_a_fixed_centre(self):
        cases, descriptions = viewports((7.42, 43.73), [200, 50])
        self.assertEqual(len(cases), 8)
        self.assertEqual([c['query'] for c in cases[:4]], [c['query'] for c in cases[4:]])
        self.assertEqual(len({tuple(c['bbox_lonlat']) for c in cases[:4]}), 1)
        a, b = cases[0]['bbox_lonlat'], cases[4]['bbox_lonlat']
        self.assertTrue(b[0] < a[0] < a[2] < b[2] and b[1] < a[1] < a[3] < b[3])
        self.assertAlmostEqual(descriptions['200m_all_nodes']['area_km2'], .04, places=6)

    def test_retains_zero_hits_and_counts_against_same_box_baseline(self):
        cases, descriptions = viewports((7.42, 43.73), [50, 200])
        counts = [0, 0, 0, 0, 100, 0, 2, 10]
        rows = [dict(name=case['name'], count=count, setup_ns=100, first_execution_ns=200)
                for case, count in zip(cases, counts, strict=True)]
        run = dict(reader='regol', cores=1, round=1, rows=rows)
        with TemporaryDirectory() as tmp:
            raw = Path(tmp)/'first.jsonl'
            raw.write_text('{}\n' + json.dumps(run) + '\n')
            _, observations = collect(raw, descriptions)
            result = {case['name']: case for case in observations}
            self.assertIsNone(result['50m_cafes']['hit_ratio'])
            self.assertEqual(result['200m_crossings']['hit_ratio'], 0)
            self.assertEqual(result['200m_cafes']['hit_ratio'], .02)
            self.assertEqual(result['200m_cafes']['arms']['regol/1']['rounds'][0]['wall_ns'], 300)
            rows[6]['count'] = 101
            raw.write_text('{}\n' + json.dumps(run) + '\n')
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                collect(raw, descriptions)


if __name__ == '__main__':
    unittest.main()
