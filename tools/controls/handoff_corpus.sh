#!/bin/sh
# Use one verified executable for the candidate and for the demand-worker control.
case "$9" in
    1) export GOL_BENCH_TILE_FETCH=leaf/32 ;;
    32) unset GOL_BENCH_TILE_FETCH ;;
    *) exit 2 ;;
esac
export GOL_BENCH_PREFETCH_CONTROL_V3=neither
exec "$(dirname "$0")/query_benchmark_mmap" "$@"
