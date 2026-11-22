import gzip
import hashlib
import json
import os
from collections import Counter
from itertools import permutations
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark_producer_corpus import command, evict_inputs, sync_inputs, williams_order
from benchmark_stacks import ROOT


class ProducerComparison(unittest.TestCase):
    def test_rust_worker_override_is_independent_of_cpu_budget(self):
        args = SimpleNamespace(regol=Path('worker'), rust_workers=None)
        for cpus, override, expected in ((1, None, '0'), (8, None, '8'), (1, 32, '32'), (8, 0, '0')):
            with self.subTest(cpus=cpus, override=override):
                args.rust_workers = override
                self.assertEqual(command('regol', args, 'file', 'spec', 'tab', cpus)[-1], expected)

    def test_distinct_pooled_policy_can_run_at_the_candidate_width(self):
        args = SimpleNamespace(regol='selective', pooled_worker='demand', rust_workers=32, pool_workers=32)
        for engine, expected in [('regol', 'selective'), ('pooled', 'demand')]:
            invocation = command(engine, args, 'file', 'spec', 'tab', 1)
            self.assertEqual((invocation[0], invocation[-1]), (expected, '32'))

    def test_pooled_arm_uses_the_same_binary_at_a_separate_width(self):
        args = SimpleNamespace(regol='same-worker', rust_workers=0, pool_workers=16)
        serial = command('regol', args, 'file', 'spec', 'tab', 1)
        pooled = command('pooled', args, 'file', 'spec', 'tab', 1)
        self.assertEqual(pooled[:-1], serial[:-1])
        self.assertEqual((serial[-1], pooled[-1]), ('0', '16'))

    def test_baseline_can_use_a_different_executor_width(self):
        args = SimpleNamespace(regol='candidate', baseline='control', rust_workers=1, baseline_workers=32)
        self.assertEqual(command('baseline', args, 'file', 'spec', 'tab', 1)[-1], '32')
        self.assertEqual(command('regol', args, 'file', 'spec', 'tab', 1)[-1], '1')
        args.baseline_workers = None
        self.assertEqual(command('baseline', args, 'file', 'spec', 'tab', 1)[-1], '1')

    def test_williams_crossover_balances_positions_and_predecessors(self):
        for count in (4, 5):
            with self.subTest(arms=count):
                arms = list(range(count))
                repeats = 2 if count % 2 else 1
                rows = [williams_order(arms, i) for i in range(count * repeats)]
                for row in rows:
                    self.assertCountEqual(row, arms)
                for position in zip(*rows, strict=True):
                    self.assertEqual(Counter(position), dict.fromkeys(arms, repeats))
                transitions = Counter(pair for row in rows for pair in zip(row, row[1:]))
                self.assertEqual(transitions, dict.fromkeys(permutations(arms, 2), repeats))
                if repeats == 2:
                    self.assertEqual(rows[count:], [row[::-1] for row in rows[:count]])

    @unittest.skipUnless(shutil.which('fincore'), 'requires util-linux fincore')
    def test_evicts_only_private_fixture_inputs_and_checks_residency(self):
        page_size = os.sysconf('SC_PAGE_SIZE')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as temp:
            inputs = {label: Path(temp) / label for label in ('first', 'second')}
            for path in inputs.values():
                path.write_bytes(b'x' * (16 * page_size))
                self.assertEqual(len(path.read_bytes()), 16 * page_size)
            sync_inputs(inputs)
            resident = evict_inputs(inputs, page_size)
            self.assertEqual(resident.keys(), inputs.keys())
            self.assertTrue(all(size <= page_size for size in resident.values()))
            report = {'fincore': [{'file': str(path), 'size': path.stat().st_size, 'pages': 2,
                                   'res': 2 * page_size}
                                  for path in inputs.values()]}
            sample = subprocess.CompletedProcess([], 0, stdout=json.dumps(report), stderr='')
            with patch('benchmark_producer_corpus.subprocess.run', return_value=sample), patch(
                    'benchmark_producer_corpus.time.sleep'):
                with self.assertRaisesRegex(ValueError, 'residency exceeds one OS page'):
                    evict_inputs(inputs, page_size)

    def test_alternate_arm_requires_its_own_verification_at_the_timed_width(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            case = {'name': 'all', 'operation': 'iterate', 'role': 'candidate_selector'}
            spec = d/'spec.json'
            spec.write_text(json.dumps([case]))
            output = d/'summary.json'
            base = dict(name='all', count=1, work_items=1,
                        fingerprint={'count': 1, 'sum': '1', 'xor': '1'})
            checks = d/'checks.jsonl'
            checks.write_text(''.join(json.dumps(dict(base, stack=stack, cpus=[1,2])) + '\n'
                                      for stack in ('python-current', 'rust-mmap')))
            pooled = d/'pooled.jsonl'
            for file in (checks, pooled):
                file.with_suffix('.metadata.json').write_text('{"gol_sha256":"hash"}')
            raw = d/'times.jsonl'
            session = dict(round=1, cores=2, reader='pooled', producer='geodesk',
                           rows=[dict(base, setup_ns=1, first_execution_ns=2)])
            header = dict(cases=['all'], inputs={'geodesk': {'sha256': 'hash'}}, rounds=1,
                          engines=['pooled'], core_counts=[2])
            raw.write_text(''.join(json.dumps(r) + '\n' for r in (header, session)))
            cmd = [sys.executable, ROOT/'tools/summarize_producer_corpus.py', raw,
                   '--spec', spec, '--output', output, '--checks', f'geodesk={checks}']
            missing = subprocess.run(cmd, capture_output=True, text=True)
            self.assertIn('incomplete verification data for geodesk/pooled/2/all', missing.stderr)
            cmd.extend(['--checks', f'geodesk/pooled={pooled}'])
            pooled.write_text(json.dumps(dict(base, stack='rust-mmap', cpus=[1])) + '\n')
            wrong_width = subprocess.run(cmd, capture_output=True, text=True)
            self.assertIn('incomplete verification data for geodesk/pooled/2/all', wrong_width.stderr)
            pooled.write_text(json.dumps(dict(base, stack='rust-mmap', cpus=[1,2])) + '\n')
            subprocess.run(cmd, check=True)
            self.assertEqual(json.loads(output.read_text())['cohorts']['iterate']['cases'], ['all'])

    def test_cross_file_types_and_file_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            labels = ['geodesk', 'native']
            names = ['matching', 'different']
            cases = [dict(name=n, role='candidate_selector', operation='tags') for n in names]
            (d/'spec.json').write_text(json.dumps(cases))
            checks = []
            hashes = {p: {'sha256': p} for p in labels}
            runs = [{'cases': names, 'inputs': hashes, 'rounds': 1,
                     'spec_sha256': hashlib.sha256((d/'spec.json').read_bytes()).hexdigest(),
                     'engines': ['python'], 'core_counts': [1]}]
            for p in labels:
                path = d/f'{p}.jsonl'
                checks.extend(['--checks', f'{p}={path}'])
                (d/f'{p}.metadata.json').write_text(json.dumps({'gol_sha256': p}))
                with path.open('w') as check:
                    for n in names:
                        print(json.dumps(dict(stack='python-current', name=n, count=1,
                                              fingerprint={'count': 1, 'sum': '1', 'xor': '1'}, work_items=1,
                                              work_fingerprint={'features': 1, 'items': 1, 'xor': '1',
                                                                'sum': p if n == 'different' else 'same'})), file=check)
                rows = [dict(name=n, count=1, work_items=1, setup_ns=200, first_execution_ns=300) for n in names]
                runs.append(dict(reader='python', producer=p, round=1, cores=1, rows=rows))
            with gzip.open(d/'timings.gz', 'wt') as f:
                for r in runs:
                    print(json.dumps(r), file=f)
            output = d/'summary'
            cmd = [sys.executable, ROOT/'tools/summarize_producer_corpus.py', d/'timings.gz',
                   *checks, '--spec', d/'spec.json', '--output', output]
            reference = d/'reference-summary.json'
            subprocess.run([*cmd, '--reference-summary', reference], check=True)
            legacy = json.loads(reference.read_bytes())
            self.assertEqual(legacy['cohorts']['tags']['cases'], sorted(names))
            self.assertEqual(set(legacy['cohorts']['tags']['prepare_and_scan_ms']), {'geodesk/python/1'})
            summary = json.loads(output.read_text())
            tags = summary['cohorts']['tags']
            self.assertIn({'name': 'different', 'operation': 'tags', 'role': 'candidate_selector',
                           'reference_count': 1, 'status': 'content_mismatch'},
                          summary['verification_cases']['native/python'])
            (d/'spec.json').write_text(json.dumps([{**c, 'query': 'different'} for c in cases]))
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertIn('timing workload differs from spec', result.stderr)
            (d/'spec.json').write_text(json.dumps(cases))
            self.assertEqual(tags['cases'], ['matching'])
            self.assertEqual(tags['prepare_and_scan_ms']['native/python/1']['median'], .0005)
            previous = d/'previous.json'
            comparison = json.loads(output.read_text())
            previous.write_text(json.dumps(comparison))
            fixed = [*cmd, '--same-cases-as', previous]
            subprocess.run(fixed, check=True)
            self.assertEqual(comparison, json.loads(output.read_text()))
            comparison['cohorts']['tags']['cases'] = ['different']
            previous.write_text(json.dumps(comparison))
            result = subprocess.run(fixed, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('cases failed local eligibility', result.stderr)
            with gzip.open(d/'timings.gz', 'wt') as f:
                for r in runs[:-1]:
                    print(json.dumps(r), file=f)
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertIn('incomplete or unexpected producer/reader rounds', result.stderr)
            (d/'native.metadata.json').write_text('{"gol_sha256":"different file"}')
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('different native files', result.stderr)


if __name__ == '__main__':
    unittest.main()
