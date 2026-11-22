"""Build and report same-reader comparisons between two UK GOL generators."""
import hashlib
import json
from pathlib import Path
from statistics import median
import subprocess
import tempfile

OPERATIONS = {"iterate": "Identity streams", "tags": "Tag decoding", "geometry": "Geometry assembly"}


def native_file(directory, uk, writer, revision):
    """Keep one cached UK native build. The runner holds an exclusive lock."""
    directory.mkdir(parents=True, exist_ok=True)
    gol = directory / "uk-regol.gol"
    record = directory / "uk-regol.json"
    recipe = {"producer": "regol", "revision": revision, "settings": [],
              "pbf_sha256": uk["pbf_sha256"]}
    if gol.is_file() and record.is_file():
        info = json.loads(record.read_bytes())
        if all(info.get(k) == v for k, v in recipe.items()) and info["bytes"] == gol.stat().st_size:
            with gol.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() == info["sha256"]:
                    return gol, info
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        staged = Path(tmp) / "native.gol"
        subprocess.run([writer, "build", uk["pbf"], staged], check=True)
        with staged.open("rb") as f:
            info = dict(recipe, bytes=staged.stat().st_size, sha256=hashlib.file_digest(f, "sha256").hexdigest())
        manifest = Path(tmp) / "identity.json"
        manifest.write_text(json.dumps(info))
        staged.replace(gol)
        manifest.replace(record)
    return gol, info


def paired_row(key, label, reader, base, native, cases, differences=()):
    if not differences and (not base or len(base) != len(native)):
        raise ValueError(f"{key}/{reader}: incomplete paired rounds")
    verified = bool(cases) and not differences
    savings = [100 * (1 - n / b) for b, n in zip(base, native, strict=True)] if verified else []
    return dict(key=f"{key}/{reader}", label=label, reader=reader, cases=cases, verified=verified,
                differences=list(differences), geodesk_ms=median(base) if verified else None,
                regol_ms=median(native) if verified else None, saved_pct=median(savings) if savings else None,
                geodesk_rounds_ms=base, regol_rounds_ms=native)


def corpus_series(summary, interval="prepare_and_scan_ms"):
    """Round sums retain query membership and the original measurement precision."""
    result = {}
    total = {}
    names = []
    for op, label in OPERATIONS.items():
        cohort = summary["cohorts"][op]
        names.extend(cohort["cases"])
        arms = {}
        for arm, times in cohort[interval].items():
            rounds = times["rounds"]
            values = [rounds[k] for k in sorted(rounds, key=int)]
            arms[arm] = values
            if arm not in total:
                total[arm] = [0.0] * len(values)
            total[arm] = [a + b for a, b in zip(total[arm], values, strict=True)]
        result[op] = label, cohort["cases"], arms
    result["total"] = "All matched cohorts", names, total
    return result


def corpus_rows(summary, n_cases):
    rows = []
    for op, (label, names, arms) in corpus_series(summary).items():
        engines = {arm.split("/")[1]: dict(median_ms=round(median(values), 2),
                                           rounds_ms=[round(v, 2) for v in values])
                   for arm, values in arms.items() if arm.startswith("geodesk/")}
        rows.append(dict(key=f"uk-corpus-{op}", cohort=op, label=label, cases=len(names),
                         total_spec_cases=n_cases, cores=1, cache="warm", verified=bool(names), engines=engines))
    return rows


def corpus_pairs(summary, *, cafes=False, cold=False):
    rows = []
    interval = "open_prepare_and_scan_ms" if cold else "prepare_and_scan_ms"
    for op, (label, names, arms) in corpus_series(summary, interval).items():
        if cafes and op != "iterate":
            continue
        key = "uk-cafes" if cafes else "uk-corpus-" + op
        label = "UK-wide cafés" if cafes else label
        if cold:
            key += "-cold"
            label += " (cold open and query)"
        for arm, base in arms.items():
            producer, reader, width = arm.split("/")
            if producer == "geodesk":
                row = paired_row(key, label, reader, base, arms[f"regol/{reader}/{width}"], len(names))
                rows.append(dict(row, cache="cold" if cold else "warm", cores=int(width), unit="queries"))
    return rows


def workflow_pair(key, label, reader, base, native):
    # Encoded CLI bytes and stream order can change with the file layout.
    # Compare decoded exports, or the per-step typed reader fingerprints.
    def steps(arm):
        if "exports" in arm:
            return arm["exports"]
        return {s["name"]: s for s in arm["signature"]}
    left, right = steps(base), steps(native)
    differences = sorted(k for k in left.keys() | right.keys() if left.get(k) != right.get(k))
    def rounds(arm):
        return [sum(s["elapsed_ns"] for s in batch) / 1e6 for batch in arm["runs"]]
    return paired_row(key, label, reader, rounds(base), rounds(native), len(left), differences)
