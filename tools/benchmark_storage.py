"""Explicit bounded-cache configuration for benchmark workers."""

import os


def cache_capacity(cache_mib):
    if type(cache_mib) is not int or cache_mib <= 0:
        raise ValueError("cache size must be a positive integer MiB count")
    sets = cache_mib * 1024**2 // (4 * 4096)
    return (1 << (sets.bit_length() - 1)) * 4 * 4096


def cache_environment(storage, cache_mib, environment=None):
    cache_capacity(cache_mib)
    result = dict(os.environ if environment is None else environment)
    result.pop("GOL_READER_BENCH_CACHE_BYTES", None)
    if storage == "cached":
        result["GOL_READER_BENCH_CACHE_BYTES"] = str(cache_mib * 1024**2)
    return result


def validate_cache(row, storage, cache_mib):
    if storage != "cached":
        return
    expected = cache_capacity(cache_mib)
    stats = row["storage_stats"]
    if stats["cache_capacity_bytes"] != expected:
        raise ValueError("worker used an unexpected cache capacity")
    allocated = stats["allocated_cache_bytes"]
    if type(allocated) is not int or not 0 <= allocated <= expected:
        raise ValueError("worker exceeded its cache payload bound")
