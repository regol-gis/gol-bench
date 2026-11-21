import unittest

from benchmark_storage import cache_capacity, cache_environment, validate_cache


class CacheConfigurationTests(unittest.TestCase):
    def test_inherited_cache_override_cannot_change_a_measurement(self):
        inherited = {"GOL_READER_BENCH_CACHE_BYTES": "bad", "EXAMPLE": "preserved"}
        self.assertEqual(
            cache_environment("cached", 16, inherited),
            {"GOL_READER_BENCH_CACHE_BYTES": "16777216", "EXAMPLE": "preserved"},
        )
        self.assertEqual(
            cache_environment("windowed", 16, inherited), {"EXAMPLE": "preserved"}
        )
        self.assertEqual(inherited["GOL_READER_BENCH_CACHE_BYTES"], "bad")

    def test_capacity_rounding_and_invalid_requests(self):
        self.assertEqual(cache_capacity(3), 2 * 1024**2)
        self.assertEqual(cache_capacity(16), 16 * 1024**2)
        for request in [0, -1, 0.5, True]:
            with self.assertRaises(ValueError):
                cache_capacity(request)

    def test_actual_worker_capacity_and_allocations_must_match(self):
        valid = {
            "storage_stats": {
                "cache_capacity_bytes": 2 * 1024**2,
                "allocated_cache_bytes": 4096,
            }
        }
        validate_cache(valid, "cached", 3)
        for capacity, allocated in [
            (3 * 1024**2, 4096),
            (2 * 1024**2, 3 * 1024**2),
            (2 * 1024**2, None),
            (2 * 1024**2, -1),
        ]:
            with self.assertRaises(ValueError):
                validate_cache(
                    {
                        "storage_stats": {
                            "cache_capacity_bytes": capacity,
                            "allocated_cache_bytes": allocated,
                        }
                    },
                    "cached",
                    3,
                )


if __name__ == "__main__":
    unittest.main()
