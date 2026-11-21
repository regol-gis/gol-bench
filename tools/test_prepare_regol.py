from pathlib import Path
import subprocess
import tempfile
import unittest

from prepare_regol import materialize, ROOT, source_inputs


class SourceLayoutTests(unittest.TestCase):
    def test_benchmark_examples_overlay_reader_trees_with_or_without_the_old_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'reader'
            source.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',source,'-c','user.name=Benchmark test',
                    '-c','user.email=test@example.invalid','-c','commit.gpgsign=false',*args],text=True).strip()
            git('init','-q')
            files = ['Cargo.toml','Cargo.lock','LICENSE','README.md','src/lib.rs','cli/Cargo.toml','examples/dump.rs',
                     'examples/tag_sidecar_trial.rs','benchmarks/uk-engine-workers.json']
            for name in files:
                file = source/name
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_text('old reader content')
            git('add','.')
            git('commit','-qm','initial')
            legacy = git('rev-parse','HEAD')
            git('rm','-q','benchmarks/uk-engine-workers.json')
            git('commit','-qm','relocate comparative research')
            modern = git('rev-parse','HEAD')
            for revision, expected in ((legacy,True),(modern,False)):
                with self.subTest(revision=revision):
                    self.assertEqual('benchmarks/uk-engine-workers.json' in source_inputs(source, revision),expected)
                    dest=Path(directory)/revision
                    self.assertEqual(materialize(dest, revision, source),revision)
                    self.assertEqual((dest/'benchmarks/uk-engine-workers.json').exists(),expected)
                    self.assertEqual((dest/'examples/tag_sidecar_trial.rs').read_bytes(),
                                     (ROOT/'examples/tag_sidecar_trial.rs').read_bytes())
                    self.assertEqual((dest/'examples/dump.rs').read_text(),'old reader content')
