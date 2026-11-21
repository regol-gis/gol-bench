#!/usr/bin/env python3
"""Compare persistent query workers under verified cgroup v2 memory limits.

Requires Linux, the user's systemd manager and fincore. GOL pages are evicted
before every service so an unrelated cgroup cannot supply its precharged cache.
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import uuid

from benchmark_cache import FileCache
from benchmark_storage import cache_capacity, validate_cache
from benchmark_memory_worker import require_limits

ROOT = pathlib.Path(__file__).resolve().parent.parent


def sha256(path):
    with pathlib.Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def service_command(
    unit, command, limit, cpus, timeout, cache_mib=16, read_advice="normal"
):
    cache_capacity(cache_mib)
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--wait",
        "--collect",
        "--pipe",
        "--service-type=exec",
        f"--unit={unit}",
        f"--working-directory={ROOT}",
        "--property=MemoryAccounting=yes",
        f"--property=MemoryMax={limit}",
        "--property=MemorySwapMax=0",
        "--property=OOMPolicy=continue",
        f"--property=RuntimeMaxSec={timeout + 10}s",
        "--property=TimeoutStopSec=5s",
        f"--setenv=GOL_READER_BENCH_READ_ADVICE={read_advice}",
        f"--setenv=GOL_READER_BENCH_CACHE_BYTES={cache_mib * 1024**2}",
        sys.executable,
        str(ROOT / "tools/benchmark_memory_worker.py"),
        "--limit-bytes",
        str(limit),
        "--timeout",
        str(timeout),
        "--cpus",
        ",".join(map(str, cpus)),
        "--",
        *command,
    ]


def cleanup(unit):
    command = ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"]
    state = subprocess.run(command, capture_output=True, text=True, check=True)
    actions = []
    if state.stdout.strip() != "not-found":
        for verb in ["stop", "reset-failed"]:
            result = subprocess.run(
                ["systemctl", "--user", verb, unit], capture_output=True, text=True
            )
            actions.append(
                {"verb": verb, "returncode": result.returncode, "stderr": result.stderr}
            )
        state = subprocess.run(command, capture_output=True, text=True, check=True)
    if state.stdout.strip() != "not-found":
        raise RuntimeError(f"benchmark unit was not collected: {unit}")
    return {"command": command, "state": state.stdout.strip(), "actions": actions}


def run_limited(command, limit, cpus, timeout, cache_mib=16, read_advice="normal"):
    unit = "gol-memory-" + uuid.uuid4().hex + ".service"
    launch = service_command(
        unit, command, limit, cpus, timeout, cache_mib, read_advice
    )
    record = {"unit": unit, "service_command": launch}
    try:
        result = subprocess.run(
            launch, capture_output=True, text=True, timeout=timeout + 25
        )
        record.update(
            service_returncode=result.returncode,
            service_stderr=result.stderr,
            service_stdout=result.stdout,
        )
        if result.returncode != 0:
            raise RuntimeError("memory supervisor/service failed")
        supervisor = json.loads(result.stdout)
        for phase in ["before", "after"]:
            require_limits(supervisor[phase], limit)
        if supervisor["affinity"] != cpus or supervisor["command"] != command:
            raise ValueError("unexpected supervisor command or CPU affinity")
        record["supervisor"] = supervisor
    except Exception as error:
        record["infrastructure_error"] = repr(error)
    finally:
        try:
            record["cleanup"] = cleanup(unit)
        except Exception as error:
            record["cleanup_error"] = repr(error)
    return record


def validate_worker(supervisor, expected, engine, samples, threads, cache_mib=16):
    if supervisor["outcome"] in {"oom", "timeout"}:
        return None
    if supervisor["outcome"] != "success":
        raise ValueError("worker failed without an OOM or timeout")
    if supervisor["stderr"]:
        raise ValueError("successful worker emitted unexpected stderr")
    row = json.loads(supervisor["stdout"])
    if row["engine"] != engine or row["name"] != expected["name"]:
        raise ValueError("unexpected worker workload or engine")
    for key in ["count", "fingerprint", "work_fingerprint"]:
        if row[key] != expected[key]:
            raise ValueError(f"worker {key} disagrees with reference")
    if len(row["samples_wall_ns"]) != samples or row["repeats"] != 1:
        raise ValueError("unexpected sample count or repetition policy")
    if engine != "python":
        if row["threads"] != threads or row["build_features"]["mmap"] != (
            engine == "mmap-immutable"
        ):
            raise ValueError("unexpected Rust execution configuration")
        if row["read_advice"] != (
            "default" if engine == "mmap-immutable" else "normal"
        ):
            raise ValueError("unexpected Rust read advice")
    validate_cache(row, engine, cache_mib)
    return row


def reference_results(report,names):
    selected=set(names)
    expected={}
    for row in report['results']:
        name=row['name']
        if name not in selected or row['engine']!='paged':
            continue
        anchor=expected.setdefault(name,row)
        if any(anchor[key]!=row[key] for key in ('count','fingerprint','work_fingerprint')):
            raise ValueError(f'paged reference differs between CPU affinities for {name}')
    if missing:=selected-expected.keys():
        raise ValueError(f'missing paged reference for {sorted(missing)}')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=pathlib.Path)
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--windowed-reference", type=pathlib.Path, required=True)
    parser.add_argument("--paged", type=pathlib.Path, required=True)
    parser.add_argument("--windowed", type=pathlib.Path, required=True)
    parser.add_argument("--python", type=pathlib.Path, required=True)
    parser.add_argument(
        "--mmap",
        type=pathlib.Path,
        help="Experimental: input must remain immutable for every worker lifetime",
    )
    parser.add_argument("--mmap-reference", type=pathlib.Path)
    parser.add_argument("--cached", type=pathlib.Path)
    parser.add_argument("--cached-reference", type=pathlib.Path)
    parser.add_argument("--cache-mib", type=int, default=16)
    parser.add_argument("--names", type=pathlib.Path, required=True)
    parser.add_argument("--limits-mib", type=int, nargs="+", default=[128, 512, 4096])
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--warm-samples", type=int, default=3)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if (
        min(
            args.limits_mib
            + [args.samples, args.warm_samples, args.threads, args.timeout]
        )
        <= 0
    ):
        parser.error("limits, samples, threads and timeout must be positive")
    cache_capacity(args.cache_mib)
    if bool(args.cached) != bool(args.cached_reference):
        parser.error("--cached and --cached-reference must be supplied together")
    if bool(args.mmap) != bool(args.mmap_reference):
        parser.error("--mmap and --mmap-reference must be supplied together")
    # Preserve the Python executable's symlink: resolving it bypasses pyvenv.cfg.
    args.python = args.python.absolute()
    args.file = args.file.resolve()
    reference = json.loads(args.reference.read_text())
    buffered_reference = json.loads(args.windowed_reference.read_text())
    binaries = {"paged": args.paged.resolve(), "windowed": args.windowed.resolve()}
    references = {"paged": reference, "windowed": buffered_reference}
    reference_paths = [args.reference, args.windowed_reference]
    if args.cached:
        binaries["cached"] = args.cached.resolve()
        references["cached"] = json.loads(args.cached_reference.read_text())
        reference_paths.append(args.cached_reference)
    if args.mmap:
        binaries["mmap-immutable"] = args.mmap.resolve()
        references["mmap-immutable"] = json.loads(args.mmap_reference.read_text())
        reference_paths.append(args.mmap_reference)
    for storage, ref in references.items():
        if sha256(binaries[storage]) != ref["rust_binary_sha256"]:
            raise ValueError(f"{storage} binary does not match reference provenance")
    file_hash = sha256(args.file)
    if any(file_hash != ref["file_sha256"] for ref in references.values()):
        raise ValueError("input does not match reference reports")
    names = json.loads(args.names.read_text())
    if not names or len(set(names)) != len(names):
        raise ValueError("unique workload names are required")
    queries = [
        next(q for q in reference["queries"] if q["name"] == name) for name in names
    ]
    expected = reference_results(reference,names)
    bad_names = {
        r["name"] for ref in references.values() for r in ref["validation_failures"]
    }
    if bad_names.intersection(names):
        raise ValueError("selected workload has an unresolved reference disagreement")
    cpus = sorted(os.sched_getaffinity(0))
    cache = FileCache(args.file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    spec = args.output.with_suffix(".queries.json").resolve()
    spec.write_text(json.dumps(queries, indent=2) + "\n")
    report = {
        "method": "Fresh cgroup per worker; zero GOL residency verified before launch; memory.max enforced, swap disabled. Supervisor footprint is included in the budget. First execution and the requested fixed repeat samples occur in one persistent worker, followed by result verification. A failed worker may fail during setup, execution or verification: it supplies no usable query latency. Cgroup peak/counters include the complete worker and supervisor, not just timed queries. No native-count shortcuts. Shared non-GOL code pages may be charged elsewhere. Repeat samples are not guaranteed a fully resident working set. Cold refers to observed GOL residency, not device-cache state.",
        "command": sys.argv,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python_executable": str(args.python.absolute()),
        "python_version": subprocess.check_output(
            [str(args.python.absolute()), "--version"], text=True
        ).strip(),
        "geodesk_version": subprocess.check_output(
            [
                str(args.python.absolute()),
                "-c",
                'import importlib.metadata; print(importlib.metadata.version("geodesk"))',
            ],
            text=True,
        ).strip(),
        "cpu_model": next(
            line.split(":", 1)[1].strip()
            for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "file": str(args.file),
        "file_sha256": file_hash,
        "file_bytes": args.file.stat().st_size,
        "queries": queries,
        "cpus": cpus,
        "threads": args.threads,
        "cache_mib": args.cache_mib if args.cached else None,
        "limits_mib": args.limits_mib,
        "samples": args.samples,
        "warm_samples": args.warm_samples,
        "timeout_seconds": args.timeout,
        "binary_sha256": {key: sha256(path) for key, path in binaries.items()},
        "reference_sha256": {str(p): sha256(p) for p in reference_paths},
        "harness_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in sorted((ROOT / "tools").glob("benchmark*.py"))
            + [ROOT / "tools/query_benchmark_python.py"]
        },
        "cache_verification": cache.metadata,
        "systemd_version": subprocess.check_output(
            ["systemd-run", "--version"], text=True
        ),
        "platform": dict(
            zip(["sysname", "nodename", "release", "version", "machine"], os.uname())
        ),
        "results": [],
        "validation_failures": [],
    }
    partial = args.output.with_suffix(".partial.json")
    engines = list(binaries) + ["python"]
    try:
        for limit_mib in args.limits_mib:
            for ordinal, name in enumerate(names):
                for sample in range(args.samples):
                    offset = (ordinal + sample) % len(engines)
                    for engine in engines[offset:] + engines[:offset]:
                        before = cache.prepare("cold")
                        if engine == "python":
                            command = [
                                str(args.python.absolute()),
                                str(ROOT / "tools/query_benchmark_python.py"),
                                str(args.file),
                                str(spec),
                                "--samples",
                                str(args.warm_samples),
                                "--target-ms",
                                "0",
                                "--repeats",
                                "1",
                                "--only",
                                name,
                            ]
                        else:
                            command = [
                                str(binaries[engine]),
                                str(args.file),
                                str(spec),
                                engine,
                                str(args.warm_samples),
                                "0",
                                name,
                                "1",
                                "verify",
                                str(args.threads),
                            ]
                        row = {
                            "engine": engine,
                            "name": name,
                            "limit_mib": limit_mib,
                            "sample": sample,
                            "os_cache_before": before,
                        }
                        row.update(
                            run_limited(
                                command,
                                limit_mib * 1024**2,
                                cpus,
                                args.timeout,
                                args.cache_mib,
                                "default" if engine == "mmap-immutable" else "normal",
                            )
                        )
                        row["os_cache_after"] = cache.snapshot()
                        report["results"].append(row)
                        if "infrastructure_error" in row or "cleanup_error" in row:
                            raise RuntimeError(
                                "cgroup infrastructure failed; inspect partial report"
                            )
                        try:
                            row["worker"] = validate_worker(
                                row["supervisor"],
                                expected[name],
                                engine,
                                args.warm_samples,
                                args.threads,
                                args.cache_mib,
                            )
                        except (ValueError, KeyError) as error:
                            report["validation_failures"].append(
                                {
                                    "engine": engine,
                                    "name": name,
                                    "limit_mib": limit_mib,
                                    "sample": sample,
                                    "error": str(error),
                                }
                            )
                        partial.write_text(json.dumps(report, indent=2) + "\n")
                        print(
                            limit_mib,
                            name,
                            sample,
                            engine,
                            row["supervisor"]["outcome"],
                            flush=True,
                        )
    except Exception as error:
        report["infrastructure_error"] = repr(error)
        partial.write_text(json.dumps(report, indent=2) + "\n")
        raise
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    partial.unlink()
    return bool(report["validation_failures"])


if __name__ == "__main__":
    sys.exit(main())
