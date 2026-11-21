# gol-bench

Reproducible GeoDesk GOL 2.0 workload, engine, CLI, and PBF-to-GOL writer benchmarks for [regol](https://github.com/regol-gis/regol).

Live historical results are published at **[https://bench.regol.org](https://bench.regol.org)** from the `gh-pages` branch of this repository.

## Suites

- **`writers` (`build-compare`)** — PBF-to-GOL construction time, peak RSS, output file size, and verified output equivalence (`regol build` vs `gol-tool`).
- **`scale`** — Ordered geographic viewport query histories across dataset scales (`tiny`, `monaco`, `liechtenstein`, `bremen`, `ghana`, `uk`, `planet`) comparing `regol` against GeoDesk Python (and optional C++, Java, Clojure, .NET, Rust FFI, PHP FFI bindings).
- **`cli`** — Fresh-process count and real `csv`, `geojson`, `geojsonl`, `xml`, and `pbf` exports with output equivalence verification (`regol` vs `gol-tool`).
- **`applications`** — Ten ordered UK application replay workflows comparing matched reader stages and CLI exports.

## Quick start

Requires Linux, Git, Python 3.11+ (with `venv`), a Rust toolchain, `util-linux` (`taskset`, `fincore`), and `zstd`.

```sh
# Install pinned workers (builds regol from https://github.com/regol-gis/regol)
./gol-bench setup regol mmap-worker cli writer python gol-tool gol-tool-legacy --jobs 4

# Smoke test on the synthetic fixture
./gol-bench smoke --engines rust-mmap,python-current \
  --rust target/release/examples/query_benchmark_mmap --output results/smoke

# Dataset scale comparison (downloads checksum-verified Geofabrik PBFs)
./gol-bench scale --dataset monaco --dataset liechtenstein --dataset bremen \
  --engines regol python --cores 1 --rounds 5 --cache warm \
  --rust target/release/examples/query_benchmark_mmap --output results/scale

# PBF-to-GOL writer comparison
./gol-bench writers --dataset bremen --engines regol geodesk-current \
  --cores 1 4 --rounds 3 --cache warm --verification tags --output results/writers

# CLI count and export formats
bremen=$(./gol-bench datasets prepare bremen)
./gol-bench cli "$bremen" --query 'n[amenity=cafe]' --query 'w[highway]' \
  --samples 5 --cores 2 --output results/cli.json
```

Run `./gol-bench list` for all available subcommands, or `python3 -m unittest discover -s tools -p 'test_*.py'` to run harness unit tests.

## Automated publishing (`bench.regol.org`)

`runner/run_public_bench.py` checks `https://github.com/regol-gis/regol` for newly merged `main` commits or `v*` tags, runs the public benchmark suite on dedicated bare-metal hardware, verifies every output, and updates the compact JSON time series on the `gh-pages` branch.

## License

The benchmark harness is licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)). External benchmarked engines retain their own licenses. OpenStreetMap data extracts are © OpenStreetMap contributors (ODbL 1.0).
