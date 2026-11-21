import unittest

from benchmark_output import fingerprint_ids, startup_specs


class OutputTests(unittest.TestCase):
    def test_reference_requires_the_selected_rust_backend_and_python(self):
        digest = fingerprint_ids([])
        for storage in ["paged", "file", "windowed", "cached", "owned", "mmap-immutable"]:
            reference = {
                "storage": storage,
                "queries": [{"name": "x", "query": "n"}],
                "results": [
                    {"name": "x", "engine": engine, "fingerprint": digest}
                    for engine in [storage, "python"]
                ],
            }
            self.assertEqual(startup_specs(reference, ["x"])[1], {"x": digest})
            reference["results"].pop()
            with self.assertRaises(ValueError):
                startup_specs(reference, ["x"])
        reference["storage"] = "unknown"
        with self.assertRaises(ValueError):
            startup_specs(reference, ["x"])

    def test_typed_id_validation_and_order_independence(self):
        ids = ["node/0", "way/0", "relation/0", "node/4503599627370495"]
        self.assertEqual(fingerprint_ids(ids), fingerprint_ids(list(reversed(ids))))
        self.assertEqual(fingerprint_ids([]), {"count": 0, "sum": "0", "xor": "0"})
        self.assertNotEqual(fingerprint_ids(["node/7"]), fingerprint_ids(["way/7"]))
        for bad in [
            None,
            {},
            [1],
            ["node/-1"],
            ["area/1"],
            ["node/１"],
            ["node/4503599627370496"],
            ["node/1", "node/01"],
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fingerprint_ids(bad)

    def test_reference_rejects_unequal_or_unsupported_work(self):
        digest = fingerprint_ids(["node/1"])
        reference = {
            "queries": [{"name": "x", "query": "n"}],
            "results": [
                {"name": "x", "engine": engine, "fingerprint": digest}
                for engine in ["paged", "python"]
            ],
        }
        specs, expected = startup_specs(reference, ["x"])
        self.assertEqual(specs, reference["queries"])
        self.assertEqual(expected, {"x": digest})
        for names in [[], ["x", "x"], ["missing"]]:
            with self.assertRaises(ValueError):
                startup_specs(reference, names)
        for extra in [{"limit": 1}, {"operation": "tags"}, {"steps": []}]:
            reference["queries"][0].update(extra)
            with self.assertRaises(ValueError):
                startup_specs(reference, ["x"])
            for key in extra:
                del reference["queries"][0][key]
        reference["results"][1]["fingerprint"] = fingerprint_ids(["node/2"])
        with self.assertRaises(ValueError):
            startup_specs(reference, ["x"])


if __name__ == "__main__":
    unittest.main()
