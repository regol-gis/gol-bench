#!/usr/bin/env python3
"""Build and measure the GeoDesk PostgreSQL 16 foreign data wrapper."""
from provenance import harness_source
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tarfile
import tempfile
import time

from benchmark_workloads import MASK, mix
from input_identity import identify, unchanged
from paths import ROOT, SDK
from setup_engines import checkout, checkout_at, fetch
from worker_identity import identities

ADAPTERS = ROOT / "tools/stack_benchmark"
IMAGE = "gol-benchmark-fdw-postgres16:local"
SOURCE = SDK / "fdw-postgres16"
GEODESK = "f0f0b5b88a44876ff8157c723aa063bd1e6fabd5"
GTL = "4c0a7a1d4a86143247b6ea7a5e4c7d6104d78590"
CATCH = "fa43b77429ba76c462b1898d6cd2f2d7a9416b14"
POSTGIS = "3.4.1"
POSTGIS_HASH = "fef6a141213d0ff27bf45b33b849cc396c22ddb1ffc6fed435469c9e891fc81d"


def mount(source, target, readonly=True):
    return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]


def setup(args):
    checkout("hjanetzek/geodesk-fdw", SOURCE)
    patch = ADAPTERS / "fdw-postgres16.patch"
    git = ["git", "-C", SOURCE, "apply"]
    if subprocess.run([*git, "--reverse", "--check", patch], capture_output=True).returncode:
        subprocess.run([*git, patch], check=True)
    engine = SOURCE / "deps/libgeodesk"
    checkout_at("https://github.com/clarisma/libgeodesk", GEODESK, engine)
    checkout_at("https://github.com/greg7mdp/gtl", GTL, engine / "build/_deps/gtl-src")
    checkout_at("https://github.com/catchorg/Catch2", CATCH, engine / "build/_deps/catch2-src")
    archive = SDK / f"postgis-{POSTGIS}.tar.gz"
    fetch(f"https://download.osgeo.org/postgis/source/{archive.name}", archive, POSTGIS_HASH)
    postgis = SOURCE / "deps/postgis"
    if not postgis.exists():
        with tempfile.TemporaryDirectory(dir=postgis.parent) as tmp:
            with tarfile.open(archive) as stream:
                stream.extractall(tmp, filter="data")
            (Path(tmp) / f"postgis-{POSTGIS}").rename(postgis)
    if args.image == IMAGE:
        subprocess.run(["docker", "build", "-f", ADAPTERS / "fdw.Dockerfile",
                        "-t", args.image, ADAPTERS], check=True)
    subprocess.run(["docker", "run", "--rm", "--network", "none",
                    "--user", f"{os.getuid()}:{os.getgid()}", "-w", "/work",
                    *mount(SOURCE, "/work", readonly=False),
                    *mount(ADAPTERS / "fdw_build.sh", "/build.sh"),
                    args.image, "sh", "/build.sh", str(args.jobs)], check=True)


def sql_literal(value):
    if any(char in value for char in "\r\n\0"):
        raise ValueError("FDW queries must be single-line strings")
    return "'" + value.replace("'", "''") + "'"


def workload(queries, samples, timeout):
    commands = [
        r"\echo connected",
        "SHOW server_version;",
        r"\timing on",
        "SET client_min_messages = warning;",
        f"SET statement_timeout = '{timeout}s';",
        "CREATE EXTENSION postgis;",
        "CREATE EXTENSION geodesk_fdw;",
        "CREATE SERVER features FOREIGN DATA WRAPPER geodesk_fdw "
        "OPTIONS (datasource '/input.gol');",
        """
CREATE FUNCTION pg_temp.bench_explain(statement text) RETURNS jsonb
LANGUAGE plpgsql AS $body$
DECLARE plan json;
BEGIN
    EXECUTE 'EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) ' || statement INTO plan;
    RETURN plan::jsonb;
END;
$body$;
""",
    ]
    for index, query in enumerate(queries):
        table = f"features_{index}"
        commands.extend([
            rf"\echo table {index}",
            f"CREATE FOREIGN TABLE {table} (fid bigint, type integer) SERVER features "
            f"OPTIONS (query {sql_literal(query)});",
            r"\timing off",
        ])
        for operation, columns in (("iterate", "type, fid"), ("count", "count(*)")):
            commands.append(rf"\echo {operation} {index}")
            statement = sql_literal(f"SELECT {columns} FROM {table}")
            commands.extend([f"SELECT pg_temp.bench_explain({statement});"] * (samples + 1))
        commands.extend([
            rf"\echo scalar {index}",
            f"SELECT count(*) FROM {table};",
            rf"\echo ids {index}",
            f"SELECT type, fid FROM {table};",
            r"\timing on",
        ])
    commands.append(r"\echo done")
    return "\n".join(commands) + "\n"


def read_session(stream, started, queries):
    session = {}
    rows = [{"query": query, "iterate": [], "count": []} for query in queries]
    state = ""
    for line in stream:
        line = line.strip()
        if line == "connected":
            session["container_to_first_psql_output_ns"] = time.perf_counter_ns() - started
            session["setup_statement_client_ns"] = []
            state = "version"
        elif state == "version":
            session["postgres_version"] = line
            state = "setup"
        elif line == "done":
            if state == "ids":
                row["fingerprint"] = {"count": count, "sum": str(total), "xor": str(xor)}
            return session, rows
        elif line.startswith("Time: "):
            elapsed = round(float(line.split()[1]) * 1_000_000)
            if state == "setup":
                session["setup_statement_client_ns"].append(elapsed)
            elif state == "table":
                row["table_setup_client_ns"] = elapsed
        elif line and line.split()[0] in ("table", "iterate", "count", "scalar", "ids"):
            if state == "ids":
                row["fingerprint"] = {"count": count, "sum": str(total), "xor": str(xor)}
            state, index = line.split()
            row = rows[int(index)]
            if state == "ids":
                count = total = xor = 0
        elif state in ("iterate", "count"):
            plan, = json.loads(line)
            scan = plan["Plan"]
            if state == "count":
                scan, = scan["Plans"]
            if scan["Node Type"] != "Foreign Scan":
                raise ValueError(f"unexpected SQL plan: {scan['Node Type']}")
            row[state].append({
                "execution_ns": round(plan["Execution Time"] * 1_000_000),
                "planning_ns": round(plan["Planning Time"] * 1_000_000),
                "actual_rows": plan["Plan"]["Actual Rows"],
                "scanned_rows": scan["Actual Rows"],
            })
        elif state == "scalar":
            row["sql_count"] = int(line)
        elif state == "ids":
            kind, fid = map(int, line.split("|"))
            if kind not in (0, 1, 2) or not 0 <= fid < (1 << 52):
                raise ValueError(f"invalid FDW typed identity: {line}")
            value = mix(fid * 4 + kind)
            count += 1
            total = (total + value) & MASK
            xor ^= value
        else:
            raise ValueError(f"unexpected psql output: {line}")
    raise ValueError("incomplete PostgreSQL session")


def python_results(args, queries, temporary, cpu):
    specs = temporary / "reference.json"
    specs.write_text(json.dumps([
        {"name": f"{index}-{operation}", "operation": operation, "query": query}
        for index, query in enumerate(queries)
        for operation in ("iterate", "count")
    ]))
    output = subprocess.check_output(
        ["taskset", "-c", str(cpu), args.python, ROOT / "tools/query_benchmark_python.py",
         args.file, specs, "--repeats", "1", "--samples", str(args.samples)],
        text=True,
    )
    return {result["name"]: result for result in map(json.loads, output.splitlines())}


def run(args):
    queries = args.query if args.query else ["n", "w", "r", "*"]
    identity = identify(args.file)
    cpu = min(os.sched_getaffinity(0))
    image = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], text=True
    ).strip()
    module = SOURCE / "geodesk_fdw.so"
    with module.open("rb") as source:
        module_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    python_identity = identities({
        "python-current": [args.python, ROOT / "tools/query_benchmark_python.py"],
    })["python-current"]
    # Bound the container even if a backend does not service statement_timeout.
    deadline = 60 + args.timeout * (6 + len(queries) * (2 * (args.samples + 1) + 3))
    command = [
        "docker", "run", "--rm", "-i", "--network", "none", "--ulimit", "core=0",
        "--cpuset-cpus", str(cpu), *mount(args.file, "/input.gol"),
        *mount(module, "/usr/lib/postgresql/16/lib/geodesk_fdw.so"),
        *mount(SOURCE / "geodesk_fdw.control", "/usr/share/postgresql/16/extension/geodesk_fdw.control"),
        *mount(SOURCE / "sql/geodesk_fdw--1.0.sql", "/usr/share/postgresql/16/extension/geodesk_fdw--1.0.sql"),
        *mount(ADAPTERS / "fdw_session.sh", "/session.sh"),
        image, "timeout", "--kill-after=10s", str(deadline),
        "sh", "/session.sh", str(os.getuid()), str(os.getgid()),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        temporary = Path(tmp)
        sql = temporary / "workload.sql"
        sql.write_text(workload(queries, args.samples, args.timeout))
        with sql.open() as script, tempfile.TemporaryFile(mode="w+t") as diagnostics:
            started = time.perf_counter_ns()
            with subprocess.Popen(
                command, stdin=script, stdout=subprocess.PIPE, stderr=diagnostics, text=True
            ) as process:
                try:
                    session, rows = read_session(process.stdout, started, queries)
                    if process.wait():
                        raise RuntimeError("PostgreSQL session failed")
                except Exception as error:
                    process.stdout.close()
                    diagnostics.seek(0)
                    detail = diagnostics.read()[-12000:]
                    raise RuntimeError(f"PostgreSQL session failed\n{detail}") from error
            diagnostics.seek(0)
            for line in diagnostics:
                if "WARNING:" in line and "Failed to apply GOQL" in line:
                    raise RuntimeError(line.strip())
        reference = python_results(args, queries, temporary, cpu)
    verified = True
    for index, row in enumerate(rows):
        checks = reference[f"{index}-iterate"]
        count = reference[f"{index}-count"]["count"]
        row["verified"] = (checks["fingerprint"] == row["fingerprint"]
                           and row["sql_count"] == count == checks["count"]
                           and all(plan["actual_rows"] == count for plan in row["iterate"])
                           and all(plan["actual_rows"] == 1 and plan["scanned_rows"] == count
                                   for plan in row["count"]))
        row["python"] = {}
        for operation in ("iterate", "count"):
            ref = reference[f"{index}-{operation}"]
            row["python"][operation] = {
                key: ref[key] for key in ("count", "open_ns", "setup_ns", "first_execution_ns",
                                          "samples_wall_ns", "fingerprint")
            }
            row[operation + "_median_execution_ns"] = statistics.median(
                plan["execution_ns"] for plan in row[operation][1:])
        verified &= row["verified"]
    unchanged(args.file, identity)
    report = {
        "input": identity, "image": image, "cpu": cpu, "session": session,
        "fdw_module_sha256": module_sha256, "python_worker": python_identity,
        "selector": "whole-file GOQL supplied as a foreign-table filter",
        "timing": "first EXPLAIN execution followed by steady samples; SQL server only",
        "verified": verified, "cases": rows,
    }
    report["harness_source"] = harness_source()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    if not verified:
        raise SystemExit("FDW results differ from the Python reference")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    build = modes.add_parser("setup", help="build the pinned FDW")
    build.add_argument("--jobs", type=int, default=4)
    build.add_argument("--image", default=IMAGE, help="use a prebuilt PostgreSQL 16 toolchain image")
    probe = modes.add_parser("run", help="benchmark SQL scans and counts and verify results")
    probe.add_argument("file", type=Path)
    probe.add_argument("--output", type=Path, required=True)
    probe.add_argument("--image", default=IMAGE)
    probe.add_argument("--python", type=Path, default=SDK / "python-current313/bin/python")
    probe.add_argument("--query", action="append", help="whole-file GOQL, repeat for multiple queries")
    probe.add_argument("--samples", type=int, default=5)
    probe.add_argument("--timeout", type=int, default=120, help="statement timeout in seconds")
    args = parser.parse_args()
    if args.mode == "setup":
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        setup(args)
    else:
        if args.samples < 1 or args.timeout < 1:
            parser.error("samples and timeout must be positive")
        if args.output.exists():
            parser.error("output already exists")
        args.file = args.file.resolve(strict=True)
        run(args)


if __name__ == "__main__":
    main()
