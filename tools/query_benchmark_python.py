#!/usr/bin/env python3
"""Persistent Python counterpart to examples/query_benchmark.rs.

Count workloads use the native scalar count. Iterator workloads consume each result. The ID
fingerprint is computed separately, outside all execution timings.
"""

import argparse
import json
import itertools
import pathlib
import sys
import time
from benchmark_workloads import consume
from benchmark_sessions import run_session

parser = argparse.ArgumentParser()
parser.add_argument("file")
parser.add_argument("spec")
parser.add_argument("--samples", type=int, default=7)
parser.add_argument("--target-ms", type=int, default=100)
parser.add_argument("--only")
parser.add_argument("--repeats", type=int)
parser.add_argument("--skip-verification", action="store_true")
parser.add_argument("--once-json", action="store_true")
parser.add_argument("--preload-shapely", action="store_true", help="model a client that already imported its geometry package")
parser.add_argument("--first-pass", action="store_true", help="execute each step once, with compact output")
args = parser.parse_args()
if args.first_pass and (args.samples or args.target_ms or args.once_json or args.repeats):
    parser.error("--first-pass requires zero samples and target duration")
# Import inside worker process so process startup is genuinely measured by the
# separate subprocess baseline, but excluded from persistent execution timings.
import_start = time.perf_counter_ns()
from geodesk import Features, Box

if args.preload_shapely:
    import shapely

import_ns = time.perf_counter_ns() - import_start
start = time.perf_counter_ns()
library = Features(args.file)
open_ns = time.perf_counter_ns() - start
specs = json.loads(pathlib.Path(args.spec).read_text())
MASK = (1 << 64) - 1


def fingerprint(query, wanted):
    count = total = xor = 0
    for f in query:
        value = f.id * 4 + (0 if f.is_node else 1 if f.is_way else 2)
        wanted.discard(value)
        mix = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        mix = ((mix ^ (mix >> 27)) * 0x94D049BB133111EB) & MASK
        mix ^= mix >> 31
        count += 1
        total = (total + mix) & MASK
        xor ^= mix
    return {"count": count, "sum": str(total), "xor": str(xor)}


for spec in specs:
    if args.only and spec["name"] != args.only:
        continue
    if "steps" in spec:
        if args.once_json:
            raise ValueError("session workloads do not support --once-json")
        record = run_session(library, Box, spec, args, fingerprint)
        record["open_ns"] = open_ns
        print(json.dumps(record), flush=True)
        continue
    start = time.perf_counter_ns()
    query = library(spec["query"])
    if "bounds" in spec:
        query = query(Box(*spec["bounds"]))
    operation = spec.get("operation", "iterate")
    limit = spec.get("limit")
    if limit is not None and (
        not isinstance(limit, int) or limit < 0 or operation != "iterate"
    ):
        raise ValueError("limits require nonnegative counts and handle iteration")

    def execute():
        if operation == "count":
            n = query.count
            return {"features": n, "items": n, "sum": "0", "xor": "0"}
        return consume(
            query if limit is None else itertools.islice(query, limit), operation
        )

    setup_ns = time.perf_counter_ns() - start
    if args.once_json:
        if operation != "iterate" or limit is not None:
            raise ValueError("JSON ID output requires an unlimited iterate workload")
        start = time.perf_counter_ns()
        print(json.dumps([str(f) for f in query], separators=(",", ":")), flush=True)
        execute_write_ns = time.perf_counter_ns() - start
        memory = pathlib.Path("/proc/self/status").read_text().splitlines()
        print(
            json.dumps(
                {
                    "name": spec["name"],
                    "import_ns": import_ns,
                    "open_ns": open_ns,
                    "setup_ns": setup_ns,
                    "execute_write_ns": execute_write_ns,
                    "process": [
                        line
                        for line in memory
                        if line.startswith(
                            ("VmRSS:", "VmHWM:", "RssAnon:", "RssFile:", "Threads:")
                        )
                    ],
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        continue
    start = time.perf_counter_ns()
    work = execute()
    count = work["features"]
    first_ns = time.perf_counter_ns() - start
    if args.first_pass:
        print(json.dumps({"name": spec["name"], "setup_ns": setup_ns, "first_execution_ns": first_ns,
                          "open_ns": open_ns, "count": count, "work_items": work["items"]}), flush=True)
        continue
    calibration_start = time.perf_counter_ns()
    repeats = args.repeats or 1
    target_ns = args.target_ms * 1_000_000
    if args.repeats is None and target_ns:
        while True:
            start = time.perf_counter_ns()
            for _ in range(repeats):
                assert execute() == work, "work changed during calibration"
            elapsed = max(time.perf_counter_ns() - start, 1)
            estimate = max(1, min(1000, target_ns * repeats // elapsed))
            if elapsed >= target_ns // 2 or repeats == 1000:
                repeats = estimate
                break
            repeats = min(1000, max(repeats + 1, estimate))
    calibration_ns = time.perf_counter_ns() - calibration_start
    wall = []
    cpu = []
    thread_cpu = []
    for _ in range(args.samples):
        c = time.process_time_ns()
        t = time.thread_time_ns()
        start = time.perf_counter_ns()
        for _ in range(repeats):
            actual = execute()
            assert actual == work, "query results changed while measuring"
        wall.append((time.perf_counter_ns() - start) / repeats)
        cpu.append((time.process_time_ns() - c) / repeats)
        thread_cpu.append((time.thread_time_ns() - t) / repeats)
    wanted = set()
    first_item_ns = release_total_ns = None
    if limit is not None:
        start = time.perf_counter_ns()
        iterator = iter(query)
        for i, feature in enumerate(itertools.islice(iterator, limit)):
            if i == 0:
                first_item_ns = time.perf_counter_ns() - start
            value = feature.id * 4 + (
                0 if feature.is_node else 1 if feature.is_way else 2
            )
            assert value not in wanted, "duplicate limited result"
            wanted.add(value)
        del iterator
        release_total_ns = time.perf_counter_ns() - start
        assert len(wanted) == count
    digest = None if args.skip_verification else fingerprint(query, wanted)
    if limit is not None and digest is not None:
        assert not wanted, "limited results absent from full query"
        assert count == min(limit, digest["count"])
    work_digest = (
        None
        if args.skip_verification or operation in ("iterate", "count")
        else consume(query, operation, verify=True)
    )
    status = (
        pathlib.Path("/proc/self/status").read_text()
        if pathlib.Path("/proc/self/status").exists()
        else ""
    )
    memory = [
        s
        for s in status.splitlines()
        if s.startswith(("VmRSS:", "VmHWM:", "RssAnon:", "RssFile:", "Threads:"))
    ]
    print(
        json.dumps(
            {
                "name": spec["name"],
                "query": spec["query"],
                "bounds": spec.get("bounds"),
                "engine": "python",
                "count": count,
                "operation": operation,
                "limit": limit,
                "first_item_ns": first_item_ns,
                "release_total_ns": release_total_ns,
                "membership_verified": limit is not None and not args.skip_verification,
                "work_items": work["items"],
                "work_fingerprint": work_digest,
                "fingerprint": digest,
                "samples_wall_ns": wall,
                "samples_cpu_ns": cpu,
                "samples_thread_cpu_ns": thread_cpu,
                "repeats": repeats,
                "calibration_ns": calibration_ns,
                "setup_ns": setup_ns,
                "first_execution_ns": first_ns,
                "open_ns": open_ns,
                "process": memory,
            }
        ),
        flush=True,
    )
