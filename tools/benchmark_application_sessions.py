#!/usr/bin/env python3
"""Replay independent first-pass workloads on the pinned UK GOL inputs."""
from contextlib import nullcontext
from benchmark_cache import FileCache
from paths import SDK, WORKER
from provenance import harness_source
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from application_export import export_signature, write_cli_output
from native_layout import crc32c, native_digests
from application_reference import degrees_per_metre, digest, file_digest, native_session, pixel_rows
from benchmark_workloads import MASK, mix

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/application-sessions.json"
SPEC = json.loads(MANIFEST.read_bytes())
CASES = {case["id"]: case for case in SPEC["cases"]}
NATIVE_PINS = ROOT / "benchmarks/application-native-signatures.json"


def verified_inputs(reference, native, native_pins, *, allow_unpinned=False):
    reference_bytes = reference.stat().st_size
    reference_sha = file_digest(reference)
    if not allow_unpinned and (reference_bytes != SPEC["input_bytes"] or reference_sha != SPEC["input_sha256"]):
        raise ValueError("UK benchmark GOL size or SHA-256 differs from the manifest")
    inputs = {"geodesk": dict(bytes=reference_bytes, sha256=reference_sha)}
    if native is not None:
        native_bytes = native.stat().st_size
        exact, layout = native_digests(native)
        if allow_unpinned:
            inputs["native"] = dict(bytes=native_bytes, sha256=exact, layout_sha256=layout, layout="custom")
        else:
            layouts = native_pins["layouts"]
            if native_bytes not in {pin["bytes"] for pin in layouts.values()}:
                raise ValueError("native UK GOL size differs from the manifest")
            matches = [name for name, pin in layouts.items()
                       if (native_bytes, layout) == (pin["bytes"], pin["layout_sha256"])]
            if not matches:
                raise ValueError("native UK GOL layout SHA-256 differs from the manifest")
            inputs["native"] = dict(bytes=native_bytes, sha256=exact, layout_sha256=layout,
                                    layout=matches[0])
    return inputs


def projected_box(box):
    west, south, east, north = box
    scale = 4294967294.9999
    def x(lon):
        return math.floor(lon * scale / 360 + 0.5)
    def y(lat):
        return math.floor(math.asinh(math.tan(math.radians(lat))) * scale / (2 * math.pi) + 0.5)
    return [x(west), y(south), x(east), y(north)]


def tile_box(tile):
    z, x, y = tile
    n = 2**z
    def lat(i):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * i / n))))
    return [360*x/n-180, lat(y+1), 360*(x+1)/n-180, lat(y)]


def reader_batches(case):
    def step(name, query, op="iterate", box=None, **kwargs):
        row = dict(name=name, query=query, operation=op, **kwargs)
        if box:
            row["bounds"] = projected_box(box)
        return row

    def around(lon, lat, radius):
        dx, dy = degrees_per_metre(lat)
        return [lon-radius*dx, lat-radius*dy, lon+radius*dx, lat+radius*dy]

    if "polygons" in case:
        batches = []
        for name, ring in case["polygons"]:
            x, y = zip(*ring)
            batches.append([step(name, "*", box=[min(x), min(y), max(x), max(y)])])
        return batches
    if case["id"] == "cesium":
        return [[step(f"{i}-{op}", "*", op, around(*view)) for op in ("count", "positions")]
                for i, view in enumerate(case["views"])]
    if case["id"] == "world2pixel":
        radius = case["pixel_metres"]/2 + case["radius_add_metres"]
        return [[step(f"pixel-{row}-{col}", case["query"], box=around(lon, lat, radius), limit=1)
                 for row, centres in enumerate(pixel_rows(case))
                 for col, (lon, lat) in enumerate(centres)]]
    if case["id"] == "next-higher":
        stages = [step("cities", case["cities"])]
        for city in case["seed_nodes"]:
            stages += [step(f"{op}-{city}", case["peaks"], op) for op in ("elevation", "peak_fields")]
        return [stages]
    if case["id"] == "campsbourne":
        return [[step(name, query, op, case["window"]) for name, query, op in [
            ("local-feature-count", "*", "count"), ("named-road-count", case["query"], "count"),
            ("named-road-geometry", case["query"], "geometry")]]]
    if case["id"] == "varna":
        return [[step("city-boundary", case["area"]),
                 step("road-vertices", "w"+case["roads"], "way_nodes", bounds=case["seed_bounds"])]]
    windows = case.get("windows") or list(map(tile_box, case["tiles"]))
    batches = [[dict(name=f"{index}-{name}", query=query, operation=op, bounds=projected_box(box))
                for name, query, op in case["layers"]] for index, box in enumerate(windows)]
    return batches if case["open_per_window"] else [sum(batches, [])]


def reader_session(case, gol, temp, engine, verify, rust, workers=0, stats=None):
    steps = []
    for index, batch in enumerate(reader_batches(case)):
        spec = temp / "queries.json"
        def query_rows(queries, check):
            spec.write_text(json.dumps(queries))
            if engine == "rust":
                cmd = [rust, gol, spec, "mmap-immutable", "1" if check else "0", "0", "-",
                       "1" if check else "0", "verify" if check else "skip", str(workers)]
            else:
                cmd = [sys.executable, ROOT / "tools/query_benchmark_python.py", gol, spec,
                       "--samples", "0", "--target-ms", "0"]
                if case.get("preload_shapely"):
                    cmd.append("--preload-shapely")
                if not check:
                    cmd.append("--first-pass")
            env = {**os.environ, "GOL_READER_BENCH_AUTO_INDEX_BYTES": "0"}
            output = subprocess.check_output(cmd, env=env)
            rows = [json.loads(line) for line in output.splitlines()]
            if [r["name"] for r in rows] != [q["name"] for q in queries]:
                raise ValueError("worker returned unexpected steps")
            return rows

        records = query_rows(batch, verify)
        pool_ns = records[0]["executor_setup_ns"] if workers else 0
        if stats is not None:
            stats["executor_setup_ns"] += pool_ns
        steps.append(dict(name=f"open-{index}", elapsed_ns=records[0]["open_ns"] + pool_ns, items=1,
                          fingerprint=None))
        for spec_row, row in zip(batch, records, strict=True):
            name = row["name"]
            if row.get("error") or row.get("retained_index_bytes", 0):
                raise ValueError(f"unexpected reader result: {row}")
            elapsed = row["setup_ns"] + row["first_execution_ns"]
            if verify:
                full_count = row["fingerprint"]["count"]
                if row["count"] != min(full_count, spec_row.get("limit", full_count)):
                    raise ValueError(f"{name}: consumed count differs from the full membership")
                if name in ("cities", "city-boundary"):
                    ids = ([i * 4 for i in case["seed_nodes"]] if name == "cities"
                           else [case["seed_relation"] * 4 + 2])
                    xor = 0
                    for value in ids:
                        xor ^= mix(value)
                    captured = dict(count=len(ids), sum=str(sum(map(mix, ids)) & MASK), xor=str(xor))
                    if captured != row["fingerprint"]:
                        raise ValueError(f"{name}: captured seed IDs differ from the result")
            fp = digest([row["fingerprint"], row["work_fingerprint"]]) if verify else None
            steps.append(dict(name=name, elapsed_ns=elapsed, items=row["work_items"],
                              features=row["count"], fingerprint=fp))
    return steps


def cli_session(case, gol, temp, executable, verify, geodesk_cli):
    steps = []
    output = temp / ("result." + case["format"])
    for index, (name, ring) in enumerate(case["polygons"]):
        area = temp / f"area-{index}.geojson"
        area.write_text(json.dumps(dict(type="Polygon", coordinates=[ring])))
        cmd = [executable, "query", gol, "*", "--area", area, "-f", case["format"]]
        if not geodesk_cli:
            cmd.append("--mmap")
        elapsed, size = write_cli_output(cmd, output, timeout=600)
        if not size:
            raise ValueError(f"{name}: empty export")
        step = dict(name=name, elapsed_ns=elapsed, items=size,
                    fingerprint=file_digest(output) if verify and geodesk_cli else None)
        if verify:
            counts, fingerprint = export_signature(output)
            step["export"] = dict(objects=counts, fingerprint=fingerprint)
        steps.append(step)
        output.unlink()
    return steps


def signature(steps):
    return [{k: v for k, v in s.items() if k not in ("elapsed_ns", "export")} for s in steps]


def job_engines(case, selected):
    engines = {"reader": ("rust", "python"),
               "binding": ("rust-reader", "python-reader", "python"),
               "cli": ("rust-reader", "python-reader", "cli", "rust-cli")}
    if selected == "readers":
        return list(engines["reader"]) if case["replay"] == "reader" else ["rust-reader", "python-reader"]
    return [engine for engine in engines[case["replay"]]
            if selected in ("all", engine.split("-")[0])
            or (selected == "cli" and engine == "rust-cli")]


def worker(case, engine, args):
    with tempfile.TemporaryDirectory(dir=args.scratch) as path:
        temp = Path(path)
        if case["replay"] == "reader" or engine.endswith("-reader"):
            stats = {"executor_setup_ns": 0} if args.reader_stats else None
            steps = reader_session(case, args.file, temp, engine.split("-")[0], args.verify,
                                   args.rust, args.reader_workers, stats)
            if stats is None:
                return steps
            import resource
            stats["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            return {"steps": steps, **stats}
        if case["replay"] == "cli":
            geodesk_cli = engine == "cli"
            executable = args.gol_tool if geodesk_cli else args.rust_cli
            return cli_session(case, args.file, temp, executable, args.verify, geodesk_cli)
        return native_session(case, args.file, temp, args.verify)


def run_worker(case, engine, args, verify=False, *, gol=None, env=None, workers=0, reader_stats=False):
    python = args.python if engine.startswith("python") else sys.executable
    cmd = [str(python), __file__, str(args.file if gol is None else gol),
           "--case", case["id"], "--worker", engine,
           "--rust", str(args.rust), "--gol-tool", str(args.gol_tool), "--rust-cli", str(args.rust_cli)]
    if workers:
        cmd += ["--reader-workers", str(workers)]
    if reader_stats:
        cmd.append("--reader-stats")
    if args.scratch:
        cmd += ["--scratch", str(args.scratch)]
    if verify:
        cmd.append("--verify")
    return json.loads(subprocess.check_output(cmd, env=env))


def round_order(jobs, number):
    # Reverse each pair of rounds so each engine runs both first and second.
    start = number // 2 * max(1, len(jobs) // 3) % len(jobs) if len(jobs) > 2 else 0
    sequence = jobs[start:] + jobs[:start]
    return sequence if number % 2 == 0 else sequence[::-1]


def run(args):
    cases = [CASES[name] for name in dict.fromkeys(args.case or CASES)]
    if args.samples < 1 or args.cores < 1 or not args.output or not args.file:
        raise ValueError("supply a GOL file, --output and positive sample and CPU budgets")
    if args.output.exists():
        raise FileExistsError(f'use a new output path: {args.output}')
    args.output.parent.mkdir(exist_ok=True, parents=True)
    native_pins = json.loads(NATIVE_PINS.read_bytes()) if args.native_file else None
    inputs = verified_inputs(args.file, args.native_file, native_pins, allow_unpinned=args.allow_unpinned_input)
    paths = {"geodesk": args.file, "native": args.native_file}
    affinity = sorted(os.sched_getaffinity(0))
    if args.cores > len(affinity):
        raise ValueError("requested CPU budget exceeds the available affinity")
    os.sched_setaffinity(0, affinity[:args.cores])
    jobs = [(label, case, engine) for case in cases for engine in job_engines(case, args.engine)
            for label in inputs]
    if not jobs:
        raise ValueError("no selected session supports this engine")
    version = lambda command: subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith(("model name", "Hardware"))), platform.machine())
    golden_path = ROOT / "benchmarks/application-session-signatures.json"
    known = json.loads(golden_path.read_bytes())
    export_pins = json.loads((ROOT / "benchmarks/application-export-signatures.json").read_bytes())
    def result_key(label, case, engine):
        key = case["id"] + "/" + engine
        return label + "/" + key if args.native_file else key

    def cgroup_value(name):
        path = Path("/sys/fs/cgroup") / name
        return path.read_text().strip() if path.exists() else None

    report = dict(manifest_sha256=file_digest(MANIFEST), gol_sha256=inputs["geodesk"]["sha256"],
                  host=platform.node(), cpu=cpu, machine=platform.machine(), kernel=platform.release(),
                  workflows={case["id"]: case["replay"] for case in cases},
                  affinity=affinity[:args.cores], python=sys.version, samples=args.samples,
                  runner=os.getenv("RUNNER_NAME"), cpu_quota=cgroup_value("cpu.max"),
                  memory_limit=cgroup_value("memory.max"), round_order=[],
                  revision=version(["git","-C",ROOT,"rev-parse","HEAD"]), versions={}, results={})
    report["harness_source"] = harness_source()
    if args.native_file:
        report["inputs"] = inputs
        report["native_pins_sha256"] = file_digest(NATIVE_PINS)
    if any(e.startswith("rust") for _, _, e in jobs):
        report["versions"]["rustc"] = version(["rustc", "-Vv"])
        if any(e in ("rust", "rust-reader") for _, _, e in jobs):
            report["versions"]["rust_worker_sha256"] = file_digest(args.rust)
        if any(e == "rust-cli" for _, _, e in jobs):
            report["versions"]["rust_cli_sha256"] = file_digest(args.rust_cli)
    if any(e.startswith("python") for _, _, e in jobs):
        versions = subprocess.check_output([args.python, "-c",
            "import importlib.metadata as m,json,sys; print(json.dumps({k:m.version(k) for k in "
            "('geodesk','shapely','numpy')} | {'python':sys.version}))"], text=True)
        versions = json.loads(versions)
        report["python"] = versions.pop("python")
        report["versions"].update(versions)
    if any(e == "cli" for _, _, e in jobs):
        located = shutil.which(str(args.gol_tool))
        if not located:
            raise ValueError(f"could not locate the GOL executable: {args.gol_tool}")
        args.gol_tool = Path(located).resolve()
        report["versions"]["gol_tool_sha256"] = file_digest(args.gol_tool)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    caches = {label: FileCache(paths[label]) for label in inputs} if args.cache == "warm" else {}
    report["cache"] = args.cache
    for number in range(args.samples):
        order = round_order(jobs, number)
        report["round_order"].append([result_key(*job) for job in order])
        for label, case, engine in order:
            key = result_key(label, case, engine)
            with caches[label].warm_mapping() if caches else nullcontext():
                rows = run_worker(case, engine, args, gol=paths[label])
            result = report["results"].setdefault(key, dict(runs=[]))
            result["runs"].append(rows)
            print(f"{key} {sum(r['elapsed_ns'] for r in rows)/1e6:.2f} ms", flush=True)

    # Validation is a fresh process outside all measured sessions.
    for label, case, engine in jobs:
        key = result_key(label, case, engine)
        result = report["results"][key]
        verified = run_worker(case, engine, args, verify=True, gol=paths[label])
        expected = signature(verified)
        for rows in result["runs"]:
            for actual, ref in zip(signature(rows), expected, strict=True):
                if actual["fingerprint"] is None:
                    actual["fingerprint"] = ref["fingerprint"]
                if actual != ref:
                    raise ValueError(f"unstable result for {key} {ref['name']}")
        result["signature"] = expected
        result["digest"] = digest(expected)
        # Native layouts can change the GeoDesk CLI's serialization order and PBF
        # compression. Pin the decoded objects for those exports below.
        if not args.allow_unpinned_input and (engine not in ("cli", "rust-cli") or (engine == "cli" and label == "geodesk")):
            pin_key = case["id"] + "/" + engine
            pin = known[pin_key]
            if label == "native":
                pin = native_pins["session_overrides"].get(pin_key, pin)
            if result["digest"] != pin:
                raise ValueError(f"{key}: verified output differs from pinned signature")
        if engine in ("cli", "rust-cli"):
            exports = {step["name"]: step["export"] for step in verified}
            if not args.allow_unpinned_input:
                for name, actual in exports.items():
                    expected_export = export_pins[case["id"]][name]
                    if actual != expected_export:
                        raise ValueError(f"{key} {name}: OSM export differs from pinned content: "
                                         f"expected {expected_export}, got {actual}")
            else:
                other_cli = "rust-cli" if engine == "cli" else "cli"
                other_res = report["results"].get(result_key(label, case, other_cli), {})
                if "exports" in other_res and exports != other_res["exports"]:
                    raise ValueError(f"cross-CLI export mismatch: {key}")
            result["exports"] = exports
        if case["replay"] == "reader" or engine.endswith("-reader"):
            counterpart = engine.replace("python", "rust") if "python" in engine else engine.replace("rust", "python")
            other = report["results"].get(result_key(label, case, counterpart), {})
            if "signature" in other:
                for a, b in zip(expected, other["signature"], strict=True):
                    if a != b:
                        raise ValueError(f"cross-engine content mismatch: {key} {a['name']}")
        result["complete"] = True
        result["median_ms"] = statistics.median(sum(s["elapsed_ns"] for s in run)/1e6
                                                for run in result["runs"])
        print(f"Verified {key}", flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, nargs="?")
    parser.add_argument("--native-file", type=Path, help="Also benchmark the default native UK build")
    parser.add_argument("--cache", choices=["inherit", "warm"], default="inherit")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--engine", choices=["all", "readers", "rust", "python", "cli"], default="all",
                        help="Select all work, the matched reader pair, Rust, Python, or both export CLIs")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rust", type=Path, default=WORKER)
    installed_python = SDK/"python-current313/bin/python"
    parser.add_argument("--python", type=Path, default=installed_python if installed_python.is_file() else Path(sys.executable),
                        help="interpreter with the pinned GeoDesk, Shapely and NumPy packages")
    installed_tool = SDK/"gol-current/gol"
    parser.add_argument("--gol-tool", type=Path, default=installed_tool if installed_tool.is_file() else Path("gol"))
    parser.add_argument("--rust-cli", type=Path, default=ROOT/"target/release/regol")
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--allow-unpinned-input", action="store_true",
                        help="Allow another UK GOL snapshot while still verifying repeat stability and cross-engine agreement")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--worker", choices=["rust", "python", "rust-reader", "python-reader", "cli", "rust-cli"], help=argparse.SUPPRESS)
    parser.add_argument("--reader-workers", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--reader-stats", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verify", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.list:
        for case in CASES.values():
            print(f"{case['id']:16s} {case['interface']:22s} {case['pattern']}")
    elif args.worker:
        print(json.dumps(worker(CASES[args.case[0]], args.worker, args)))
    else:
        run(args)


if __name__ == "__main__":
    main()
