import pathlib
import tempfile
import unittest
from unittest import mock

from benchmark_cache import (
    CachePreparationError,
    FileCache,
    parse_snapshot,
    require_state,
)


class CacheState(unittest.TestCase):
    def test_partial_final_page_is_rounded_and_states_are_strict(self):
        path = pathlib.Path("/tmp/example.gol")
        document = {
            "fincore": [{"file": str(path), "size": 4097, "pages": 2, "res": 8192}]
        }
        warm = parse_snapshot(document, path, 4097, 4096)
        require_state(warm, "warm")
        with self.assertRaises(ValueError):
            require_state(warm, "cold")
        document["fincore"][0].update(pages=0, res=0)
        cold = parse_snapshot(document, path, 4097, 4096)
        require_state(cold, "cold")
        for state in ["warm", "unknown"]:
            with self.assertRaises(ValueError):
                require_state(cold, state)
        document["fincore"][0].update(pages=1, res=4096)
        partial = parse_snapshot(document, path, 4097, 4096)
        for state in ["warm", "cold"]:
            with self.assertRaises(ValueError):
                require_state(partial, state)

    def test_warm_mapping_faults_partial_final_page_without_modifying_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            contents = b"benchmark" * 2000
            path.write_bytes(contents)
            cache = FileCache(path)
            with cache.sample("warm") as observation:
                require_state(observation, "warm")
                self.assertEqual(observation["resident_pages"],
                                 (len(contents) + cache.page_size - 1) // cache.page_size)
                self.assertEqual(observation["file_bytes"], len(contents))
            self.assertEqual(path.read_bytes(), contents)

    def test_wrong_files_sizes_and_impossible_counts_are_rejected(self):
        row = {"file": "/tmp/example.gol", "size": 4097, "pages": 0, "res": 0}
        for change in [
            {"file": "/tmp/other.gol"},
            {"size": 4096},
            {"pages": -1},
            {"pages": True},
            {"pages": 3, "res": 12288},
            {"res": 1},
            {"pages": "0"},
            {"size": False},
        ]:
            with self.assertRaises(ValueError):
                parse_snapshot(
                    {"fincore": [dict(row, **change)]}, row["file"], 4097, 4096
                )
        for document in [
            {},
            [],
            {"fincore": []},
            {"fincore": [row, row]},
            {"fincore": [None]},
        ]:
            with self.assertRaises(ValueError):
                parse_snapshot(document, row["file"], 4097, 4096)

    def test_ignored_eviction_cannot_produce_a_cold_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            path.write_bytes(b"x")
            cache = object.__new__(FileCache)
            cache.path = path
            events = []
            cache.snapshot = mock.Mock(
                side_effect=lambda: events.append("observe")
                or {"resident_pages": 1, "total_pages": 1}
            )
            with mock.patch(
                "benchmark_cache.os.posix_fadvise",
                create=True,
                side_effect=lambda *args: events.append("evict"),
            ) as evict, mock.patch(
                "benchmark_cache.os.POSIX_FADV_DONTNEED", 4, create=True
            ), mock.patch(
                "benchmark_cache.time.sleep"
            ) as sleep:
                with self.assertRaises(CachePreparationError) as error:
                    cache.prepare("cold")
                self.assertEqual(len(error.exception.attempts), 5)
                self.assertEqual(events, ["evict", "observe"] * 5)
                self.assertEqual(evict.call_args.args[1:], (0, 0, 4))
                self.assertEqual(sleep.call_count, 4)
                cache.prepare("warm")
                self.assertEqual(evict.call_count, 5)

    def test_transient_residency_is_retained_before_verified_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            path.write_bytes(b"x")
            cache = object.__new__(FileCache)
            cache.path = path
            cache.snapshot = mock.Mock(
                side_effect=[
                    {"resident_pages": 1, "total_pages": 1},
                    {"resident_pages": 0, "total_pages": 1},
                ]
            )
            with mock.patch(
                "benchmark_cache.os.posix_fadvise", create=True
            ), mock.patch(
                "benchmark_cache.os.POSIX_FADV_DONTNEED", 4, create=True
            ), mock.patch(
                "benchmark_cache.time.sleep"
            ) as sleep:
                result = cache.prepare("cold")
            self.assertEqual(result["resident_pages"], 0)
            self.assertEqual(
                [r["resident_pages"] for r in result["preparation_attempts"]], [1, 0]
            )
            sleep.assert_called_once_with(0.25)

    def test_changed_file_is_rejected_before_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            path.write_bytes(b"x")
            cache = object.__new__(FileCache)
            cache.path = path
            info = path.stat()
            cache.identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            path.write_bytes(b"changed")
            with mock.patch("benchmark_cache.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    cache.snapshot()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
