"""Validate the actual JSON ID output outside the startup timing interval."""

MASK = (1 << 64) - 1
KINDS = {"node": 0, "way": 1, "relation": 2}


def fingerprint_ids(ids):
    if not isinstance(ids, list):
        raise ValueError("expected a JSON array of ID strings")
    total = xor = 0
    seen = set()
    for item in ids:
        if not isinstance(item, str):
            raise ValueError("expected an ID string")
        kind, separator, number = item.partition("/")
        if (
            not separator
            or kind not in KINDS
            or not number.isascii()
            or not number.isdecimal()
        ):
            raise ValueError("invalid typed ID")
        number = int(number)
        if number >= 1 << 52:
            raise ValueError("ID exceeds GOL's 52-bit field")
        value = number * 4 + KINDS[kind]
        if value in seen:
            raise ValueError("duplicate typed ID")
        seen.add(value)
        mix = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        mix = ((mix ^ (mix >> 27)) * 0x94D049BB133111EB) & MASK
        mix ^= mix >> 31
        total = (total + mix) & MASK
        xor ^= mix
    return {"count": len(ids), "sum": str(total), "xor": str(xor)}


def startup_specs(reference, names):
    if not names or len(names) != len(set(names)):
        raise ValueError("startup workload names must be nonempty and unique")
    by_name = {spec["name"]: spec for spec in reference["queries"]}
    specs = []
    expected = {}
    storage = reference.get("storage", "paged")
    if storage not in {"paged", "file", "windowed", "cached", "owned", "mmap-immutable"}:
        raise ValueError("unknown Rust storage in reference")
    for name in names:
        if name not in by_name:
            raise ValueError(f"unknown startup workload: {name}")
        spec = by_name[name]
        if (
            "steps" in spec
            or spec.get("operation", "iterate") != "iterate"
            or "limit" in spec
        ):
            raise ValueError("startup workloads require unlimited handle iteration")
        rows = [row for row in reference["results"] if row["name"] == name]
        if {row["engine"] for row in rows} != {storage, "python"}:
            raise ValueError("startup reference requires both engines")
        if not rows or any(
            row["fingerprint"] != rows[0]["fingerprint"] for row in rows
        ):
            raise ValueError("startup reference fingerprints disagree")
        expected[name] = rows[0]["fingerprint"]
        specs.append(spec)
    return specs, expected
