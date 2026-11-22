#!/usr/bin/env python3
"""Automated pull-based benchmark runner for https://bench.regol.org.

Watches https://github.com/regol-gis/regol for new commits or tags, runs the
verified public gol-bench suite, normalizes results into compact JSON, and
pushes updates to the gh-pages branch of regol-gis/gol-bench.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from generators import corpus_pairs, corpus_rows, native_file, workflow_pair

ROOT = Path(__file__).resolve().parents[1]
REGOL_REPO_URL = "https://github.com/regol-gis/regol.git"


def run(cmd, *, cwd=None, env=None, capture=False):
    print("+", " ".join(map(str, cmd)), flush=True)
    if capture:
        return subprocess.check_output(cmd, cwd=cwd, env=env, text=True).strip()
    subprocess.run(cmd, cwd=cwd, env=env, check=True)
    return ""


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def machine_profile() -> dict:
    cpu = platform.machine()
    if Path("/proc/cpuinfo").is_file():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    threads = os.cpu_count() or 1
    cores = threads
    try:
        out = subprocess.check_output(["lscpu"], text=True)
        c_per_s = sockets = None
        for line in out.splitlines():
            if line.startswith("Core(s) per socket:"):
                c_per_s = int(line.split(":", 1)[1].strip())
            elif line.startswith("Socket(s):"):
                sockets = int(line.split(":", 1)[1].strip())
        if c_per_s and sockets:
            cores = c_per_s * sockets
    except Exception:
        pass
    ram_gib = 0
    if Path("/proc/meminfo").is_file():
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram_gib = round(int(line.split()[1]) / (1024 * 1024))
                break
    storage = "HDD"
    try:
        rota = subprocess.check_output(["lsblk", "-dno", "ROTA", "/dev/sda"], text=True).strip()
        storage = "SSD/NVMe" if rota == "0" else "HDD (7200 RPM)"
    except Exception:
        pass
    slug = f"{cpu.lower()}-{ram_gib}g".replace("(r)", "").replace("(tm)", "")
    slug = "".join(c if c.isalnum() else "-" for c in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:48]
    return {
        "id": slug,
        "label": f"{cpu} ({cores}C/{threads}T) · {ram_gib} GiB RAM · {storage}",
        "cpu": cpu,
        "cores": cores,
        "threads": threads,
        "ram_gib": ram_gib,
        "storage": storage,
        "os": f"Linux {platform.release()} ({platform.machine()})",
    }


def ensure_regol_checkout(repo_dir: Path, ref: str) -> dict:
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", REGOL_REPO_URL, repo_dir])
    else:
        run(["git", "-C", repo_dir, "fetch", "--tags", "origin"])
    commit = run(["git", "-C", repo_dir, "rev-parse", "--verify", f"{ref}^{{commit}}"], capture=True)
    commit_date = run(["git", "-C", repo_dir, "show", "-s", "--format=%cI", commit], capture=True)
    commit_subject = run(["git", "-C", repo_dir, "show", "-s", "--format=%s", commit], capture=True)
    tags = run(["git", "-C", repo_dir, "tag", "--points-at", commit], capture=True).splitlines()
    tag = next((t for t in tags if t.startswith("v")), tags[0] if tags else None)
    cargo_toml = run(["git", "-C", repo_dir, "show", f"{commit}:Cargo.toml"], capture=True)
    regol_version = None
    for line in cargo_toml.splitlines():
        s = line.strip()
        if s.startswith("version = "):
            regol_version = s.split("=", 1)[1].strip().strip('"')
            break
    return {
        "commit": commit,
        "short_commit": commit[:7],
        "commit_date": commit_date,
        "commit_subject": commit_subject,
        "tag": tag,
        "regol_version": regol_version or "unknown",
    }


def ensure_pbf(url: str, dest: Path, expected_sha: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        if expected_sha is None or file_sha256(dest) == expected_sha:
            return dest
        dest.unlink()
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        staged = Path(tmp) / dest.name
        run(["curl", "--fail", "--location", "--retry", "3", "-o", staged, url])
        if expected_sha and file_sha256(staged) != expected_sha:
            raise ValueError(f"SHA-256 mismatch for {url}")
        staged.replace(dest)
    return dest


def setup_workers(regol_dir: Path, commit: str, jobs: int) -> dict:
    # Remove per-run materialized regol trees so setup rebuilds cleanly at this commit.
    ref_regol = ROOT / ".reference/regol"
    if ref_regol.exists():
        shutil.rmtree(ref_regol)
    sdk = Path(os.environ.get("GOL_BENCH_SDK_DIR", ROOT / ".reference/corpus")).expanduser().absolute()
    comps = ["regol", "mmap-worker", "cli", "writer"]
    if not (sdk / "python-current313/bin/python").is_file():
        comps.append("python")
    if not (sdk / "gol-current/gol").is_file():
        comps.append("gol-tool")
    if not (sdk / "gol-legacy/gol").is_file():
        comps.append("gol-tool-legacy")
    has_java_tools = bool(shutil.which("java") and shutil.which("javac"))
    if has_java_tools and not (sdk / "classes/CorpusJava.class").is_file():
        comps.append("java")
    run([
        sys.executable, ROOT / "tools/setup_engines.py",
        *comps, "--source", regol_dir, "--revision", commit,
        "--jobs", str(jobs), "--python", sys.executable,
    ])
    py = sdk / "python-current313/bin/python"
    py_ver = json.loads(run([
        py, "-c",
        "import importlib.metadata as m, json; print(json.dumps({k: m.version(k) for k in ('geodesk', 'shapely')}))",
    ], capture=True))
    java_ok = has_java_tools and (sdk / "classes/CorpusJava.class").is_file()
    java_runtime = None
    if java_ok:
        out = subprocess.check_output(["java", "-version"], stderr=subprocess.STDOUT, text=True)
        java_runtime = out.splitlines()[0].strip()
    gol_current = sdk / "gol-current/gol"
    gol_legacy = sdk / "gol-legacy/gol"
    gol_current_ok = False
    if gol_current.is_file():
        probe = subprocess.run([gol_current, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gol_current_ok = probe.returncode == 0
    active_gol = gol_current if gol_current_ok else gol_legacy
    active_gol_version = "2.5.3" if gol_current_ok else "2.0.0"
    competitors = {
        "geodesk_python": py_ver["geodesk"],
        "shapely": py_ver["shapely"],
        "gol_tool": active_gol_version,
        "gol_tool_legacy": "2.0.0",
    }
    if java_ok:
        competitors["geodesk_java"] = "2.1.0"
        competitors["java_runtime"] = java_runtime
    return {
        "sdk": sdk,
        "python": py,
        "has_java": java_ok,
        "gol_tool": active_gol,
        "gol_version": active_gol_version,
        "gol_supports_pbf": gol_current_ok,
        "writer_ref_engine": "geodesk-current" if gol_current_ok else "geodesk",
        "gol_legacy": gol_legacy,
        "regol_worker": ROOT / "target/release/examples/query_benchmark",
        "regol_mmap": ROOT / "target/release/examples/query_benchmark_mmap",
        "regol_cli": ROOT / "target/release/regol",
        "competitors": competitors,
    }


def prepare_datasets(dataset_dir: Path, pbf_dir: Path, workers: dict) -> dict:
    catalog = json.loads((ROOT / "benchmarks/datasets.json").read_text())
    prepared = {}
    for name in ("monaco", "liechtenstein", "bremen", "ghana", "uk"):
        item = catalog[name]
        pbf_name = Path(item["pbf_url"]).name
        expected_sha = item.get("pbf", {}).get("sha256")
        pbf_path = ensure_pbf(item["pbf_url"], pbf_dir / pbf_name, expected_sha)
        cmd = [
            sys.executable, ROOT / "tools/datasets.py", "prepare", name,
            "--directory", dataset_dir,
        ]
        if "layout_sha256" in item:
            cmd += ["--source-pbf", pbf_path, "--writer", workers["regol_cli"]]
        else:
            # Build once into dataset_dir if missing, using cached PBF.
            gol_path = dataset_dir / f"{name}-{item['sha256'][:12]}.gol"
            if not gol_path.is_file():
                gol_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=dataset_dir) as tmp:
                    stage = Path(tmp) / "dataset.gol"
                    run([workers["gol_legacy"], "build", stage, pbf_path, "-w"])
                    rem = stage.stat().st_size % 4096
                    if rem:
                        with stage.open("ab") as out:
                            out.write(bytes(4096 - rem))
                    stage.replace(gol_path)
            prepared[name] = {
                "pbf": pbf_path,
                "gol": gol_path,
                "gol_bytes": gol_path.stat().st_size,
                "gol_sha256": file_sha256(gol_path),
                "pbf_sha256": expected_sha or file_sha256(pbf_path),
            }
            continue
        out = run(cmd, capture=True)
        gol_path = Path(out.splitlines()[-1].strip())
        prepared[name] = {
            "pbf": pbf_path,
            "gol": gol_path,
            "gol_bytes": gol_path.stat().st_size,
            "gol_sha256": file_sha256(gol_path),
            "pbf_sha256": expected_sha or file_sha256(pbf_path),
        }
    return prepared


def run_writers(scratch: Path, datasets: dict, workers: dict, cores_list: list[int], rounds: int) -> list[dict]:
    rows = []
    ref_eng = workers["writer_ref_engine"]
    for ds in ("monaco", "bremen", "ghana"):
        out_dir = scratch / f"writers-{ds}"
        verif = "identities" if ds == "ghana" else "tags"
        run([
            sys.executable, ROOT / "tools/build_compare.py",
            datasets[ds]["pbf"],
            "--reference-gol", datasets[ds]["gol"],
            "--engines", "regol", ref_eng,
            "--cores", *map(str, cores_list),
            "--rounds", str(rounds),
            "--cache", "warm",
            "--verification", verif,
            "--discard-outputs",
            "--writer", workers["regol_cli"],
            "--gol-tool", workers["gol_legacy"],
            "--gol-tool-current", workers["gol_tool"],
            "--python", workers["python"],
            "--spec", ROOT / "benchmarks/writer-features.json",
            "--output", out_dir,
        ])
        summary = json.loads((out_dir / "build-summary.json").read_text())
        by_width: dict[int, dict] = {}
        for group in summary["groups"]:
            width = group["cores"]
            eng = group["producer"]
            rounds_ms = [round(s["wall_ns"] / 1e6, 2) for s in group["samples"] if s.get("status") == "completed"]
            by_width.setdefault(width, {})[eng] = {
                "wall_ms_median": round(group["wall_ms_median"], 2) if "wall_ms_median" in group else None,
                "wall_ms_rounds": rounds_ms,
                "peak_rss_mib": round(group["peak_rss_kib_median"] / 1024.0, 2) if "peak_rss_kib_median" in group else None,
                "output_mib": round(group["bytes_median"] / (1024.0 * 1024.0), 2) if "bytes_median" in group else None,
                "verified": bool(group.get("comparable")),
            }
        for width in sorted(by_width):
            rows.append({
                "key": f"{ds}-{width}c",
                "dataset": ds,
                "cores": width,
                "ref_engine": ref_eng,
                "ref_label": f"GOL Tool {workers['gol_version']}",
                "engines": by_width[width],
            })
    return rows


def run_scale(scratch: Path, datasets: dict, workers: dict, rounds: int) -> list[dict]:
    out_dir = scratch / "scale"
    engines = ["regol", "python", "java"] if workers.get("has_java") else ["regol", "python"]
    cmd = [
        sys.executable, ROOT / "tools/scale.py",
        "--engines", *engines,
        "--cores", "1",
        "--rounds", str(rounds),
        "--cache", "warm",
        "--allow-unpinned-inputs",
        "--rust", workers["regol_mmap"],
        "--python", workers["python"],
        "--output", out_dir,
    ]
    for ds in ("monaco", "liechtenstein", "bremen", "ghana", "uk"):
        cmd += ["--dataset", ds, "--file", f"{ds}={datasets[ds]['gol']}"]
    run(cmd)
    idx = json.loads((out_dir / "index.json").read_text())
    rows = []
    for ds in ("monaco", "liechtenstein", "bremen", "ghana", "uk"):
        entry = idx[ds]
        summary = entry["prepare_and_scan"]
        reg = summary.get("regol/1") or {}
        py = summary.get("python/1") or {}
        eng_map = {
            "regol": {
                "median_ms": round(reg["median_ms"], 3),
                "rounds_ms": [round(v, 3) for v in reg["rounds_ms"]],
            },
            "python": {
                "median_ms": round(py["median_ms"], 3),
                "rounds_ms": [round(v, 3) for v in py["rounds_ms"]],
            },
        }
        if "java/1" in summary:
            jv = summary["java/1"]
            eng_map["java"] = {
                "median_ms": round(jv["median_ms"], 3),
                "rounds_ms": [round(v, 3) for v in jv["rounds_ms"]],
            }
        rows.append({
            "key": f"{ds}-1c-warm",
            "dataset": ds,
            "gol_mib": round(datasets[ds]["gol_bytes"] / (1024.0 * 1024.0), 2),
            "cores": 1,
            "cache": "warm",
            "engines": eng_map,
            "verified": True,
        })
    return rows


def run_cli(scratch: Path, datasets: dict, workers: dict, samples: int, cores: int) -> list[dict]:
    out_file = scratch / "cli-leeds.json"
    formats = ["count", "csv", "geojson", "geojsonl", "xml", "pbf"] if workers["gol_supports_pbf"] else ["count", "csv", "geojson", "geojsonl"]
    run([
        sys.executable, ROOT / "tools/benchmark_cli.py",
        datasets["uk"]["gol"],
        "--query", "n[amenity=cafe]",
        "--query", "w[highway]",
        "--bbox=-1.57,53.785,-1.51,53.815",
        "--formats", *formats,
        "--samples", str(samples),
        "--cores", str(cores),
        "--regol", workers["regol_cli"],
        "--gol-tool", workers["gol_tool"],
        "--scratch", scratch,
        "--output", out_file,
    ])
    report = json.loads(out_file.read_text())
    rows = []
    for item in report["results"]:
        q = item["query"]
        fmt = item["format"]
        q_slug = "cafes" if "cafe" in q else "roads"
        engines = {}
        selected = None
        for name, arm in item["arms"].items():
            runs_ms = [round(r["wall_ns"] / 1e6, 2) for r in arm.get("runs", [])]
            if selected is None and isinstance(arm.get("signature"), dict):
                selected = arm["signature"].get("selected")
            engines[name] = {
                "median_ms": round(statistics.median(runs_ms), 2) if runs_ms else None,
                "rounds_ms": runs_ms,
                "error": arm.get("error"),
            }
        rows.append({
            "key": f"leeds-{q_slug}-{fmt}",
            "dataset": "uk (Leeds bbox)",
            "query": q,
            "format": fmt,
            "cores": cores,
            "selected": selected,
            "verified": bool(item.get("verified", False)),
            "engines": engines,
        })
    return rows


def run_applications(scratch, datasets, workers, samples, native=None):
    out_file = scratch / "applications.json"
    uk = datasets["uk"]
    run([
        sys.executable, ROOT / "tools/benchmark_application_sessions.py", uk["gol"],
        *(["--native-file", native, "--cache", "warm"] if native else []),
        "--engine", "all" if workers["gol_supports_pbf"] else "readers",
        "--cores", "1", "--samples", str(samples), "--rust", workers["regol_worker"],
        "--python", workers["python"], "--gol-tool", workers["gol_tool"],
        "--rust-cli", workers["regol_cli"], "--scratch", scratch, "--allow-unpinned-input",
        "--output", out_file,
    ])
    report = json.loads(out_file.read_bytes())
    if native and (report["inputs"]["native"]["sha256"] != uk["generators"]["regol"]["sha256"]
                   or report["gol_sha256"] != uk["gol_sha256"]):
        raise ValueError("workflow input differs from the recorded generator artifact")
    manifest = json.loads((ROOT / "benchmarks/application-sessions.json").read_bytes())
    rows, pairs = [], []
    prefix = "geodesk/" if native else ""
    for case in manifest["cases"]:
        for kind, engines in (("reader", ("rust", "python")), ("reader", ("rust-reader", "python-reader")),
                              ("cli", ("rust-cli", "cli"))):
            key = f"{case['id']}-{kind}"
            label = case["id"] + (" (CLI export)" if kind == "cli" else "")
            arms = [report["results"].get(f"{prefix}{case['id']}/{e}") for e in engines]
            if not all(arms):
                continue
            stats = {}
            names = ("regol-cli", "gol-tool") if kind == "cli" else ("regol", "python")
            for engine, name, arm in zip(engines, names, arms, strict=True):
                suffix = f"{case['id']}/{engine}"
                times = [round(sum(s["elapsed_ns"] for s in batch) / 1e6, 2) for batch in arm["runs"]]
                stats[name] = dict(median_ms=round(arm["median_ms"], 2), rounds_ms=times)
                if native:
                    pairs.append(workflow_pair(key, label, name, arm, report["results"]["native/" + suffix]))
            rows.append(dict(key=key, id=case["id"], label=label, interface=case["interface"],
                                 kind=kind, cores=1, cache=report["cache"], verified=True, engines=stats))
    return rows, pairs


def run_corpus(scratch, datasets, workers, native=None, cafes=False):
    uk = datasets["uk"]
    out = scratch / ("cafes" if cafes else "corpus")
    spec = ROOT / "benchmarks" / ("uk-national-cafes.json" if cafes else "uk-corpus-queries.json")
    engines = ["regol", "python"] + (["java"] if workers["has_java"] else [])
    args = ["--library", f"geodesk={uk['gol']}"]
    baseline = scratch / "geodesk-corpus.json"
    if native:
        lock = scratch / "uk-inputs.json"
        lock.write_text(json.dumps(uk["generators"]))
        args += ["--library", f"regol={native}", "--input-lock", lock]
        if not cafes:
            args += ["--reference-summary", baseline]
    run([sys.executable, ROOT / "tools/campaign.py", *args, "--engines", *engines, "--cores", "1",
         "--rounds", str(2 * len(engines) if native else 3), "--cache", "warm",
         "--rust", workers["regol_mmap"], "--python", workers["python"], "--spec", spec, "--output", out])
    summary = json.loads((out / "summary.json").read_bytes())
    legacy = []
    if not cafes:
        reference = json.loads(baseline.read_bytes()) if native else summary
        legacy = corpus_rows(reference, len(json.loads(spec.read_bytes())))
    evidence = dict(spec_sha256=summary["spec_sha256"],
                    cohort_cases={op: c["cases"] for op, c in summary["cohorts"].items()},
                    verification=summary["verification"],
                    exclusions={arm: {c["name"]: c["status"] for c in cases
                                      if c["role"] == "candidate_selector" and c["reference_count"]
                                      and c["status"] not in ("identities_match", "content_match")}
                                for arm, cases in summary["verification_cases"].items()})
    return legacy, corpus_pairs(summary, cafes=cafes) if native else [], evidence


def rebuild_aggregates(data_dir: Path, manifest: dict):
    runs_dir = data_dir / "runs"
    history = {"writers": {}, "scale": {}, "cli": {}, "applications": {}, "corpus": {}, "generators": {}}
    latest_run = None
    for meta in manifest["runs"]:
        run_path = data_dir.parent / meta["file"]
        if not run_path.is_file():
            continue
        payload = json.loads(run_path.read_text())
        latest_run = payload
        base = {
            "run_id": meta["id"],
            "timestamp": meta["timestamp"],
            "commit": meta["commit"],
            "short_commit": meta["short_commit"],
            "tag": meta.get("tag"),
            "regol_version": meta.get("regol_version"),
            "machine_id": meta["machine_id"],
            "rustc": meta.get("rustc"),
        }
        gol_label = f"GOL Tool {meta.get('competitors', {}).get('gol_tool', '2.0.0')}"
        for row in payload.get("writers", []):
            reg = row["engines"].get("regol", {})
            ref_key = row.get("ref_engine") or ("geodesk-current" if "geodesk-current" in row["engines"] else "geodesk")
            ref = row["engines"].get(ref_key, {})
            history["writers"].setdefault(row["key"], []).append({
                **base,
                "dataset": row["dataset"],
                "cores": row["cores"],
                "regol_ms": reg.get("wall_ms_median"),
                "ref_ms": ref.get("wall_ms_median"),
                "regol_rounds": reg.get("wall_ms_rounds", []),
                "ref_rounds": ref.get("wall_ms_rounds", []),
                "regol_rss_mib": reg.get("peak_rss_mib"),
                "ref_rss_mib": ref.get("peak_rss_mib"),
                "regol_mib": reg.get("output_mib"),
                "ref_mib": ref.get("output_mib"),
                "ref_label": row.get("ref_label", gol_label),
                "verified": bool(reg.get("verified") and ref.get("verified")),
            })
        for row in payload.get("scale", []):
            reg = row["engines"].get("regol", {})
            ref = row["engines"].get("python", {})
            jv = row["engines"].get("java", {})
            history["scale"].setdefault(row["key"], []).append({
                **base,
                "dataset": row["dataset"],
                "gol_mib": row.get("gol_mib"),
                "cores": row["cores"],
                "regol_ms": reg.get("median_ms"),
                "ref_ms": ref.get("median_ms"),
                "java_ms": jv.get("median_ms"),
                "regol_rounds": reg.get("rounds_ms", []),
                "ref_rounds": ref.get("rounds_ms", []),
                "java_rounds": jv.get("rounds_ms", []),
                "ref_label": "GeoDesk Python",
                "verified": bool(row.get("verified")),
            })
        for row in payload.get("cli", []):
            reg = row["engines"].get("Regol", {})
            ref = row["engines"].get("GOL Tool", {})
            history["cli"].setdefault(row["key"], []).append({
                **base,
                "dataset": row["dataset"],
                "query": row["query"],
                "format": row["format"],
                "cores": row["cores"],
                "selected": row.get("selected"),
                "regol_ms": reg.get("median_ms"),
                "ref_ms": ref.get("median_ms"),
                "regol_rounds": reg.get("rounds_ms", []),
                "ref_rounds": ref.get("rounds_ms", []),
                "ref_label": gol_label,
                "verified": bool(row.get("verified")),
            })
        for row in payload.get("applications", []):
            if row["kind"] == "cli":
                reg = row["engines"].get("regol-cli", {})
                ref = row["engines"].get("gol-tool", {})
                ref_label = gol_label
            else:
                reg = row["engines"].get("regol", {})
                ref = row["engines"].get("python", {})
                ref_label = "GeoDesk Python"
            history["applications"].setdefault(row["key"], []).append({
                **base,
                "id": row["id"],
                "label": row["label"],
                "kind": row["kind"],
                "cores": row["cores"],
                "regol_ms": reg.get("median_ms"),
                "ref_ms": ref.get("median_ms"),
                "regol_rounds": reg.get("rounds_ms", []),
                "ref_rounds": ref.get("rounds_ms", []),
                "ref_label": ref_label,
                "verified": bool(row.get("verified")),
            })
        for row in payload.get("generators", []):
            history["generators"].setdefault(row["key"], []).append({
                **base, **{k: row[k] for k in ("reader", "label", "cases", "verified", "saved_pct",
                                                "regol_ms", "geodesk_ms", "differences")},
            })
        for row in payload.get("corpus", []):
            reg = row["engines"].get("regol", {})
            ref = row["engines"].get("python", {})
            jv = row["engines"].get("java", {})
            history["corpus"].setdefault(row["key"], []).append({
                **base,
                "cohort": row["cohort"],
                "label": row["label"],
                "cases": row["cases"],
                "cores": row["cores"],
                "regol_ms": reg.get("median_ms"),
                "ref_ms": ref.get("median_ms"),
                "java_ms": jv.get("median_ms"),
                "regol_rounds": reg.get("rounds_ms", []),
                "ref_rounds": ref.get("rounds_ms", []),
                "java_rounds": jv.get("rounds_ms", []),
                "ref_label": "GeoDesk Python",
                "verified": bool(row.get("verified")),
            })

    (data_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (data_dir / "history.json").write_text(json.dumps(history, indent=2) + "\n")
    if latest_run is not None:
        (data_dir / "latest.json").write_text(json.dumps(latest_run, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path.home() / "regol-bench")
    parser.add_argument("--pages-dir", type=Path, required=True, help="Local checkout of the gh-pages branch")
    parser.add_argument("--ref", default="origin/main", help="Git ref in regol-gis/regol to benchmark")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    parser.add_argument("--suites", nargs="+", default=["writers", "scale", "cli", "applications", "corpus", "generators"],
                        choices=["writers", "scale", "cli", "applications", "corpus", "generators"])
    parser.add_argument("--force", action="store_true", help="Run even if this commit is already recorded")
    parser.add_argument("--push", action="store_true", help="Commit and push updated data to gh-pages")
    args = parser.parse_args()

    work = args.work_dir.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    lock_file = (work / ".bench.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another regol-bench run is already in progress; exiting.")
        return

    pages = args.pages_dir.expanduser().resolve()
    data_dir = pages / "data"
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    if args.push:
        run(["git", "-C", pages, "pull", "--rebase", "origin", "gh-pages"])

    regol_dir = work / "regol-repo"
    rev = ensure_regol_checkout(regol_dir, args.ref)
    machine = machine_profile()

    manifest_path = data_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"schema_version": 1, "updated_at": None, "machines": {}, "runs": []}

    suites = set(args.suites)
    prior = [json.loads((pages / r["file"]).read_bytes()) for r in manifest["runs"]
             if r["commit"] == rev["commit"] and r["machine_id"] == machine["id"]]
    if prior and all(prior[-1].get(s) for s in suites) and not args.force:
        print(f"Requested suites already recorded for {rev['short_commit']} on {machine['id']}.")
        return
    if args.push and prior:
        # Each published snapshot stands alone, including on the first generator rollout.
        suites.update(s for s in ("writers", "scale", "cli", "applications", "corpus", "generators")
                      if prior[-1].get(s))
    started = time.monotonic()
    workers = setup_workers(regol_dir, rev["commit"], args.jobs)
    datasets = prepare_datasets(work / "datasets", work / "pbfs", workers)
    native = None
    if "generators" in suites:
        uk = datasets["uk"]
        if prior and (prior[-1]["datasets"]["uk"]["gol_sha256"] != uk["gol_sha256"] or
                      prior[-1]["datasets"]["uk"]["pbf_sha256"] != uk["pbf_sha256"]):
            raise ValueError("the cached UK input differs from the previous run for this revision")
        native, info = native_file(work / "datasets", uk, workers["regol_cli"], rev["commit"])
        uk["generators"] = {"regol": info, "geodesk": dict(producer="GOL Tool", revision="2.0.0",
                            settings=["-w"], page_alignment_bytes=4096, pbf_sha256=uk["pbf_sha256"],
                            bytes=uk["gol_bytes"], sha256=uk["gol_sha256"])}
    rustc = run(["rustc", "--version"], capture=True)

    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y-%m-%dT%H%M%SZ')}-{rev['short_commit']}"
    run_rel = f"data/runs/{run_id}.json"

    with tempfile.TemporaryDirectory(prefix="regol-bench-", dir=work) as tmp:
        scratch = Path(tmp)
        payload = {
            "id": run_id,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            **rev,
            "rustc": rustc,
            "machine_id": machine["id"],
            "machine": machine,
            "competitors": workers["competitors"],
            "datasets": {
                name: {
                    "gol_bytes": info["gol_bytes"],
                    "gol_sha256": info["gol_sha256"],
                    "pbf_sha256": info["pbf_sha256"],
                    **({"generators": info["generators"]} if "generators" in info else {}),
                }
                for name, info in datasets.items()
            },
            "writers": [],
            "scale": [],
            "cli": [],
            "applications": [],
            "corpus": [],
        }

        payload["generators"] = []
        if "writers" in suites:
            payload["writers"] = run_writers(scratch, datasets, workers, [1, min(4, machine["cores"])], rounds=3)
        if "scale" in suites:
            payload["scale"] = run_scale(scratch, datasets, workers, rounds=5)
        if "cli" in suites:
            payload["cli"] = run_cli(scratch, datasets, workers, samples=5, cores=min(2, machine["cores"]))
        if "applications" in suites or native:
            payload["applications"], pairs = run_applications(scratch, datasets, workers, 4 if native else 3, native)
            payload["generators"].extend(pairs)
        if "corpus" in suites or native:
            payload["corpus"], pairs, corpus = run_corpus(scratch, datasets, workers, native)
            payload["generators"].extend(pairs)
        if native:
            _, pairs, cafes = run_corpus(scratch, datasets, workers, native, cafes=True)
            payload["generators"].extend(pairs)
            payload["generator_evidence"] = {"corpus": corpus, "cafes": cafes}
        payload["harness_revision"] = run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture=True)
        payload["elapsed_seconds"] = round(time.monotonic() - started, 1)

    (pages / run_rel).write_text(json.dumps(payload, indent=2) + "\n")

    manifest["machines"][machine["id"]] = machine
    manifest["updated_at"] = payload["timestamp"]
    manifest["runs"].append({
        "id": run_id,
        "timestamp": payload["timestamp"],
        **rev,
        "rustc": rustc,
        "machine_id": machine["id"],
        "competitors": workers["competitors"],
        "file": run_rel,
    })
    manifest["runs"].sort(key=lambda r: (r["commit_date"], r["timestamp"]))

    rebuild_aggregates(data_dir, manifest)
    print(f"Wrote benchmark run {run_id} to {pages / run_rel}")

    if args.push:
        run(["git", "-C", pages, "add", "data/"])
        label = rev["tag"] or rev["short_commit"]
        run(["git", "-C", pages, "commit", "-m", f"Record benchmark results for regol {label}"])
        run(["git", "-C", pages, "push", "origin", "gh-pages"])


if __name__ == "__main__":
    main()
