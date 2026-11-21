# Reader adapters

Install release workers from the repository root. Select just the components you intend to run. Python needs Python 3.13 with venv support, the JVM adapters need a JDK, and C++ needs CMake, Git and a C++20 compiler. The .NET adapter requires the .NET 10 SDK. PHP uses Docker. `GOL_BENCH_SDK_DIR` can point to an existing adapter installation. Set `GOL_BENCH_DOTNET` if the SDK executable is outside PATH.

```sh
./gol-bench setup regol python java cpp clojure
./gol-bench setup counts cpp java clojure dotnet rust-ffi
./gol-bench smoke --engines rust-paged,rust-safe,rust-mmap,python-current,cpp,java-current,clojure-current,dotnet,rust-ffi --output results/bindings
```

`counts` generates additional scalar adapters before building the selected SDKs. Include `counts` in the same setup call as the desired C++, .NET or FFI count adapters. To compare SDK generations, install `python-legacy java-legacy`. The legacy stack names are `python,java,clojure`, and the current names end in `-current`. GOL Tool release binaries are available for x86-64 Linux through `setup gol-tool gol-tool-legacy`. Regol CLI is `setup cli`. [Pinned SDKs, build options and prior results](../../benchmarks/sdk-pins.json)

```sh
./gol-bench stacks inputs/uk-geodesk.gol --preserve-order \
  --spec benchmarks/uk-corpus-queries.json \
  --engines rust-mmap,python-current,java-current,cpp --affinity one \
  --samples 5 --target-ms 25 --timeout 300 --output results/stacks.jsonl
./gol-bench audit results/stacks.jsonl --reference python-current --output results/audit.json
./gol-bench counts inputs/uk-geodesk.gol --engines rust-mmap,python-current,cpp \
  --affinity one --output results/counts.jsonl
./gol-bench cli-counts inputs/uk-geodesk.gol --engines regol gol-2.0.0 gol-2.5.3 \
  --output results/cli-counts.jsonl
./gol-bench audit-counts results/counts.jsonl results/cli-counts.jsonl --output results/count-audit.json
```

`stacks` runs one process at a time. It logs bounded case timeouts and crashes, preserves completed rows and starts a new process for the remaining cases. A whole-file checksum pass precedes timing. For 524-case matched comparisons use `compare`, which also controls order, performs independent verification and supports file eviction.

A feature handle, all tags and owned geometry are separate consumers. Identity digests include the OSM feature type and ID. Geometry verification compares coordinates, multiplicity, hole grouping and geometry types with normalized ring/line/component order. The Rust FFI binding eagerly materializes rich records and then copies them into Rust. It accepts degree-based bounding boxes. C++ tag traversal lacks the same typed content verification as the Python endpoint. The audit retains these differences.

The `php-ffi` adapter uses PHP 8.4 and an in-process C ABI over GeoDesk C++ to measure feature iteration and scalar counts. The original `php` extension is retained as a diagnostic because its supported public query calls failed. The PostgreSQL foreign-data wrapper has a separate, verified SQL driver. [Build pins, replay commands and measurement boundaries](php-fdw.md)

The file-advice experiment uses a separate safe build. Run `./gol-bench setup safe-worker` to install it under `target/safe`.

For a different Regol commit, use a fresh gol-bench clone and pass `./gol-bench setup regol --source /path/to/gol-reader --revision COMMIT`. This overlays the same public-API workload adapters onto the selected reader. The default source tags, tree hashes and original upstream revisions are recorded in the [reader source lock](../../benchmarks/regol-sources.json).

To update the project-wide pin, run `python3 tools/lock_regol.py /path/to/gol-reader COMMIT --label current` in a gol-bench checkout. Review and commit the changed lock and push its reported source tag along with the harness revision. The command writes a separate Git tree from the upstream commit, leaving local source edits outside the snapshot.
