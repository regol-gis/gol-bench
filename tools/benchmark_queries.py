#!/usr/bin/env python3
"""Compare identical prepared queries, output iteration, CPU time and affinity.

Inputs must remain immutable. OSM data is never copied into the output report.
"""

from paths import ROOT, WORKER
from benchmark_storage import cache_environment, validate_cache

import argparse
import hashlib
import json
import math
import os
import pathlib
import platform
import statistics
import subprocess
import time
from benchmark_sessions import delivered_count

parser = argparse.ArgumentParser()
parser.add_argument("file", type=pathlib.Path)
parser.add_argument(
    "--spec", type=pathlib.Path, default=ROOT/"benchmarks/uk-queries.json"
)
parser.add_argument("--extra-spec", type=pathlib.Path, action="append", default=[])
parser.add_argument("--python", required=True)
parser.add_argument("--rust", default=str(WORKER))
parser.add_argument("--output", type=pathlib.Path, required=True)
parser.add_argument("--samples", type=int, default=7)
parser.add_argument("--target-ms", type=int, default=100)
parser.add_argument("--only")
parser.add_argument(
    "--storage",
    choices=["paged", "owned", "file", "windowed", "cached", "mmap-immutable"],
    default="paged",
)
parser.add_argument("--threads", type=int, default=0)
parser.add_argument(
    "--one-cpu-threads",
    type=int,
    default=0,
    help="Rust workers in the one-CPU control; 0 uses the serial iterator",
)
parser.add_argument("--affinity", choices=["all", "one", "both"], default="both")
parser.add_argument("--subprocess", action="store_true")
parser.add_argument("--cache-mib", type=int, default=16)
args = parser.parse_args()
rust_cache_env = cache_environment(args.storage, args.cache_mib)
if (
    args.samples <= 0
    or args.target_ms < 0
    or min(args.threads, args.one_cpu_threads) < 0
):
    parser.error("invalid sample budget")
specs = json.loads(args.spec.read_text())
for extra in args.extra_spec:
    specs.extend(json.loads(extra.read_text()))
if len({spec["name"] for spec in specs}) != len(specs):
    parser.error("duplicate workload names")
WIDTH = 4294967294.9999
for spec in [step for spec in specs for step in spec.get("steps", [spec])]:
    if "bbox_lonlat" in spec:
        west, south, east, north = spec["bbox_lonlat"]
        x = lambda lon: math.floor(lon * WIDTH / 360 + 0.5)
        y = lambda lat: math.floor(
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
            * WIDTH
            / (2 * math.pi)
            + 0.5
        )
        spec["bounds"] = [x(west), y(south), x(east), y(north)]
if args.only:
    specs = [s for s in specs if s["name"] == args.only]
if not specs:
    parser.error("no queries selected")
args.output.parent.mkdir(parents=True, exist_ok=True)
resolved = args.output.with_suffix(".queries.json")
resolved.write_text(json.dumps(specs, indent=2) + "\n")
affinity = sorted(os.sched_getaffinity(0))
plans = []
if args.affinity in ("all", "both"):
    plans.append(("all", affinity))
if args.affinity in ("one", "both"):
    plans.append(("one", [affinity[0]]))
records = []
source_paths = [ROOT/"benchmarks/regol-sources.json"]
source_paths += sorted((ROOT/"examples").rglob("*.rs"))
source_paths += sorted((ROOT/"tools").glob("*benchmark*.py"))
run_source_hashes = {
    str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
}
run_binary_hash = hashlib.sha256(pathlib.Path(args.rust).read_bytes()).hexdigest()
for index, (mode, cpus) in enumerate(plans):
    # Reverse engine order for the second CPU budget to reduce ordering bias.
    engines = ["rust", "python"] if index % 2 == 0 else ["python", "rust"]
    for engine in engines:
        prefix = ["taskset", "-c", ",".join(map(str, cpus))]
        if engine == "rust":
            command = prefix + [
                args.rust,
                str(args.file),
                str(resolved),
                args.storage,
                str(args.samples),
                str(args.target_ms),
                "-",
                "0",
                "verify",
                str(args.one_cpu_threads if mode == "one" else args.threads),
            ]
        else:
            command = prefix + [
                args.python,
                str(ROOT/"tools/query_benchmark_python.py"),
                str(args.file),
                str(resolved),
                "--samples",
                str(args.samples),
                "--target-ms",
                str(args.target_ms),
            ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            text=True,
            env=(
                rust_cache_env
                if engine == "rust"
                else cache_environment("python", args.cache_mib)
            ),
        )
        try:
            for line in process.stdout:
                record = json.loads(line)
                if engine == "rust":
                    validate_cache(record, args.storage, args.cache_mib)
                record["affinity"] = mode
                record["cpus"] = cpus
                record["median_wall_ns"] = statistics.median(record["samples_wall_ns"])
                record["median_cpu_ns"] = statistics.median(record["samples_cpu_ns"])
                if "samples_thread_cpu_ns" in record:
                    record["median_thread_cpu_ns"] = statistics.median(
                        record["samples_thread_cpu_ns"]
                    )
                records.append(record)
                # Preserve each completed query if a subsequent query fails.
                args.output.with_suffix(".partial.json").write_text(
                    json.dumps(records, indent=2) + "\n"
                )
                print(
                    f"{mode:3} {engine:6} {record['name']:25} {record['count']:10} {record['median_wall_ns']/1e6:10.3f} ms cpu/wall {record['median_cpu_ns']/max(1,record['median_wall_ns']):.2f}",
                    flush=True,
                )
        finally:
            process.stdout.close()
        if process.wait():
            raise SystemExit(f"{engine} benchmark worker failed")
validation = []
for spec in specs:
    group = [r for r in records if r["name"] == spec["name"]]
    baseline = group[0]["fingerprint"]
    for r in group:
        try:
            delivered_count(spec, r)
        except ValueError as error:
            validation.append(
                {
                    "name": spec["name"],
                    "engine": r["engine"],
                    "affinity": r["affinity"],
                    "reason": str(error),
                }
            )
    for r in group[1:]:
        if r["fingerprint"] != baseline or r.get("work_fingerprint") != group[0].get(
            "work_fingerprint"
        ):
            validation.append(
                {
                    "name": spec["name"],
                    "engine": r["engine"],
                    "affinity": r["affinity"],
                    "expected": baseline,
                    "actual": r["fingerprint"],
                    "expected_work": group[0].get("work_fingerprint"),
                    "actual_work": r.get("work_fingerprint"),
                }
            )
subprocesses = []
if args.subprocess:
    for spec in specs:
        if spec["name"] not in ("cafes", "london_cafes", "amenity_presence"):
            continue
        samples = []
        count = None
        for _ in range(5):
            start = time.perf_counter_ns()
            data = subprocess.check_output(
                [
                    args.python,
                    str(ROOT/"tools/query_benchmark_python.py"),
                    str(args.file),
                    str(resolved),
                    "--only",
                    spec["name"],
                    "--once-json",
                ]
            )
            count = len(json.loads(data))
            samples.append(time.perf_counter_ns() - start)
        expected = next(
            r["count"]
            for r in records
            if r["name"] == spec["name"] and r["engine"] == "python"
        )
        assert count == expected
        subprocesses.append(
            {
                "name": spec["name"],
                "count": count,
                "samples_wall_ns": samples,
                "median_wall_ns": statistics.median(samples),
            }
        )
report = {
    "source_sha256": run_source_hashes,
    "rust_binary_sha256": run_binary_hash,
    "storage": args.storage,
    "rust_cache_mib": args.cache_mib if args.storage == "cached" else None,
    "rust_threads": args.threads,
    "rust_one_cpu_threads": args.one_cpu_threads,
    "platform": platform.platform(),
    "cpu_model": next(
        (
            line.split(":", 1)[1].strip()
            for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        None,
    ),
    "affinity_available": affinity,
    "file": str(args.file),
    "file_bytes": args.file.stat().st_size,
    "file_sha256": hashlib.file_digest(args.file.open("rb"), "sha256").hexdigest(),
    "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
    "git_commit": subprocess.check_output(
        ["git", "-C", ROOT, "rev-parse", "HEAD"], text=True
    ).strip(),
    "python_version": subprocess.check_output(
        [args.python, "--version"], text=True
    ).strip(),
    "geodesk_version": subprocess.check_output(
        [
            args.python,
            "-c",
            'import importlib.metadata; print(importlib.metadata.version("geodesk"))',
        ],
        text=True,
    ).strip(),
    "method": "One untimed first execution; separate warm-batch calibration; all results or the requested limit iterated; full-query ID fingerprint, limited-result membership and optional field-content fingerprint outside timing. Result order is unspecified. Timed limited runs include iterator release; separate first_item_ns/release_total_ns are single diagnostics with identity capture, not sample medians or isolated cleanup timings. CPU affinity is a budget, not worker-thread control. No native count shortcuts.",
    "queries": specs,
    "results": records,
    "subprocess_json": subprocesses,
    "validation_failures": validation,
}
args.output.write_text(json.dumps(report, indent=2) + "\n")
args.output.with_suffix(".partial.json").unlink(missing_ok=True)
if validation:
    raise SystemExit(
        f"{len(validation)} result fingerprint mismatches; inspect report before interpreting timings"
    )
