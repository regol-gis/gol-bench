"""Catch aggregate counts hiding a bad step or unverified early termination."""

import copy
import unittest

from benchmark_sessions import delivered_count


class SessionValidationTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"steps": [{"query": "*"}, {"query": "n", "limit": 3}]}
        self.record = {
            "count": 7,
            "fingerprint": {"count": 14, "steps": [{"count": 4}, {"count": 10}]},
            "step_results": [{"count": 4}, {"count": 3, "membership_verified": True}],
        }

    def test_full_and_limited_counts(self):
        self.assertEqual(delivered_count(self.spec, self.record), 7)
        self.spec["steps"][1]["limit"] = 0
        self.record["step_results"][1]["count"] = 0
        self.record["count"] = 4
        self.assertEqual(delivered_count(self.spec, self.record), 4)

    def test_equal_total_cannot_hide_bad_steps(self):
        self.record["step_results"][0]["count"] = 5
        self.record["step_results"][1]["count"] = 2
        with self.assertRaises(ValueError):
            delivered_count(self.spec, self.record)

    def test_membership_and_structure_required(self):
        for mutate in [
            lambda r: r["step_results"][1].pop("membership_verified"),
            lambda r: r["step_results"].pop(),
            lambda r: r["fingerprint"]["steps"].pop(),
            lambda r: r["fingerprint"].update(count=15),
            lambda r: r.update(count=8),
        ]:
            record = copy.deepcopy(self.record)
            mutate(record)
            with self.assertRaises(ValueError):
                delivered_count(self.spec, record)


if __name__ == "__main__":
    unittest.main()
