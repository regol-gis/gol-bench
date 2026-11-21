from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_tag_sidecars import invoke, observations, parse_cases, ready_files


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.cases = [{'name': 'cafes', 'query': 'n[amenity=cafe]', 'operation': 'tags'}]
        self.rows = [{'open_ns': 10},
                     {'name': 'cafes', 'count': 1, 'items': 4, 'payload_errors': 0, 'plan': 'direct'},
                     {'faults': [0, 0, 0]}]

    def test_spec_rejects_silently_ignored_bounds_limits_and_operations(self):
        parse_cases(json.dumps(self.cases))
        for change in ({'operation': 'count'}, {'limit': 1}, {'bbox_lonlat': [1, 2, 3, 4]}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'projected bounds'):
                parse_cases(json.dumps([self.cases[0] | change]))

    def test_requires_payload_and_same_timed_plan(self):
        reference = observations(self.rows, self.cases, 'exact', verification=True)
        timed = [self.rows[0], self.rows[1] | {'elapsed_ns': 30, 'operation': 'tags'}, self.rows[-1]]
        self.assertEqual(observations(timed, self.cases, 'exact', False, reference), reference)
        with self.assertRaisesRegex(ValueError, 'timed plan differs'):
            observations([timed[0], timed[1] | {'plan': 'gol'}, timed[2]], self.cases, 'exact', False, reference)
        with self.assertRaisesRegex(ValueError, 'count or consumption differs'):
            observations([timed[0], timed[1] | {'items': 5}, timed[2]], self.cases, 'exact', False, reference)
        with self.assertRaisesRegex(ValueError, 'payload errors'):
            observations([self.rows[0], self.rows[1] | {'payload_errors': 1}, self.rows[2]],
                         self.cases, 'exact', verification=True)

    def test_joint_residency_is_checked_after_both_files_prepare(self):
        gol, sidecar = Path('input.gol'), Path('index')
        events = []

        def cache(path, resident):
            def prepare(state):
                events.append(('prepare', path, state))
                return nullcontext()

            def snapshot():
                events.append(('snapshot', path))
                return {'file_bytes': 4096, 'resident_bytes': resident * 4096,
                        'total_pages': 1, 'resident_pages': resident}

            return SimpleNamespace(faulted_mapping=lambda: prepare("warm"), mapping_snapshot=lambda _:snapshot(),
                                   sample=prepare,snapshot=snapshot)

        with self.assertRaisesRegex(ValueError, 'expected 1'):
            with ready_files([gol, sidecar], {gol: cache(gol, 0), sidecar: cache(sidecar, 1)}, 'warm'):
                self.fail('worker started without joint residency')
        self.assertEqual(events, [('prepare', gol, 'warm'), ('prepare', sidecar, 'warm'), ('snapshot', gol)])

    def test_legacy_control_worker_has_explicit_rebuild_error(self):
        cfg = SimpleNamespace(cpu=1, worker=Path('worker'), gol=Path('input.gol'), timeout=10)
        proc = subprocess.CompletedProcess([], 1, b'', b'Error: expected build GOL or run|verify GOL')
        with patch('benchmark_tag_sidecars.subprocess.run', return_value=proc):
            with self.assertRaisesRegex(ValueError, 'does not support verify-leaves.*setup sidecars'):
                invoke(cfg, 'verify-leaves', Path('sidecar'), Path('spec'))


if __name__ == '__main__':
    unittest.main()
