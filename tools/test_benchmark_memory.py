import copy
import json
import unittest

from benchmark_memory import service_command, validate_worker
from benchmark_memory_worker import (
    cgroup_path,
    classify,
    parse_counters,
    require_limits,
)


class ReferenceAffinityTests(unittest.TestCase):
    def test_reference_can_come_from_one_cpu(self):
        from benchmark_memory import reference_results
        row=dict(name='map',engine='paged',affinity='one',count=3,fingerprint=7,work_fingerprint=None)
        self.assertEqual(reference_results({'results':[row]},['map']),{'map':row})
        with self.assertRaisesRegex(ValueError,'between CPU affinities'):
            reference_results({'results':[row,dict(row,affinity='all',count=4)]},['map'])
        with self.assertRaisesRegex(ValueError,'missing paged'):
            reference_results({'results':[row]},['missing'])


class MemoryProtocolTests(unittest.TestCase):
    def state(self, kills=0):
        return {
            "memory.max": "67108864",
            "memory.swap.max": "0",
            "memory.swap.current": "0",
            "memory.events": f"max 2\noom 1\noom_kill {kills}\n",
        }

    def test_unenforced_or_swapping_limits_are_rejected(self):
        require_limits(self.state(), 67108864)
        for key, value in [
            ("memory.max", "max"),
            ("memory.max", "134217728"),
            ("memory.swap.max", "max"),
            ("memory.swap.current", "4096"),
        ]:
            state = self.state()
            state[key] = value
            with self.assertRaises(ValueError):
                require_limits(state, 67108864)

    def test_signal_death_is_not_assumed_to_be_an_oom(self):
        self.assertEqual(
            classify(-9, False, self.state(), self.state()), "worker_error"
        )
        self.assertEqual(classify(-9, False, self.state(), self.state(1)), "oom")
        self.assertEqual(classify(-9, True, self.state(), self.state()), "timeout")
        self.assertEqual(classify(0, False, self.state(), self.state()), "success")
        with self.assertRaises(ValueError):
            classify(0, False, self.state(1), self.state())

    def test_cgroup_parser_rejects_root_duplicates_and_escaping_paths(self):
        self.assertEqual(
            str(cgroup_path("0::/user.slice/probe.service\n")),
            "/sys/fs/cgroup/user.slice/probe.service",
        )
        for text in ["", "0::/", "0::/../other", "0::relative", "0::/a\n0::/b"]:
            with self.assertRaises(ValueError):
                cgroup_path(text)
        for text in ["oom_kill -1", "oom_kill 1\noom_kill 2", "bad"]:
            with self.assertRaises(ValueError):
                parse_counters(text)

    def test_failed_workers_never_supply_latency(self):
        for outcome in ["oom", "timeout"]:
            self.assertIsNone(
                validate_worker(
                    {"outcome": outcome, "stdout": "partial"}, {}, "paged", 3, 32
                )
            )
        with self.assertRaises(ValueError):
            validate_worker({"outcome": "worker_error"}, {}, "paged", 3, 32)

    def test_success_requires_matching_content_and_configuration(self):
        expected = {
            "name": "tags",
            "count": 2,
            "fingerprint": {"count": 2, "sum": "3", "xor": "3"},
            "work_fingerprint": {"sum": "4"},
        }
        row = expected | {
            "engine": "paged",
            "samples_wall_ns": [1, 2, 3],
            "repeats": 1,
            "threads": 32,
            "build_features": {"mmap": False},
            "read_advice": "normal",
        }
        supervisor = {"outcome": "success", "stderr": "", "stdout": json.dumps(row)}
        self.assertEqual(validate_worker(supervisor, expected, "paged", 3, 32), row)
        mapped = row | {
            "engine": "mmap-immutable",
            "build_features": {"mmap": True},
            "read_advice": "default",
        }
        self.assertEqual(
            validate_worker(
                supervisor | {"stdout": json.dumps(mapped)},
                expected,
                "mmap-immutable",
                3,
                32,
            ),
            mapped,
        )
        for key, value in [
            ("build_features", {"mmap": False}),
            ("read_advice", "normal"),
        ]:
            with self.assertRaises(ValueError):
                validate_worker(
                    supervisor | {"stdout": json.dumps(mapped | {key: value})},
                    expected,
                    "mmap-immutable",
                    3,
                    32,
                )
        launch = service_command(
            "probe", ["worker"], 67108864, [0], 10, read_advice="default"
        )
        self.assertIn("--setenv=GOL_READER_BENCH_READ_ADVICE=default", launch)
        for key, value in [
            ("count", 1),
            ("work_fingerprint", None),
            ("engine", "python"),
            ("threads", 1),
            ("repeats", 2),
            ("samples_wall_ns", [1]),
            ("build_features", {"mmap": True}),
            ("read_advice", "random"),
        ]:
            damaged = copy.deepcopy(row)
            damaged[key] = value
            with self.assertRaises(ValueError):
                validate_worker(
                    supervisor | {"stdout": json.dumps(damaged)},
                    expected,
                    "paged",
                    3,
                    32,
                )


if __name__ == "__main__":
    unittest.main()
