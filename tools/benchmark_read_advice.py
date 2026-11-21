#!/usr/bin/env python3
"""Compare Rust file-access advice with verified cache state and full traversal.

This isolates first-query execution without serializing every ID. The worker
then repeats once with warm decoder state and validates a full fingerprint.
Whole-child resource counters and post-exit residency include those later passes.
"""

from benchmark_storage import cache_environment, validate_cache

from paths import ROOT
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import pathlib
import platform
import resource
import statistics
import subprocess
import time

from benchmark_cache import CachePreparationError, FileCache
from benchmark_output import startup_specs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--names", type=pathlib.Path, required=True)
    parser.add_argument("--rust",default=str(ROOT/"target/safe/release/examples/query_benchmark"))
    parser.add_argument(
        "--storage", choices=["paged", "windowed", "file", "cached"], default="paged"
    )
    parser.add_argument(
        "--advice",
        nargs="+",
        choices=["default", "normal", "random"],
        default=["normal", "random"],
    )
    parser.add_argument("--cache", choices=["warm", "cold"], default="cold")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--affinity", choices=["all", "one", "both"], default="both")
    parser.add_argument("--fincore", default="fincore")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--cache-mib", type=int, default=16)
    args = parser.parse_args()
    rust_cache_env = cache_environment(args.storage, args.cache_mib)
    if (
        args.samples < 1
        or args.threads < 0
        or len(args.advice) != len(set(args.advice))
    ):
        parser.error("invalid sample/worker/advice settings")
    reference = json.loads(args.reference.read_text())
    specs, expected = startup_specs(reference, json.loads(args.names.read_text()))
    with args.file.open("rb") as file:
        file_hash = hashlib.file_digest(file, "sha256").hexdigest()
    if file_hash != reference["file_sha256"]:
        parser.error("input differs from the validated reference dataset")
    cache = FileCache(args.file, args.fincore)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    resolved = args.output.with_suffix(".queries.json")
    resolved.write_text(json.dumps(specs, indent=2) + "\n")
    paths = [ROOT/"benchmarks/regol-sources.json"]
    paths += sorted((ROOT/"examples").rglob("*.rs"))
    paths += sorted((ROOT/"tools").glob("*benchmark*.py"))
    report = {
        "method": __doc__,
        "rust_cache_mib": args.cache_mib if args.storage == "cached" else None,
        "platform": platform.platform(),
        "file": str(args.file),
        "file_bytes": args.file.stat().st_size,
        "file_sha256": file_hash,
        "reference": str(args.reference),
        "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
        "rust_binary_sha256": hashlib.sha256(
            pathlib.Path(args.rust).read_bytes()
        ).hexdigest(),
        "git_commit": subprocess.check_output(
            ["git","-C",ROOT,"rev-parse","HEAD"], text=True
        ).strip(),
        "storage": args.storage,
        "advice": args.advice,
        "cache": args.cache,
        "cache_verification": cache.metadata,
        "samples": args.samples,
        "rust_threads": args.threads,
        "queries": specs,
        "results": [],
    }
    available = sorted(os.sched_getaffinity(0))
    plans = []
    if args.affinity in ("all", "both"):
        plans.append(("all", available))
    if args.affinity in ("one", "both"):
        plans.append(("one", available[:1]))
    for budget, (affinity, cpus) in enumerate(plans):
        for ordinal, spec in enumerate(specs):
            name = spec["name"]
            for sample in range(args.samples):
                # Rotate and reverse order so a setting is not always first.
                offset = (sample + ordinal + budget) % len(args.advice)
                order = args.advice[offset:] + args.advice[:offset]
                if (sample + ordinal + budget) % 2:
                    order = list(reversed(order)) if len(order) > 2 else order
                for advice in order:
                    env = rust_cache_env | {"GOL_READER_BENCH_READ_ADVICE": advice}
                    command = [
                        "taskset",
                        "-c",
                        ",".join(map(str, cpus)),
                        args.rust,
                        str(args.file),
                        str(resolved),
                        args.storage,
                        "1",
                        "0",
                        name,
                        "1",
                        "verify",
                        str(args.threads if affinity == "all" else 0),
                    ]
                    with ExitStack() as cache_context:
                        try:
                            cache_before = cache_context.enter_context(cache.sample(args.cache))
                        except CachePreparationError as error:
                            report["cache_failure"] = {
                                "name": name,
                                "advice": advice,
                                "affinity": affinity,
                                "sample": sample,
                                "attempts": error.attempts,
                            }
                            args.output.with_suffix(".cache-failure.json").write_text(
                                json.dumps(report, indent=2) + "\n"
                            )
                            raise
                        before = resource.getrusage(resource.RUSAGE_CHILDREN)
                        start = time.perf_counter_ns()
                        process = subprocess.run(
                            command, env=env, capture_output=True, check=True
                        )
                        elapsed = time.perf_counter_ns() - start
                        after = resource.getrusage(resource.RUSAGE_CHILDREN)
                        cache_after = cache.snapshot()
                    worker = json.loads(process.stdout)
                    validate_cache(worker, args.storage, args.cache_mib)
                    if (
                        process.stderr
                        or worker["name"] != name
                        or worker.get("read_advice") != advice
                        or worker["fingerprint"] != expected[name]
                        or worker["count"] != expected[name]["count"]
                        or worker["repeats"] != 1
                        or len(worker["samples_wall_ns"]) != 1
                    ):
                        raise ValueError("invalid worker result or changed fingerprint")
                    if worker["build_features"]["mmap"]:
                        raise ValueError("read-advice requires the safe-build control: use gol-bench setup safe-worker")
                    report["results"].append(
                        {
                            "name": name,
                            "affinity": affinity,
                            "cpus": cpus,
                            "advice": advice,
                            "sample": sample,
                            "command": command,
                            "process_wall_ns": elapsed,
                            "child_cpu_ns": round(
                                (
                                    (after.ru_utime + after.ru_stime)
                                    - (before.ru_utime + before.ru_stime)
                                )
                                * 1e9
                            ),
                            "child_input_blocks": after.ru_inblock - before.ru_inblock,
                            "child_major_faults": after.ru_majflt - before.ru_majflt,
                            "os_cache_before": cache_before,
                            "os_cache_after": cache_after,
                            "worker": worker,
                        }
                    )
                    args.output.with_suffix(".partial.json").write_text(
                        json.dumps(report, indent=2) + "\n"
                    )
            for advice in args.advice:
                rows = [
                    r
                    for r in report["results"]
                    if r["affinity"] == affinity
                    and r["name"] == name
                    and r["advice"] == advice
                ]
                first_ms = (
                    statistics.median(r["worker"]["first_execution_ns"] for r in rows)
                    / 1e6
                )
                print(
                    f"{affinity} {name} {advice}: {first_ms:.3f} ms first query",
                    flush=True,
                )
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".partial.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
