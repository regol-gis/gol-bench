import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datasets import catalogue, prepare, verify
from scale import history, validate


class ScaleTests(unittest.TestCase):
    def test_catalogue_and_city_histories(self):
        datasets = catalogue()
        self.assertEqual(list(datasets), ['tiny','monaco','liechtenstein','bremen','ghana','uk','planet'])
        verify(prepare('tiny',datasets['tiny'],Path('.')),datasets['tiny'])
        planet = history(datasets['planet'])
        self.assertEqual(len(planet),30)
        self.assertEqual(len({q['name'] for q in planet}),len(planet))
        self.assertTrue(all(len(q['bbox_lonlat'])==4 for q in planet))
        world = history(datasets['planet'],True)
        self.assertEqual(world[:-1],planet)
        self.assertNotIn('bbox_lonlat',world[-1])

    def test_existing_input_cannot_substitute_another_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'input.gol'
            p.write_bytes(b'x'*16384)
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                verify(p,catalogue()['tiny'])
            with self.assertRaisesRegex(ValueError,'bytes'):
                verify(p,catalogue()['planet'])

    def test_digest_reuse_detects_in_place_writes_and_file_replacement(self):
        from input_identity import identify, unchanged
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data'
            path.write_bytes(b'abc')
            original=identify(path)
            unchanged(path,original)
            path.write_bytes(b'def')
            # Emulate the runner filesystem where consecutive updates to a
            # small file can receive the same change and modification times.
            with patch('input_identity.stamp',return_value=original['stat']):
                with self.assertRaisesRegex(ValueError,'changed'):
                    unchanged(path,original)
            copy=Path(tmp)/'replacement'
            copy.write_bytes(b'abc')
            copy.replace(path)
            with self.assertRaisesRegex(ValueError,'changed'):
                unchanged(path,original)

    def test_preflight_runs_before_remote_access(self):
        with tempfile.TemporaryDirectory() as tmp, patch('datasets.planet_pin') as remote:
            with self.assertRaisesRegex(OSError,'required'):
                prepare('planet',dict(bytes=2**62,sha256='a'*64),Path(tmp))
            remote.assert_not_called()

    def test_empty_steps_participate_in_history_verification(self):
        case={'name':'empty','query':'n','operation':'iterate'}
        good={'name':'empty','count':0,'work_items':0,'fingerprint':{'count':0,'sum':'0','xor':'0'}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            check=root/'monaco-1.jsonl'
            check.write_text(''.join(json.dumps(dict(good,stack=k))+'\n' for k in ('python-current','rust-mmap')))
            def write_timing(count):
                with gzip.open(root/'timings.jsonl.gz','wt') as dst:
                    print('{}',file=dst)
                    row=dict(good,count=count,setup_ns=100,first_execution_ns=200)
                    print(json.dumps(dict(reader='regol',cores=1,rows=[row])),file=dst)
            write_timing(0)
            self.assertAlmostEqual(validate(root/'timings.jsonl.gz',[check],[case])['regol/1']['median_ms'],0.0003)
            write_timing(1)
            with self.assertRaisesRegex(ValueError,'incorrect timed result'):
                validate(root/'timings.jsonl.gz',[check],[case])
