#!/usr/bin/env python3
"""Fetch the verified UK native benchmark layout when CI cannot rebuild it in time."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from fetch_application_gol import ROOT, fetch, matches_file

MANIFEST = ROOT / 'benchmarks/uk-20261115-native-lfs.json'


def download(destination, layout, manifest, server, repository, token):
    archive = manifest['default']

    def unpack(target, record):
        fetch(target, server, repository, token, tag=manifest['tag'], parts=record['parts'],
              sha256=record['sha256'], size=record['bytes'])

    if layout == 'fanout-8':
        unpack(destination, archive)
        return
    if layout != 'merged-standard':
        raise ValueError(f'unknown native layout {layout}')
    control = manifest['control']
    if destination.exists():
        if matches_file(destination, control['sha256'], control['bytes']):
            return
        raise ValueError(f'existing file has a different SHA-256: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        base, delta, output = (Path(scratch) / n for n in ('default.gol', 'control.vcdiff', 'control.gol'))
        unpack(base, archive)
        unpack(delta, manifest['delta'])
        subprocess.run(['xdelta3', '-d', '-s', base, delta, output], check=True)
        if not matches_file(output, control['sha256'], control['bytes']):
            raise ValueError('reconstructed native control has the wrong checksum')
        output.replace(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--layout', required=True, choices=['fanout-8', 'merged-standard'])
    args = parser.parse_args()
    download(args.destination, args.layout, json.loads(MANIFEST.read_bytes()),
             os.environ['GITHUB_SERVER_URL'], os.environ['GITHUB_REPOSITORY'],
             os.environ['BENCHMARK_DOWNLOAD_TOKEN'])
