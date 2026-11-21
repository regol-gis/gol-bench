#!/usr/bin/env python3
"""Fetch pinned UK benchmark inputs over Git LFS using a repository read credential."""
import argparse
import base64
import json
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from urllib import request, parse

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "47e289273cad6bacd5a8e2df69739a64ef27e2d104c6e460914b4bd1f2d94549"
TAG = "benchmark-uk-20261115-lfs"


class SameHostRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl)[:2] != parse.urlsplit(req.full_url)[:2]:
            raise ValueError("benchmark release redirected to a different origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def matches_file(path, sha256, size=None):
    if size is not None and path.stat().st_size != size:
        raise ValueError(f"wrong size for assembled benchmark input: {path}")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == sha256


def fetch(destination, server, repository, token, *, sha256=EXPECTED, size=None, tag=TAG,
          parts_path=ROOT / "benchmarks/uk-20261115-lfs.json", parts=None):
    if destination.exists():
        if matches_file(destination, sha256, size):
            return
        raise ValueError(f"existing file has a different SHA-256: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = request.build_opener(SameHostRedirect())
    if parts is None:
        parts = json.loads(parts_path.read_bytes())
    # SHA-256 values of the compressed chunks are also their Git LFS object IDs.
    endpoint = f"{server.rstrip('/')}/{repository}.git/info/lfs/objects/batch"
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    payload = {"operation": "download", "transfers": ["basic"], "ref": {"name": "refs/tags/" + tag},
               "objects": [{"oid": part["oid"], "size": part["size"]} for part in parts]}
    req = request.Request(endpoint, data=json.dumps(payload).encode(), headers={
        "Authorization": "Basic " + auth, "Accept": "application/vnd.git-lfs+json",
        "Content-Type": "application/vnd.git-lfs+json"})
    with opener.open(req, timeout=120) as response:
        objects = {row["oid"]: row for row in json.load(response)["objects"]}
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        output = Path(scratch) / destination.name
        with ExitStack() as stack:
            stream = stack.enter_context(output.open("wb"))
            proc = stack.enter_context(subprocess.Popen(["zstd", "-qd", "-c"], stdin=subprocess.PIPE,
                                                        stdout=stream))
            pipe = stack.enter_context(proc.stdin)
            for part in parts:
                filename, expected = part["name"], part["oid"]
                action = objects[expected].get("actions", {}).get("download")
                if not action or parse.urlsplit(action["href"])[:2] != parse.urlsplit(endpoint)[:2]:
                    raise ValueError(f"missing or unexpected LFS download for {filename}")
                req = request.Request(action["href"], headers=action.get("header", {}))
                checksum = hashlib.sha256()
                downloaded = 0
                with opener.open(req, timeout=120) as response:
                    for block in iter(lambda: response.read(1 << 20), b""):
                        downloaded += len(block)
                        if downloaded > part["size"]:
                            raise ValueError(f"wrong size for {filename}")
                        pipe.write(block)
                        checksum.update(block)
                if downloaded != part["size"]:
                    raise ValueError(f"wrong size for {filename}")
                if checksum.hexdigest() != expected:
                    raise ValueError(f"wrong checksum for {filename}")
                print(f"Verified {filename}", flush=True)
            pipe.close()
            if proc.wait():
                raise RuntimeError("zstd decompression failed")
        if not matches_file(output, sha256, size):
            raise ValueError("wrong checksum for assembled benchmark input")
        output.replace(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    fetch(args.destination, os.environ["GITHUB_SERVER_URL"], os.environ["GITHUB_REPOSITORY"],
          os.environ["BENCHMARK_DOWNLOAD_TOKEN"])
