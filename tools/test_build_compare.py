from argparse import Namespace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from build_compare import check_output, group_summary, measure, validate_spec, writer_command
from verify_writer_output import content_payload
from writer_readers import select_outputs


class WriterBenchmarkTests(unittest.TestCase):
    def test_writer_option_and_geo_desk_overwrite_arguments(self):
        self.assertEqual(writer_command('geodesk-current', 'gol', (), 'a', 'b'),
                         ['gol', 'build', 'b', 'a', '-w'])
        self.assertEqual(writer_command('regol-compact', 'gol-build', ('--compact-dictionary',), 'a', 'b'),
                         ['gol-build', '--compact-dictionary', 'a', 'b'])

    def test_incomplete_group_preserves_samples_without_counting_mismatches(self):
        sample = dict(status='completed', verification='matched', wall_ns=234000000,
                      user_s=0.1, system_s=0.02, peak_rss_kib=12345, bytes=1024, allocated_bytes=4096)
        mismatch = dict(sample, verification='mismatch', wall_ns=12000000)
        timeout = dict(status='timeout', verification='unavailable')
        summary = group_summary([sample, mismatch, timeout], 'regol', 4, 3)
        self.assertFalse(summary['comparable'])
        self.assertEqual(summary['completed'], 2)
        self.assertEqual(summary['verified'], 1)
        self.assertEqual(summary['samples'], [sample, mismatch, timeout])
        self.assertEqual(summary['wall_ms_median'], 234)

    def test_group_with_no_match_has_no_median(self):
        result = group_summary([dict(status='failed')], 'regol', 1, 1)
        self.assertEqual(result['verified'], 0)
        self.assertFalse(result['comparable'])
        self.assertNotIn('wall_ms_median', result)

    def test_verification_requires_unique_named_iteration_selectors(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'spec.json'
            path.write_text(json.dumps([dict(name='a', query='a')] * 2))
            with self.assertRaisesRegex(ValueError, 'unique'):
                validate_spec(path)
            path.write_text(json.dumps([dict(name='a', query='a', limit=1)]))
            with self.assertRaisesRegex(ValueError, 'unlimited'):
                validate_spec(path)

    def test_verifier_does_not_overwrite_build_log(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            build_log = directory / 'sample.log'
            build_log.write_text('writer output')
            args = Namespace(python=Path('/bin/python'), verification='identities', timeout=1)

            def verify(command, stdout, stderr, **kwargs):
                stdout.write(b'[]')
                stderr.write(b'verifier details')

            with patch('build_compare.subprocess.run', side_effect=verify):
                check_output(directory / 'file.gol', directory / 'sample.json', args, [0], directory / 'spec.json')
            self.assertEqual(build_log.read_text(), 'writer output')
            self.assertEqual((directory / 'sample.verify.log').read_text(), 'verifier details')

    def test_timeout_records_killed_writer(self):
        with tempfile.TemporaryDirectory() as scratch:
            command = [sys.executable, '-c', 'import time; time.sleep(2)']
            sample = measure(command, [min(os.sched_getaffinity(0))], Path(scratch) / 'writer.log', 0.05)
            self.assertEqual(sample['status'], 'timeout')
            self.assertLess(sample['exit_code'], 0)

    def test_typed_tags_preserve_numeric_types(self):
        attributes = dict(id=42, is_node=True)
        numeric = Namespace(**attributes, tags=[('lanes', 2)])
        text = Namespace(**attributes, tags=[('lanes', '2')])
        self.assertEqual(content_payload(numeric, 'tags'), content_payload(text, 'tags'))
        self.assertNotEqual(content_payload(numeric, 'typed-tags'), content_payload(text, 'typed-tags'))

    def test_reader_replay_selects_final_verified_gol_at_requested_width(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch)
            first = dict(round=1, bytes=16, sha256='one', status='completed', verification='matched')
            final = dict(first, round=2, sha256='two')
            successful = dict(producer='regol', cores=4, comparable=True, completed=2,
                              verified=2, samples=[first, final])
            mismatch = dict(successful, producer='geodesk', comparable=False, verified=0)
            alternate_width = dict(successful, cores=1)
            groups = [successful, mismatch, alternate_width]
            (path / 'regol-4cpu.gol').touch()
            (path / 'build-summary.json').write_text(json.dumps(dict(schema_version=1,
                dataset='monaco', rounds=2, groups=groups)))
            files, provenance = select_outputs(path, 4)
            self.assertEqual(files, {'regol': path / 'regol-4cpu.gol'})
            self.assertEqual(provenance['included'], {'regol': {'bytes': 16, 'sha256': 'two'}})
            self.assertIn('geodesk', provenance['excluded'])
