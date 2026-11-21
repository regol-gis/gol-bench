"""Materialize an exact Regol build input and install this harness's worker sources."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'benchmarks/regol-sources.json'
DEFAULT_REPO = 'https://github.com/regol-gis/regol.git'
INPUTS = ['Cargo.toml', 'Cargo.lock', 'LICENSE', 'README.md', 'src', 'cli', 'examples']


def git(source, *args):
    return subprocess.check_output(['git', '-C', source, *args], text=True).strip()


def source_inputs(source, ref):
    legacy = 'benchmarks/uk-engine-workers.json'
    matched = git(source, 'ls-tree', '-r', '--name-only', ref, '--', legacy)
    return [*INPUTS, *matched.splitlines()]


def ensure_remote_checkout(repo_url, ref):
    cache = ROOT / '.reference/regol-upstream.git'
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        subprocess.run(['git', 'init', '--bare', '-q', cache], check=True)
        subprocess.run(['git', '-C', cache, 'remote', 'add', 'origin', repo_url], check=True)
    if subprocess.run(['git', '-C', cache, 'rev-parse', '--verify', '--quiet', ref + '^{commit}']).returncode:
        subprocess.run(['git', '-C', cache, 'fetch', '--depth=1', 'origin', ref], check=True)
        return cache, git(cache, 'rev-parse', 'FETCH_HEAD')
    return cache, git(cache, 'rev-parse', ref + '^{commit}')


def materialize(destination, revision='current', source=None):
    pins = json.loads(LOCK.read_text())
    pin = pins.get(revision) or next((v for v in pins.values() if v.get('revision') == revision), None)
    if source is None:
        if pin and 'tag' in pin:
            source = ROOT
            ref = 'refs/tags/' + pin['tag']
            if subprocess.run(['git', '-C', ROOT, 'show-ref', '--verify', '--quiet', ref]).returncode:
                subprocess.run(['git', '-C', ROOT, 'fetch', '--no-tags', 'origin', f'{ref}:{ref}'], check=True)
            if git(source, 'rev-parse', ref + '^{tree}') != pin['tree']:
                raise ValueError('Regol source tree differs from the lock')
            original = pin['revision']
        else:
            repo_url = (pin or {}).get('repository', DEFAULT_REPO)
            target_ref = (pin or {}).get('ref') or (pin or {}).get('revision') or revision
            source, original = ensure_remote_checkout(repo_url, target_ref)
            ref = original
    else:
        ref = pin['revision'] if pin else revision
        original = git(source, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}')
        ref = original
    if destination.exists():
        raise FileExistsError(f'Use a fresh output directory: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        stage = Path(tmp) / 'source'
        stage.mkdir()
        archive = Path(tmp) / 'input.tar'
        subprocess.run(['git', '-C', source, 'archive', '-o', archive.resolve(), ref, *source_inputs(source, ref)], check=True)
        with tarfile.open(archive) as data:
            data.extractall(stage, filter='data')
        shutil.copytree(ROOT / 'examples', stage / 'examples', dirs_exist_ok=True)
        (stage / 'BENCHMARK-READER-REVISION').write_text(original + '\n')
        stage.rename(destination)
    return original


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--revision', default='current')
    parser.add_argument('--source', type=Path, help='Use an independent Regol Git checkout')
    args = parser.parse_args()
    print(materialize(args.destination, args.revision, args.source))
