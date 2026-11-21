# PHP FFI and PostgreSQL FDW adapters

## PHP

`php-ffi` runs PHP 8.4.27 with its FFI extension in a network-disabled Docker container. It links a narrow C ABI against the pinned GeoDesk C++ 2.2.1 SDK with GEOS disabled. PHP opens the GOL once and pulls one typed ID at a time from the C++ iterator. `counts` calls the C++ scalar count API in the same process. The iteration worker supports whole-file and projected bounding-box selectors. The workload covers IDs and counts. Tags, geometry, early stops and session workloads are outside its scope.

`php` refers to the separately pinned upstream `zitsky/php-libgeodesk` extension. The native extension loaded on PHP 8.4.27 NTS and opened valid tiny and regional GOLs. It exited during its public `Features::query()` call, before `count()` or `toArray()` returned. The call constructs a GeoDesk view and marshals it through the pinned PHP-CPP layer. A local compatibility trial gave inconsistent results and supplied no benchmark timings. The upstream source advertises PHP 8.0+, with PHP 8.1+ for the PIE install method. The public repository had one commit and no tags or releases when checked. [Upstream source](https://github.com/zitsky/php-libgeodesk) · [GitHub releases](https://api.github.com/repos/zitsky/php-libgeodesk/releases)

The unchanged pinned extension and submodules also built against PHP 8.3.37 NTS in the official Bookworm CLI image. The Linux amd64 image digest was `sha256:69bd66659e3d6ef080bcbba47e1b4a31abdea89cd6aadab34e51bf4399fff0cc`. The module loaded and opened the tiny fixture, then exited with status 139 during `Features::query('n')`. The run stopped at that failure, before the Liechtenstein check. [Official PHP image](https://hub.docker.com/_/php)

```sh
./gol-bench setup python php-ffi --jobs 4
region=$(./gol-bench datasets prepare liechtenstein)
for suite in stacks counts; do
  ./gol-bench "$suite" "$region" --spec benchmarks/php-ffi-smoke.json \
    --engines php-ffi,python-current --samples 5 --target-ms 12 --preserve-order \
    --output "results/php-ffi-$suite-liechtenstein.jsonl"
done
./gol-bench audit results/php-ffi-stacks-liechtenstein.jsonl \
  --corpus benchmarks/php-ffi-smoke.json --reference python-current \
  --output results/php-ffi-audit-liechtenstein.json
./gol-bench audit-counts results/php-ffi-counts-liechtenstein.jsonl \
  --output results/php-ffi-count-audit-liechtenstein.json
```

The same nine selectors can be run against `tests/fixtures/query_semantics.gol`. The local verification matched every typed-identity digest and scalar count against Python on both files. On the locked Liechtenstein GOL the full scan returned 60,682 features, with five-sample medians of 4.477 ms for PHP typed-ID iteration and 0.218 ms for the native scalar count. The Vaduz building selector returned 6,522 features, with medians of 0.510 and 0.047 ms. These are one-host observations. Python's iteration timer consumes feature handles, and PHP FFI's timer includes a call and typed-ID projection per feature.

## PostgreSQL

`fdw setup` builds upstream revision `5d1bc202` with a PostgreSQL 16 compatibility patch, GeoDesk C++ 2.2.1 and PostGIS 3.4.1 source for LWGEOM. The patch preserves full GOQL passed through the table's `query` option when no additional filter is present. Each run starts a disposable cluster in a network-disabled container and connects over a private Unix socket. PostgreSQL runs as the user's effective UID or the image's unprivileged PostgreSQL account for root callers. The GOL is mounted read-only and must be readable by that account. The cluster is discarded with the container.

```sh
./gol-bench setup python
./gol-bench fdw setup --jobs 4
region=$(./gol-bench datasets prepare liechtenstein)
for replay in 1 2; do
  ./gol-bench fdw run "$region" --samples 7 \
    --query n --query w --query r --query '*' --query 'n[amenity]' --query 'a[building]' \
    --output "results/fdw-liechtenstein-$replay.json"
done
python3 - <<'PY'
import json
from pathlib import Path

first, second = [
    json.loads(Path(f"results/fdw-liechtenstein-{replay}.json").read_text())
    for replay in (1, 2)
]

def identities(report):
    assert report["verified"]
    return report["input"]["sha256"], [
        (case["query"], case["sql_count"], case["fingerprint"])
        for case in report["cases"]
    ]

assert identities(first) == identities(second)
PY
```

The container-to-connection interval includes cluster initialization and the first psql output. Setup uses separate client timings. The first and steady SQL statement intervals use PostgreSQL's `EXPLAIN ANALYZE` planning and execution measurements. The backend persists, and the FDW opens the GOL during each scan. Execution excludes client transfer. Whole-file GOQL runs inside the FDW. This workload checks the `type, fid` projection and SQL `count(*)` against Python. SQL counts scan foreign tuples, while Python uses a native scalar query. SQL predicate translation, spatial pushdown and geometry are outside the measured workload. Python verification runs after SQL timing, and the input checksum pass precedes both.

The tiny fixture matched 43 typed IDs. Two fresh runs on the locked Liechtenstein GOL matched all six selections, including 60,682 identities for the full scan and 17,791 buildings. The seven-sample full-scan SQL medians were 4.544 and 3.447 ms for projection and 4.252 and 3.035 ms for aggregation. [Dataset lock](../../benchmarks/datasets.json) · [FDW compatibility patch](fdw-postgres16.patch)
