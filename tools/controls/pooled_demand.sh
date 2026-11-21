#!/bin/sh
# The corpus runner and verifier both invoke this beside the instrumented mmap worker.
export GOL_BENCH_PREFETCH_CONTROL_V3=neither
exec "$(dirname "$0")/query_benchmark_mmap" "$@"
