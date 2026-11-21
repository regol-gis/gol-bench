"""Equivalent field consumption and untimed content validation for query workers."""

import struct

MASK = (1 << 64) - 1


def typed(feature):
    return feature.id * 4 + (0 if feature.is_node else 1 if feature.is_way else 2)


def mix(value):
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return value ^ (value >> 31)


def byte_hash(value, data):
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & MASK
    return value


def consume(query, operation, verify=False):
    if operation == "iterate" and not verify:
        count = sum(1 for _ in query)
        return {"features": count, "items": count, "sum": "0", "xor": "0"}
    count = items = total = xor = 0
    for feature in query:
        count += 1
        parent = typed(feature) if verify else 0
        if operation == "iterate":
            values = (parent,)
        elif operation == "tags":
            values = feature.tags
        elif operation == "positions" and feature.is_node:
            values = ((feature.x, feature.y),)
        elif operation == "positions" and feature.is_relation:
            values = ()
        elif operation in ("elevation", "peak_fields"):
            elevation = feature["ele"]
            label = ""
            xy = 0
            if operation == "peak_fields":
                label = next((v for key in ("name:cs", "name:en", "name")
                              if isinstance(v := feature[key], str) and v.strip()), "")
                xy = ((feature.x & 0xFFFFFFFF) << 32) | (feature.y & 0xFFFFFFFF)
            values = ((elevation, label, xy),)
        elif operation in ("coordinates", "positions"):
            values = ((node.x, node.y) for node in feature.nodes)
        elif operation == "way_nodes":
            values = ((i, node.id, node.x, node.y) for i, node in enumerate(feature.nodes))
        elif operation == "members":
            values = ((member, member.role) for member in feature.members)
        elif operation == "parents":
            values = feature.parents.relations
        elif operation == "geometry":
            values = (feature.shape,)
        else:
            raise ValueError(f"Unknown workload: {operation}")
        for value in values:
            items += 1
            if operation == "way_nodes" and value[1] == 0:
                raise ValueError("way_nodes workload requires retained OSM node IDs")
            if not verify:
                continue
            if operation == "tags":
                key, value = value
                item = byte_hash(14695981039346656037, key.encode("utf-8") + b"\xff")
                if isinstance(value, str):
                    item = byte_hash(item, b"T" + value.encode("utf-8"))
                else:
                    item = byte_hash(item, b"N" + struct.pack("<d", float(value)))
            elif operation in ("elevation", "peak_fields"):
                elevation, label, xy = value
                bits = (byte_hash(1, elevation.encode()) if isinstance(elevation, str)
                        else int.from_bytes(struct.pack("<d", float(elevation)), "little"))
                item = byte_hash(bits, label.encode()) ^ xy
            elif operation in ("coordinates", "positions"):
                x, y = value
                item = ((x & 0xFFFFFFFF) << 32) | (y & 0xFFFFFFFF)
            elif operation == "way_nodes":
                index, node_id, x, y = value
                xy = ((x & 0xFFFFFFFF) << 32) | (y & 0xFFFFFFFF)
                item = mix(index ^ mix(node_id)) ^ xy
            elif operation == "members":
                member, role = value
                item = typed(member) ^ byte_hash(
                    14695981039346656037, role.encode("utf-8")
                )
            elif operation == "parents":
                item = typed(value)
            elif operation == "geometry":
                item = geometry_hash(value)
            else:
                item = value
            hashed = mix(parent ^ mix(item))
            total = (total + hashed) & MASK
            xor ^= hashed
    return {"features": count, "items": items, "sum": str(total), "xor": str(xor)}


def sequence_hash(kind, points, start=0):
    value = byte_hash(14695981039346656037, kind + struct.pack("<Q", len(points)))
    for i in range(len(points)):
        x, y = points[(start + i) % len(points)]
        assert x == int(x) and y == int(y), "expected integer Mercator coordinates"
        value = byte_hash(value, struct.pack("<ii", int(x), int(y)))
    return value


def least_rotation(points):
    n = len(points)
    i, j, k = 0, 1, 0
    while i < n and j < n and k < n:
        a, b = points[(i + k) % n], points[(j + k) % n]
        if a == b:
            k += 1
            continue
        if a > b:
            i += k + 1
            if i <= j:
                i = j + 1
        else:
            j += k + 1
            if j <= i:
                j = i + 1
        k = 0
    return min(i, j)


def path_hash(points, ring=False):
    points = list(points)
    if ring and points and points[0] == points[-1]:
        points.pop()
    kind = b"R" if ring else b"L"
    forward = sequence_hash(kind, points, least_rotation(points) if ring else 0)
    points.reverse()
    reverse = sequence_hash(kind, points, least_rotation(points) if ring else 0)
    return min(forward, reverse)


def components_hash(kind, values):
    values = sorted(values)
    value = byte_hash(14695981039346656037, kind + struct.pack("<Q", len(values)))
    for child in values:
        value = byte_hash(value, struct.pack("<Q", child))
    return value


def geometry_hash(geometry):
    kind = geometry.geom_type
    if kind == "Point":
        return sequence_hash(b"P", list(geometry.coords))
    if kind == "LineString":
        return path_hash(geometry.coords)
    if kind == "Polygon":
        exterior = path_hash(geometry.exterior.coords, ring=True)
        holes = components_hash(
            b"H", (path_hash(r.coords, ring=True) for r in geometry.interiors)
        )
        return byte_hash(
            byte_hash(14695981039346656037, b"A"), struct.pack("<QQ", exterior, holes)
        )
    if kind in ("MultiPolygon", "GeometryCollection"):
        return components_hash(
            b"M" if kind == "MultiPolygon" else b"C",
            (geometry_hash(child) for child in geometry.geoms),
        )
    raise ValueError(f"Unsupported geometry for benchmark verification: {kind}")
