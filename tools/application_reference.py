"""Application reader stages that need native GeoDesk spatial operations or export."""
import hashlib
import json
import math
import re
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def elevations(feature):
    value = feature["ele"]
    # GOL numeric and OSM textual elevations both occur in the UK file.
    match = re.match(r"\s*(-?[0-9]+)", str(value))
    return int(match[1]) if match else 0


def degrees_per_metre(lat):
    dy = 180 / (math.pi * 6371000)
    return dy / math.cos(math.radians(lat)), dy


def pixel_rows(case):
    lon, lat = case["northwest"]
    size, width = case["grid_side"], case["pixel_metres"]
    dx, dy = degrees_per_metre(lat)
    for y in range(size):
        yield [(lon+(x+0.5)*width*dx, lat-(y+0.5)*width*dy) for x in range(size)]


def native_session(case, gol, temp, detailed):
    """Measure reader stages separately from verification and adapter startup."""
    steps = []

    def measure(name, operation, verify):
        start = time.perf_counter_ns()
        result = operation()
        elapsed = time.perf_counter_ns() - start
        count, fingerprint = verify(result)
        steps.append(dict(name=name, elapsed_ns=elapsed, items=count, fingerprint=fingerprint))
        return result

    from geodesk import Box, Features

    if case.get("preload_shapely"):
        import shapely  # The notebook imports GeoPandas before opening its GOL.

    def open_library(name):
        return measure(name, lambda: Features(str(gol)), lambda _: (1, None))

    def projected(rows):
        return len(rows), digest(sorted(rows)) if detailed else None

    def checked_count(n):
        return n, digest(n) if detailed else None

    if case["id"] == "cesium":
        for i, (lon, lat, radius) in enumerate(case["views"]):
            library = open_library(f"open-{i}")
            hits = library.around(lon=lon, lat=lat, meters=radius)
            measure(f"count-{i}", lambda: hits.count, checked_count)

            def positions():
                points = []
                for feature in hits:
                    if feature.is_node:
                        points.append((feature.lon, feature.lat))
                    elif feature.is_way:
                        points.extend((node.lon, node.lat) for node in feature.nodes)
                return points

            measure(f"positions-{i}", positions, projected)
        return steps

    library = open_library("open")
    if case["id"] == "world2pixel":
        lon, lat = case["northwest"]
        size, width = case["grid_side"], case["pixel_metres"]
        lon_per_m, lat_per_m = degrees_per_metre(lat)
        margin = 100
        inner = library(Box(west=lon-margin*lon_per_m, south=lat-margin*lat_per_m,
                            east=lon+(size*width+margin)*lon_per_m, north=lat+margin*lat_per_m))
        radius = width / 2 + case["radius_add_metres"]

        for y, centres in enumerate(pixel_rows(case)):
            measure(f"row-{y}", lambda: [bool(inner(case["query"]).around(lon=x, lat=y, meters=radius))
                                         for x, y in centres], lambda row: (sum(row), digest(row) if detailed else None))
    elif case["id"] == "next-higher":
        cities = measure("cities", lambda: sorted((c.id, c.x, c.y) for c in library(case["cities"])),
                         projected)
        if [c[0] for c in cities] != case["seed_nodes"]:
            raise ValueError("city seeds differ from the captured UK trace")
        peaks = library(case["peaks"])
        for city, _, _ in cities:
            def highest():
                return max((ele, p.id) for p in peaks if (ele := elevations(p)) < 9000)

            measure(f"highest-for-{city}", highest, lambda row: (1, digest(row)))

            def projections():
                rows = []
                for p in peaks:
                    ele = elevations(p)
                    if ele < 9000:
                        name = next((n for k in ("name:cs", "name:en", "name")
                                     if (n := p[k]) and str(n).strip()), "Bezejmenný vrchol")
                        rows.append((p.id, p.x, p.y, ele, name))
                return rows

            measure(f"profile-for-{city}", projections, projected)
    elif case["id"] == "campsbourne":
        w, s, e, n = case["window"]
        local = library(Box(west=w, south=s, east=e, north=n))
        measure("local-feature-count", lambda: local.count, checked_count)
        roads = local(case["query"])
        road_count = measure("named-road-count", lambda: roads.count, checked_count)
        output = temp / "roads.geojson"
        measure("named-road-export", lambda: roads.geojson.save(str(output)),
                lambda _: (projected([(f["id"], f) for f in json.loads(output.read_bytes())["features"]])
                           if detailed else (road_count, None)))
    elif case["id"] == "varna":
        city = measure("city-boundary", lambda: library(case["area"]).one,
                       lambda a: (1, digest(a.id)))

        if city.id != case["seed_relation"] or [city.bounds.minx, city.bounds.miny, city.bounds.maxx, city.bounds.maxy] != case["seed_bounds"]:
            raise ValueError("city boundary differs from the captured UK trace")

        def roads():
            rows = []
            for way in library.ways(case["roads"]).intersecting(city):
                nodes = tuple((n.id, n.x, n.y) for n in way.nodes)
                rows.append((way.id, str(way["maxspeed"]), nodes))
            return rows

        measure("intersecting-road-vertices", roads, projected)
    else:
        raise ValueError(f"unsupported application {case['id']}")
    return steps
