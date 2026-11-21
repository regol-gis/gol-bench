"""Canonical OSM export records for application-session verification."""
import hashlib
import json
import subprocess
import time
import struct
import xml.etree.ElementTree as ET
import zlib

from pbf_expected import fields, packed

KINDS = ("node", "way", "relation")


def xml_coords(attrs):
    return tuple(round(float(attrs[key]) * 1_000_000_000) for key in ("lon", "lat"))


def xml_records(path):
    events = ET.iterparse(path, events=("start", "end"))
    _, root = next(events)
    if root.tag != "osm":
        raise ValueError("expected an OSM XML document")
    for event, element in events:
        if event != "end" or element.tag not in KINDS:
            continue
        kind, attrs = element.tag, element.attrib
        tags = sorted((child.attrib["k"], child.attrib["v"])
                      for child in element.iterfind("tag"))
        if kind == "node":
            payload = xml_coords(attrs)
        elif kind == "way":
            payload = []
            for child in element.iterfind("nd"):
                attrib = child.attrib
                if "lat" in attrib or "lon" in attrib:
                    payload.append((int(attrib.get("ref", 0)), *xml_coords(attrib)))
                else:
                    payload.append(int(attrib["ref"]))
        else:
            payload = [(child.attrib["type"], int(child.attrib["ref"]), child.attrib["role"])
                       for child in element.iterfind("member")]
        yield kind, int(attrs["id"]), tags, payload
        root.clear()


def zigzag(value):
    return (value >> 1) ^ -(value & 1)


def signed(value):
    return value if value < 1 << 63 else value - (1 << 64)


def deltas(values):
    current = 0
    for value in values:
        current += zigzag(value)
        yield current


def split_fields(data, repeated=()):
    result = {key: [] for key in repeated}
    for key, wire, value in fields(data):
        if key in repeated:
            result[key].extend(packed(value) if wire == 2 else (value,))
        else:
            result[key] = value
    return result


def read_blob(path):
    with path.open("rb") as stream:
        while prefix := stream.read(4):
            length, = struct.unpack(">I", prefix)
            header = split_fields(stream.read(length))
            blob = stream.read(header[3])
            if len(blob) != header[3]:
                raise ValueError("truncated PBF blob")
            body = split_fields(blob)
            if 1 in body:
                data = body[1]
            elif 3 in body:
                data = zlib.decompress(body[3])
                if len(data) != body[2]:
                    raise ValueError("PBF blob has unexpected decompressed size")
            else:
                raise ValueError("unsupported PBF compression")
            yield header[1], data


def pbf_records(path):
    def tags(keys, values):
        return sorted((strings[k], strings[v]) for k, v in zip(keys, values, strict=True))

    blobs = read_blob(path)
    if next(blobs)[0] != b"OSMHeader":
        raise ValueError("expected an OSM PBF header")
    for kind, data in blobs:
        if kind != b"OSMData":
            continue
        block = list(fields(data))
        table, = (value for key, _, value in block if key == 1)
        strings = [v.decode("utf-8") for key, _, v in fields(table) if key == 1]
        settings = {key: value for key, _, value in block if key in (17, 19, 20)}
        granularity = settings.get(17, 100)
        lat_offset, lon_offset = (signed(settings.get(key, 0)) for key in (19, 20))

        def coords(lat, lon):
            return lon_offset + lon * granularity, lat_offset + lat * granularity

        for key, _, group in block:
            if key != 2:
                continue
            for kind, _, data in fields(group):
                if kind == 1:
                    node = split_fields(data, (2, 3))
                    yield "node", zigzag(node[1]), tags(node[2], node[3]), coords(
                        zigzag(node[8]), zigzag(node[9]))
                elif kind == 2:
                    dense = split_fields(data, (1, 8, 9, 10))
                    pairs = iter(dense[10])
                    for ident, lat, lon in zip(*(deltas(dense[k]) for k in (1, 8, 9)), strict=True):
                        node_tags = []
                        if dense[10]:
                            while (key := next(pairs)) != 0:
                                node_tags.append((strings[key], strings[next(pairs)]))
                        yield "node", ident, sorted(node_tags), coords(lat, lon)
                    if next(pairs, None) is not None:
                        raise ValueError("extra DenseNodes tags")
                elif kind == 3:
                    way = split_fields(data, (2, 3, 8, 9, 10))
                    refs = list(deltas(way[8]))
                    if way[9] or way[10]:
                        refs = [(ident, *coords(lat, lon)) for ident, lat, lon in
                                zip(refs, deltas(way[9]), deltas(way[10]), strict=True)]
                    yield "way", signed(way[1]), tags(way[2], way[3]), refs
                elif kind == 4:
                    relation = split_fields(data, (2, 3, 8, 9, 10))
                    members = [(KINDS[k], ident, strings[role]) for k, ident, role in
                               zip(relation[10], deltas(relation[9]), relation[8], strict=True)]
                    yield "relation", signed(relation[1]), tags(relation[2], relation[3]), members
                else:
                    raise ValueError(f"unsupported OSM primitive {kind}")


def write_cli_output(cmd, output, timeout=120):
    """Measure launch through exit with stdout connected directly to a real file."""
    with output.open("wb") as stream:
        start = time.perf_counter_ns()
        result = subprocess.run(cmd, stdout=stream, stderr=subprocess.PIPE, timeout=timeout)
        elapsed = time.perf_counter_ns() - start
    if result.returncode:
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: "
                           + result.stderr.decode(errors="replace")[-1500:].strip())
    return elapsed, output.stat().st_size


def record_hash(row):
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                         allow_nan=False).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest(), "big")


def export_signature(path, *, allow_empty=False):
    records = xml_records(path) if path.suffix == ".xml" else pbf_records(path)
    counts = dict.fromkeys(KINDS, 0)
    total = 0
    # Hash each canonical object, then sum modulo 2**256 to check the
    # multiset without sorting it or retaining the decoded export in memory.
    for row in records:
        counts[row[0]] += 1
        total += record_hash(row)
    if not allow_empty and not sum(counts.values()):
        raise ValueError("export contains no OSM objects")
    return counts, f"{total % (1 << 256):064x}"
