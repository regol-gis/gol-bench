import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from concurrent_readers import history_groups, timed_batch, validate_observation, verified_counts


class ConcurrentReaderTests(unittest.TestCase):
    def test_keeps_complete_city_histories_and_global_tail(self):
        cases = [{'name': name} for name in
                 ('city-a_streets', 'city-a_amenities', 'city-b_streets', 'world_boundaries')]
        self.assertEqual(history_groups(cases, ['city-a', 'city-b']),
                         {'city-a': cases[:2], 'city-b': cases[2:3], 'global': cases[3:]})
        with self.assertRaisesRegex(ValueError, 'noncontiguous'):
            history_groups([cases[0], cases[2], cases[1]], ['city-a', 'city-b'])

    def test_client_slots_overlap_and_keep_each_result(self):
        started = Barrier(2)

        def invoke(cmd, **kwargs):
            started.wait(timeout=3)
            return subprocess.CompletedProcess(cmd, 0)

        with patch('concurrent_readers.subprocess.run', side_effect=invoke):
            wall, results = timed_batch([['a'], ['b'], ['c'], ['d']], 2, {}, 3)
        self.assertEqual([process.args for _, process in results], [['a'], ['b'], ['c'], ['d']])
        self.assertTrue(all(0 < latency < wall for latency, _ in results))

    def test_requires_same_input_width_and_workload(self):
        with TemporaryDirectory() as tmp:
            report = Path(tmp)/'check.jsonl'
            cases = [{'name': 'all', 'query': '*'}]
            report.with_suffix('.metadata.json').write_text(json.dumps(
                {'gol_sha256': 'exact', 'cpu_affinity': [1, 2]}))
            report.with_suffix('.queries.json').write_text(json.dumps(cases))
            report.write_text(json.dumps({'name': 'all', 'stack': 'python-current'}) + '\n')
            verified_counts(report, cases, 'exact', 2, 'python-current')
            with self.assertRaisesRegex(ValueError, 'different GOL or CPU width'):
                verified_counts(report, cases, 'exact', 1, 'python-current')
            with self.assertRaisesRegex(ValueError, 'verification workload differs'):
                verified_counts(report, [{'name': 'all', 'query': 'n'}], 'exact', 2, 'python-current')

    def test_checks_empty_and_nonempty_results_after_timing(self):
        cases = [{'name': name} for name in ('roads', 'empty')]
        rows = [dict(name=name, count=count, work_items=count)
                for name, count in (('roads', 4), ('empty', 0))]
        reference = {row['name']: row.copy() for row in rows}

        def output():
            return subprocess.CompletedProcess(['stub'], 0,
                                               '\n'.join(json.dumps(row) for row in rows).encode())

        validate_observation(output(), cases, reference)
        rows[-1]['count'] = 1
        with self.assertRaisesRegex(ValueError, 'client output differs'):
            validate_observation(output(), cases, reference)
        rows.pop()
        with self.assertRaisesRegex(ValueError, 'incomplete or reordered'):
            validate_observation(output(), cases, reference)


if __name__ == '__main__':
    unittest.main()
