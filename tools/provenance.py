"""Identify the actual benchmark source used, including local development edits."""
from functools import lru_cache
import hashlib
import os
import subprocess

from paths import ROOT


@lru_cache(maxsize=1)
def harness_source():
    paths = ['gol-bench', 'tools', 'examples', 'benchmarks']
    git = ['git', '-C', ROOT]
    files = subprocess.check_output([*git,'ls-files','--cached','--others','--exclude-standard','-z','--',*paths])
    state = hashlib.sha256()
    for name in sorted(set(files.split(b'\x00')) - {b''}):
        path = ROOT/os.fsdecode(name)
        if path.is_file():
            state.update(name+b'\x00'+hashlib.sha256(path.read_bytes()).digest())
    changes = subprocess.check_output([*git,'status','--porcelain','--untracked-files=all','--',*paths])
    revision = subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()
    return {'revision':revision,'modified':bool(changes),'source_sha256':state.hexdigest()}
