#!/usr/bin/env python3
"""Fetch and verify the dated UK PBF used to build the benchmark GOLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

from fetch_application_gol import fetch as fetch_lfs

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "benchmarks/uk-data-provenance.json"
LFS_PARTS = ROOT / "benchmarks/uk-20261115-pbf-lfs.json"
LFS_TAG = "benchmark-uk-20261115-pbf-lfs"


def verify(path, source):
    if path.stat().st_size != source["pbf_bytes"]:
        raise ValueError(f"PBF size differs from the manifest: {path}")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != source["pbf_sha256"]:
            raise ValueError(f"PBF SHA-256 differs from the manifest: {path}")


def fetch(destination, url=None, lfs=False):
    source = json.loads(PROVENANCE.read_bytes())
    if destination.exists():
        verify(destination, source)
        return
    if lfs:
        fetch_lfs(destination, os.environ["GITHUB_SERVER_URL"], os.environ["GITHUB_REPOSITORY"],
                  os.environ["BENCHMARK_DOWNLOAD_TOKEN"], sha256=source["pbf_sha256"],
                  size=source["pbf_bytes"], tag=LFS_TAG, parts_path=LFS_PARTS)
        return
    url = url or source["source_url"]
    if urlsplit(url).scheme != "https":
        raise ValueError("PBF download URL must use HTTPS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        download = Path(scratch) / "source.osm.pbf"
        subprocess.run(["curl", "--fail", "--location", "--retry", "3",
                        "--proto", "=https", "--proto-redir", "=https",
                        "--output", download, "--", url], check=True)
        verify(download, source)
        download.replace(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--url", help="HTTPS mirror of the exact dated UK PBF")
    transport.add_argument("--lfs", action="store_true", help="Fetch the pinned PBF from private Git LFS")
    args = parser.parse_args()
    fetch(args.destination, args.url, args.lfs)
