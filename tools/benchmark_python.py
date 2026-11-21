#!/usr/bin/env python3
"""Measure pinned persistent-Python and subprocess+JSON baselines."""
import argparse
import importlib.metadata
import json
import pathlib
import platform
import statistics
import subprocess
import sys
import time
from geodesk import Features

parser = argparse.ArgumentParser()
parser.add_argument("file", type=pathlib.Path)
parser.add_argument("--iterations", type=int, default=200)
parser.add_argument("--query", default="n[amenity=cafe][capacity>=500]")
parser.add_argument("--output", type=pathlib.Path, required=True)
args = parser.parse_args()
path = str(args.file.resolve())
library = Features(path)
query_text = args.query
query = library(query_text)
report = {"python": sys.version, "geodesk": importlib.metadata.version("geodesk"),
          "platform": platform.platform(), "file": str(args.file), "iterations": args.iterations, "query":query_text,
          "note": "Warm cache; Python result iteration is compared to Rust result iteration, not native count shortcuts.", "measurements": {}}

def measure(name, fn, iterations=args.iterations):
    samples = []
    work = None
    for _ in range(5):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            work = fn()
        samples.append((time.perf_counter_ns() - start) / iterations)
    report["measurements"][name] = {"median_ns": statistics.median(samples), "samples_ns": samples, "work_per_iteration": work}

def scan():
    return sum(1 for _ in library)

def first():
    return next(iter(library)).id

def select():
    return sum(1 for _ in query)

measure("first_result", first)
measure("scan", scan)
measure("query", select)

code = "from geodesk import Features; import json,sys; print(json.dumps([str(f) for f in Features(sys.argv[1])(sys.argv[2])]))"
def spawn():
    out = subprocess.check_output([sys.executable, "-c", code, path, query_text])
    return len(json.loads(out))
measure("subprocess_query_json", spawn, 5)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report["measurements"], indent=2))
