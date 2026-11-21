#!/usr/bin/env python3
"""Measure a public gol-build invocation on Linux."""
from paths import ROOT
import argparse
import json
from pathlib import Path
import resource
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pbf', type=Path)
    parser.add_argument('gol', type=Path)
    parser.add_argument('--writer', type=Path, default=ROOT/'target/release/gol-build')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    start = time.monotonic()
    subprocess.run([args.writer.resolve(), args.pbf, args.gol], check=True)
    elapsed = time.monotonic() - start
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    metrics = dict(wall_s=elapsed, user_s=usage.ru_utime, system_s=usage.ru_stime,
                   peak_rss_kib=usage.ru_maxrss, major_faults=usage.ru_majflt,
                   bytes=args.gol.stat().st_size)
    args.output.write_text(json.dumps(metrics, indent=2) + '\n')


if __name__ == '__main__':
    main()
