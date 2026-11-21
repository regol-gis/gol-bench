"""List, prepare and verify benchmark inputs using a checksum-locked catalogue."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from paths import ROOT

CATALOG = ROOT / 'benchmarks/datasets.json'


def catalogue(planet_lock=None):
    records = json.loads(CATALOG.read_bytes())
    if planet_lock:
        pin = json.loads(planet_lock.read_bytes())
        if set(pin) != {'bytes', 'sha256', 'snapshot'} or not isinstance(pin['bytes'], int) or pin['bytes'] <= 0:
            raise ValueError('invalid planet lock')
        if len(bytes.fromhex(pin['sha256'])) != 32:
            raise ValueError('expected a SHA-256 digest')
        records['planet'].update(pin)
    return records


def planet_pin(item):
    with urlopen(Request(item['index'],headers={'User-Agent':'gol-bench/1.0'}), timeout=40) as response:
        rows = json.load(response)['files']
    row, = [r for r in rows if r['entity'] == 'planet-gol' and r['remote_version'] == 'v2']
    return {'bytes': row['size'], 'sha256': row['checksum_sha256'],
            'snapshot': datetime.fromtimestamp(row['updated'] / 1000, timezone.utc).date().isoformat()}


def verify(path, item):
    if path.stat().st_size != item['bytes']:
        raise ValueError(f"{path}: expected {item['bytes']} bytes, got {path.stat().st_size}")
    if 'layout_sha256' in item:
        from native_layout import native_digests
        actual,layout=native_digests(path)
        if layout!=item['layout_sha256']:
            raise ValueError(f'{path}: native layout SHA-256 differs from the lock')
        return actual
    with path.open('rb') as src:
        actual = hashlib.file_digest(src, 'sha256').hexdigest()
    if actual != item['sha256']:
        raise ValueError(f'{path}: unexpected SHA-256 {actual}')
    return actual


def dataset_path(name, item, dest):
    if 'fixture' in item:
        return ROOT / item['fixture']
    digest = item.get('layout_sha256') or item['sha256']
    return dest / (name + '-' + digest[:12] + '.gol')


def build_regol(writer, pbf, stage):
    cmd = [writer.resolve(), 'build', pbf, stage] if writer.name == 'regol' else [writer.resolve(), pbf, stage]
    subprocess.run(cmd, stdout=sys.stderr, check=True)


def download_pbf(item, dest_file):
    req = Request(item['pbf_url'], headers={'User-Agent': 'gol-bench/1.0'})
    with urlopen(req, timeout=300) as response, dest_file.open('wb') as out:
        shutil.copyfileobj(response, out, 4 << 20)
    if 'pbf' in item:
        verify(dest_file, item['pbf'])


def prepare(name, item, dest, writer=None, pbf=None, planet_url=None):
    path = dataset_path(name, item, dest)
    if 'fixture' in item:
        verify(path, item)
        return path
    dest.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if 'sha256' in item or 'layout_sha256' in item:
            try:
                verify(path, item)
                return path
            except ValueError:
                if 'pbf_url' not in item or 'blob' in item:
                    raise
                return path
        return path
    free = shutil.disk_usage(dest).free
    required = item['bytes'] + (2 << 30 if name == 'planet' else 0)
    if 'layout_sha256' in item:
        required += item['pbf']['bytes'] * 8
    if free < required:
        raise OSError(f'{dest}: {free:,} bytes available; {required:,} required for {name}')
    # Link the verified private staging file atomically. Concurrent fetchers
    # cannot clobber one another, and failed downloads leave no partial input.
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        stage = Path(tmp) / 'dataset.gol'
        built_from_public_pbf = False
        if 'blob' in item:
            ref = 'refs/tags/' + item['tag']
            if subprocess.run(['git','-C',ROOT,'show-ref','--quiet','--verify',ref]).returncode:
                subprocess.run(['git','-C',ROOT,'fetch','--no-tags','origin',f'{ref}:{ref}'],check=True)
            actual = subprocess.check_output(['git','-C',ROOT,'rev-parse',ref+':'+item['member']],text=True).strip()
            if actual != item['blob']:
                raise ValueError('asset tag differs from the catalogue')
            with stage.open('wb') as out, subprocess.Popen(['git','-C',ROOT,'cat-file','blob',actual],stdout=subprocess.PIPE) as src:
                subprocess.run(['zstd','-dq','-c'], stdin=src.stdout, stdout=out, check=True)
                if src.wait():
                    raise ValueError('could not read compressed Git asset')
            if 'original_gol' in item:
                verify(stage,item['original_gol'])
                with stage.open('ab') as out:
                    out.write(bytes(item['bytes'] - item['original_gol']['bytes']))
        elif 'layout_sha256' in item:
            default_writer = ROOT / 'target/release/regol'
            if not default_writer.is_file():
                default_writer = ROOT / 'target/release/gol-build'
            writer = writer or default_writer
            if not writer.is_file():
                raise FileNotFoundError('this dataset needs the pinned writer: run gol-bench setup writer')
            if pbf is None:
                pbf = Path(tmp) / 'input.osm.pbf'
                download_pbf(item, pbf)
            else:
                verify(pbf, item['pbf'])
            build_regol(writer, pbf, stage)
        elif 'pbf_url' in item:
            from paths import SDK
            gol_tool = SDK / 'gol-legacy/gol'
            if not gol_tool.is_file():
                gol_tool = SDK / 'gol-current/gol'
            if pbf is None:
                pbf = Path(tmp) / 'input.osm.pbf'
                download_pbf(item, pbf)
            elif 'pbf' in item:
                verify(pbf, item['pbf'])
            if gol_tool.is_file():
                subprocess.run([gol_tool.resolve(), 'build', stage, pbf, '-w'], stdout=sys.stderr, check=True)
                rem = stage.stat().st_size % 4096
                if rem:
                    with stage.open('ab') as out:
                        out.write(bytes(4096 - rem))
            else:
                default_writer = writer or (ROOT / 'target/release/regol')
                if not default_writer.is_file():
                    raise FileNotFoundError('run gol-bench setup gol-tool-legacy or gol-bench setup writer first')
                build_regol(default_writer, pbf, stage)
            built_from_public_pbf = True
        elif name == 'planet':
            url=planet_url or item['url']
            if urlsplit(url).scheme!='https' or not urlsplit(url).hostname:
                raise ValueError('planet mirror must be an HTTPS URL')
            if planet_url is None:
                live = planet_pin(item)
                if live['sha256'] != item['sha256'] or live['bytes'] != item['bytes']:
                    raise ValueError('publisher rotated the planet; supply the archived GOL, an HTTPS mirror, or capture a new lock with datasets pin-planet')
            if shutil.which('aria2c'):
                subprocess.run(['aria2c','--console-log-level=warn','--summary-interval=60',
                                '--file-allocation=falloc','--max-connection-per-server=8','--split=8',
                                '--min-split-size=32M','--user-agent=gol-bench/1.0',
                                '--dir',tmp,'--out',stage.name,url],stdout=sys.stderr,check=True)
            else:
                req=Request(url,headers={'User-Agent':'gol-bench/1.0'})
                with urlopen(req,timeout=180) as response, stage.open('wb') as out:
                    if int(response.headers['Content-Length']) != item['bytes']:
                        raise ValueError('planet length differs from the lock')
                    shutil.copyfileobj(response,out,4<<20)
        else:
            subprocess.run([sys.executable, ROOT/'tools/fetch_application_gol.py',stage],stdout=sys.stderr,check=True)
        if not built_from_public_pbf:
            verify(stage, item)
        try:
            os.link(stage, path)
        except FileExistsError:
            if not built_from_public_pbf:
                verify(path, item)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action',required=True)
    sub.add_parser('list')
    pin = sub.add_parser('pin-planet', help='capture today’s published size and digest without downloading the GOL')
    pin.add_argument('output',type=Path)
    fetch = sub.add_parser('prepare')
    fetch.add_argument('dataset', choices=catalogue())
    fetch.add_argument('--directory', type=Path, default=ROOT/'.reference/datasets')
    fetch.add_argument('--planet-lock', type=Path)
    fetch.add_argument('--planet-url',help='HTTPS mirror of the exact locked planet GOL')
    fetch.add_argument('--source-pbf',type=Path,help='reuse a local PBF for a writer-derived dataset')
    fetch.add_argument('--writer',type=Path,help='Regol builder for a writer-derived dataset')
    a = p.parse_args()
    items = catalogue(getattr(a,'planet_lock',None))
    if a.action == 'list':
        for key, item in items.items():
            print(f"{key:15} {item['bytes']:>15,}  {item['coverage']}")
    elif a.action == 'pin-planet':
        with a.output.open('x') as out:
            json.dump(planet_pin(items['planet']),out,indent=2)
            out.write('\n')
    else:
        if (a.source_pbf or a.writer) and 'layout_sha256' not in items[a.dataset]:
            p.error('--source-pbf and --writer apply to writer-derived datasets')
        if (a.planet_url or a.planet_lock) and a.dataset!='planet':
            p.error('planet options require a planet dataset')
        print(prepare(a.dataset, items[a.dataset], a.directory.resolve(),a.writer,a.source_pbf,a.planet_url))


if __name__ == '__main__':
    main()
