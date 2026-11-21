#!/usr/bin/env python3
"""Supervise one benchmark inside an already configured cgroup v2 service.

The supervisor shares the memory budget and remains alive after a child OOM so
it can record the controller's counters. Its footprint is included, not subtracted.
"""

import argparse
import json
import os
import pathlib
import signal
import subprocess
import time


def parse_counters(text):
    counters = {}
    for line in text.splitlines():
        key, value = line.split()
        if key in counters or not value.isdecimal():
            raise ValueError("invalid cgroup counters")
        counters[key] = int(value)
    return counters


def cgroup_path(text):
    entries = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    if len(entries) != 1 or not entries[0].startswith("/"):
        raise ValueError("one unified cgroup entry is required")
    path = pathlib.PurePosixPath(entries[0])
    if ".." in path.parts or path == pathlib.PurePosixPath("/"):
        raise ValueError("a non-root cgroup is required")
    return pathlib.Path("/sys/fs/cgroup") / str(path).lstrip("/")


def snapshot(root):
    result = {}
    for name in [
        "memory.max",
        "memory.swap.max",
        "memory.current",
        "memory.peak",
        "memory.swap.current",
        "memory.events",
        "memory.stat",
        "cpu.stat",
    ]:
        result[name] = (root / name).read_text().strip()
    for name in ["io.stat", "memory.pressure"]:
        try:
            result[name] = (root / name).read_text().strip()
        except FileNotFoundError:
            result[name] = None
    return result


def require_limits(state, limit):
    if state["memory.max"] != str(limit) or state["memory.swap.max"] != "0":
        raise ValueError("requested memory/swap limits are not enforced")
    if int(state["memory.swap.current"]) != 0:
        raise ValueError("worker cgroup has swap usage")


def classify(returncode, timed_out, before, after):
    old = parse_counters(before["memory.events"])
    new = parse_counters(after["memory.events"])
    if new["oom_kill"] < old["oom_kill"]:
        raise ValueError("OOM counter went backwards")
    if new["oom_kill"] > old["oom_kill"]:
        return "oom"
    if timed_out:
        return "timeout"
    return "success" if returncode == 0 else "worker_error"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-bytes", type=int, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--cpus", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.limit_bytes <= 0 or args.timeout <= 0 or not command:
        parser.error("positive limits and a child command are required")
    cpus = {int(cpu) for cpu in args.cpus.split(",")}
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != cpus:
        raise ValueError("CPU affinity was not applied")
    root = cgroup_path(pathlib.Path("/proc/self/cgroup").read_text())
    before = snapshot(root)
    require_limits(before, args.limit_bytes)
    started = time.perf_counter_ns()
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = child.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Only the child and its descendants belong to this new process group.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = child.communicate()
    elapsed = time.perf_counter_ns() - started
    after = snapshot(root)
    require_limits(after, args.limit_bytes)
    print(
        json.dumps(
            {
                "cgroup": str(root),
                "affinity": sorted(os.sched_getaffinity(0)),
                "command": command,
                "before": before,
                "after": after,
                "child_returncode": child.returncode,
                "timed_out": timed_out,
                "outcome": classify(child.returncode, timed_out, before, after),
                "child_process_wall_ns": elapsed,
                "stdout": stdout,
                "stderr": stderr,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
