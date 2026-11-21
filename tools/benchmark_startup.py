#!/usr/bin/env python3
"""Fresh-process, equal-output comparison with warm or verified-cold GOL pages."""

from benchmark_storage import cache_environment, validate_cache

from paths import ROOT, WORKER
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

from benchmark_output import fingerprint_ids, startup_specs
from benchmark_cache import CachePreparationError, FileCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument(
        "--names", type=pathlib.Path, default=ROOT/"benchmarks/uk-startup.json"
    )
    parser.add_argument("--python", required=True)
    parser.add_argument("--rust", default=str(WORKER))
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--storage",
        choices=["paged", "file", "windowed", "cached", "mmap-immutable"],
        default="paged",
    )
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--affinity", choices=["all", "one", "both"], default="both")
    parser.add_argument(
        "--read-advice",
        choices=["default", "normal", "random"],
        default="default",
        help="Linux file-access advice for the actual Rust read descriptor",
    )
    parser.add_argument("--cache", choices=["warm", "cold"], default="warm")
    parser.add_argument(
        "--verify-cache",
        action="store_true",
        help="also verify full input residency before warm samples; cold always verifies zero",
    )
    parser.add_argument(
        "--fincore", default="fincore", help="residency observer executable"
    )
    parser.add_argument("--cache-mib", type=int, default=16)
    args = parser.parse_args()
    if args.samples < 1 or args.threads < 0:
        parser.error("invalid sample/worker budget")
    if args.storage == "mmap-immutable" and args.read_advice != "default":
        parser.error("mmap requires default read advice")
    worker_env = cache_environment("python", args.cache_mib)
    worker_env.pop("GOL_READER_BENCH_READ_ADVICE", None)
    rust_env = cache_environment(args.storage, args.cache_mib, worker_env) | {
        "GOL_READER_BENCH_READ_ADVICE": args.read_advice
    }
    reference = json.loads(args.reference.read_text())
    specs, expected = startup_specs(reference, json.loads(args.names.read_text()))
    # Hashing pre-reads the file. Cold mode evicts and verifies it again before
    # every timed child; warm mode can additionally verify full residency.
    with args.file.open("rb") as file:
        file_hash = hashlib.file_digest(file, "sha256").hexdigest()
    if file_hash != reference["file_sha256"]:
        parser.error("input differs from the validated reference dataset")
    cache = (
        FileCache(args.file, args.fincore)
        if args.cache == "cold" or args.verify_cache
        else None
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    resolved = args.output.with_suffix(".queries.json")
    resolved.write_text(json.dumps(specs, indent=2) + "\n")
    source_paths = [ROOT/"benchmarks/regol-sources.json"]
    source_paths += sorted((ROOT/"examples").rglob("*.rs"))
    source_paths += sorted((ROOT/"tools").glob("*benchmark*.py"))
    source_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_paths
    }
    binary_hash = hashlib.sha256(pathlib.Path(args.rust).read_bytes()).hexdigest()
    available = sorted(os.sched_getaffinity(0))
    plans = []
    if args.affinity in ("all", "both"):
        plans.append(("all", available))
    if args.affinity in ("one", "both"):
        plans.append(("one", available[:1]))
    results = []
    cache_diagnostics = []
    for budget, (mode, cpus) in enumerate(plans):
        for ordinal, spec in enumerate(specs):
            name = spec["name"]
            group = []
            for sample in range(args.samples):
                engines = [args.storage, "python"]
                if (sample + ordinal + budget) % 2:
                    engines.reverse()
                for engine in engines:
                    prefix = ["taskset", "-c", ",".join(map(str, cpus))]
                    if engine != "python":
                        command = prefix + [
                            args.rust,
                            str(args.file),
                            str(resolved),
                            args.storage,
                            "1",
                            "0",
                            name,
                            "1",
                            "skip",
                            str(args.threads if mode == "all" else 0),
                            "ids-json",
                        ]
                    else:
                        command = prefix + [
                            args.python,
                            str(ROOT/"tools/query_benchmark_python.py"),
                            str(args.file),
                            str(resolved),
                            "--only",
                            name,
                            "--once-json",
                        ]
                    # Observer subprocesses and eviction are outside both the
                    # timed interval and the child resource-usage delta.
                    with ExitStack() as cache_context:
                        try:
                            cache_before = cache_context.enter_context(cache.sample(args.cache)) if cache else None
                        except CachePreparationError as error:
                            args.output.with_suffix(".cache-failure.json").write_text(
                                json.dumps(
                                    {
                                        "name": name,
                                        "engine": engine,
                                        "affinity": mode,
                                        "sample": sample,
                                        "input_cache": args.cache,
                                        "completed_samples": len(results),
                                        "error": str(error),
                                        "attempts": error.attempts,
                                    },
                                    indent=2,
                                )
                                + "\n"
                            )
                            raise
                        before = resource.getrusage(resource.RUSAGE_CHILDREN)
                        start = time.perf_counter_ns()
                        process = subprocess.run(
                            command,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=worker_env if engine == "python" else rust_env,
                        )
                        exited = time.perf_counter_ns()
                        if process.returncode:
                            raise RuntimeError(process.stderr.decode(errors="replace"))
                        ids = json.loads(process.stdout)
                        parsed = time.perf_counter_ns()
                        after = resource.getrusage(resource.RUSAGE_CHILDREN)
                        cache_after = cache.snapshot() if cache else None
                    digest = fingerprint_ids(ids)
                    if digest != expected[name]:
                        raise ValueError(
                            f"ID fingerprint mismatch: {mode}/{engine}/{name}"
                        )
                    metadata = json.loads(process.stderr)
                    if metadata["name"] != name:
                        raise ValueError("worker metadata names a different workload")
                    if (
                        engine != "python"
                        and metadata.get("read_advice") != args.read_advice
                    ):
                        raise ValueError(
                            "Rust worker did not confirm requested read advice"
                        )
                    record = {
                        "name": name,
                        "engine": engine,
                        "affinity": mode,
                        "cpus": cpus,
                        "sample": sample,
                        "command": command,
                        "process_wall_ns": exited - start,
                        "parent_parse_ns": parsed - exited,
                        "end_to_end_ns": parsed - start,
                        "child_cpu_ns": round(
                            (
                                (after.ru_utime + after.ru_stime)
                                - (before.ru_utime + before.ru_stime)
                            )
                            * 1e9
                        ),
                        "child_minor_faults": after.ru_minflt - before.ru_minflt,
                        "child_major_faults": after.ru_majflt - before.ru_majflt,
                        "child_input_blocks": after.ru_inblock - before.ru_inblock,
                        "child_output_blocks": after.ru_oublock - before.ru_oublock,
                        "os_cache_before": cache_before,
                        "os_cache_after": cache_after,
                        "output_bytes": len(process.stdout),
                        "fingerprint": digest,
                        "worker": metadata,
                    }
                    results.append(record)
                    group.append(record)
                    args.output.with_suffix(".partial.json").write_text(
                        json.dumps(results, indent=2) + "\n"
                    )
            if len({row["output_bytes"] for row in group}) != 1:
                raise ValueError(f"JSON output byte lengths differ: {name}")
            # Counting retained pages walks a size-dependent directory. Keep
            # that observer cost out of the timed fresh-process samples.
            command = next(
                row["command"] for row in group if row["engine"] != "python"
            ).copy()
            command[-1] = "ids-json-stats"
            diagnostic = subprocess.run(
                command, capture_output=True, check=True, env=rust_env
            )
            digest = fingerprint_ids(json.loads(diagnostic.stdout))
            metadata = json.loads(diagnostic.stderr)
            validate_cache(metadata, args.storage, args.cache_mib)
            if (
                digest != expected[name]
                or len(diagnostic.stdout) != group[0]["output_bytes"]
                or metadata["name"] != name
                or metadata.get("read_advice") != args.read_advice
            ):
                raise ValueError("untimed cache diagnostic produced different output")
            cache_diagnostics.append(
                {
                    "name": name,
                    "affinity": mode,
                    "command": command,
                    "fingerprint": digest,
                    "output_bytes": len(diagnostic.stdout),
                    "worker": metadata,
                }
            )
            for engine in [args.storage, "python"]:
                median = statistics.median(
                    row["end_to_end_ns"] for row in group if row["engine"] == engine
                )
                print(
                    f"{mode:3} {engine:6} {name:25} {median/1e6:9.3f} ms end-to-end",
                    flush=True,
                )
    report = {
        "method": "Fresh process per sample, alternating engine order. Input is initially pre-read for hashing. Cold mode requests per-file eviction and requires zero resident GOL pages immediately before every timed worker. Verified warm mode requires all GOL pages resident. Residency snapshots/eviction are outside timing and child resource deltas; snapshots are not locks. Executables, libraries and storage-device caches are not flushed. Each worker opens the library, prepares one query, iterates all results and emits the same compact JSON ID-string array. Parent receives output, waits for child exit and parses JSON inside end-to-end timing. Typed-ID fingerprints and duplicate checks run afterwards. Rust streams through BufWriter; Python uses its native JSON encoder on a list of ID strings. Lightweight diagnostic reporting and teardown are included in parent process time. Storage diagnostics run separately without cache reset, whose output is also validated; paged-storage diagnostics scan the cache directory. Worker execute_write_ns combines traversal, serialization and output flushing; it does not isolate pure query time. Child CPU, faults and native getrusage block counters include process startup and teardown. No result caching or count shortcuts.",
        "platform": platform.platform(),
        "file": str(args.file),
        "file_bytes": args.file.stat().st_size,
        "file_sha256": file_hash,
        "reference": str(args.reference),
        "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        "source_sha256": source_hashes,
        "rust_binary_sha256": binary_hash,
        "python_version": subprocess.check_output(
            [args.python, "--version"], text=True
        ).strip(),
        "geodesk_version": subprocess.check_output(
            [
                args.python,
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('geodesk'))",
            ],
            text=True,
        ).strip(),
        "git_commit": subprocess.check_output(
            ["git","-C",ROOT,"rev-parse","HEAD"], text=True
        ).strip(),
        "samples": args.samples,
        "rust_threads": args.threads,
        "rust_storage": args.storage,
        "rust_cache_mib": args.cache_mib if args.storage == "cached" else None,
        "rust_read_advice": args.read_advice,
        "input_cache": args.cache,
        "cache_verification": cache.metadata if cache else None,
        "queries": specs,
        "results": results,
        "cache_diagnostics": cache_diagnostics,
        "validation_failures": [],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".partial.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
