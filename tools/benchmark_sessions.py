"""Time fixed-order sessions; retain independent validation for every step."""

import itertools
import pathlib
import time

from benchmark_workloads import consume


def run_session(library, box, spec, args, fingerprint):
    start = time.perf_counter_ns()
    steps = spec["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("a session needs at least one step")
    prepared = []
    for step in steps:
        query = library(step["query"])
        if "bounds" in step:
            query = query(box(*step["bounds"]))
        operation = step.get("operation", "iterate")
        limit = step.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or limit < 0 or operation != "iterate"
        ):
            raise ValueError("limits require nonnegative counts and handle iteration")
        prepared.append((query, operation, limit))
    setup_ns = time.perf_counter_ns() - start

    def execute():
        return [
            consume(
                query if limit is None else itertools.islice(query, limit), operation
            )
            for query, operation, limit in prepared
        ]

    start = time.perf_counter_ns()
    work = execute()
    first_ns = time.perf_counter_ns() - start
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
    digests = []
    work_digests = []
    step_results = []
    for step, (query, operation, limit), expected in zip(steps, prepared, work):
        wanted = set()
        if limit is not None:
            for feature in itertools.islice(query, limit):
                value = feature.id * 4 + (
                    0 if feature.is_node else 1 if feature.is_way else 2
                )
                assert value not in wanted, "duplicate limited result"
                wanted.add(value)
            assert len(wanted) == expected["features"]
        digest = None if args.skip_verification else fingerprint(query, wanted)
        if limit is not None and digest is not None:
            assert not wanted, "limited results absent from full query"
            assert expected["features"] == min(limit, digest["count"])
        digests.append(digest)
        work_digests.append(
            None
            if args.skip_verification or operation == "iterate"
            else consume(query, operation, verify=True)
        )
        step_results.append(
            {
                "name": step.get("name"),
                "count": expected["features"],
                "work_items": expected["items"],
                "membership_verified": limit is not None and not args.skip_verification,
            }
        )
    status = pathlib.Path("/proc/self/status")
    memory = [
        s
        for s in (status.read_text() if status.exists() else "").splitlines()
        if s.startswith(("VmRSS:", "VmHWM:", "RssAnon:", "RssFile:", "Threads:"))
    ]
    return {
        "name": spec["name"],
        "query": None,
        "bounds": None,
        "engine": "python",
        "count": sum(w["features"] for w in work),
        "work_items": sum(w["items"] for w in work),
        "operation": "session",
        "limit": None,
        "first_item_ns": None,
        "release_total_ns": None,
        "membership_verified": False,
        "step_results": step_results,
        "fingerprint": (
            None
            if args.skip_verification
            else {"count": sum(d["count"] for d in digests), "steps": digests}
        ),
        "work_fingerprint": None if args.skip_verification else {"steps": work_digests},
        "samples_wall_ns": wall,
        "samples_cpu_ns": cpu,
        "samples_thread_cpu_ns": thread_cpu,
        "repeats": repeats,
        "calibration_ns": calibration_ns,
        "setup_ns": setup_ns,
        "first_execution_ns": first_ns,
        "process": memory,
    }


def delivered_count(spec, record):
    """Derive delivered counts from full-query digests, checking each session step."""
    if "steps" not in spec:
        expected = record["fingerprint"]["count"]
        limit = spec.get("limit")
        if limit is not None:
            expected = min(limit, expected)
            if not record.get("membership_verified"):
                raise ValueError("unverified limited results")
        if record["count"] != expected:
            raise ValueError("invalid delivered count")
        return expected
    steps = spec["steps"]
    digests = record["fingerprint"]["steps"]
    results = record["step_results"]
    if len(steps) != len(digests) or len(steps) != len(results):
        raise ValueError("invalid session step count")
    total = 0
    for step, digest, result in zip(steps, digests, results):
        total += delivered_count(step, {**result, "fingerprint": digest})
    if total != record["count"] or record["fingerprint"]["count"] != sum(
        d["count"] for d in digests
    ):
        raise ValueError("invalid session total")
    return total
