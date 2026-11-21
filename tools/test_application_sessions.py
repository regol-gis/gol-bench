"""Check the request history, library lifetimes and input checks in the application suite."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from application_export import export_signature
from application_reference import pixel_rows
from benchmark_application_sessions import (CASES, ROOT, crc32c, job_engines, native_digests,
                                            reader_batches, round_order, verified_inputs)
from fetch_application_gol import fetch
import fetch_native_snapshot
import fetch_application_pbf
from make_fixture import dataset, encode


class ApplicationSessionsTest(unittest.TestCase):
    def test_pans_reopen_or_reuse_library_as_in_source(self):
        osm2world = reader_batches(CASES["osm2world"])
        self.assertEqual([len(batch) for batch in osm2world], [3, 3, 3])
        for batch in osm2world:
            self.assertEqual([row["operation"] for row in batch], ["tags", "way_nodes", "members"])
            self.assertEqual(len({tuple(row["bounds"]) for row in batch}), 1)
        self.assertLess(osm2world[1][0]["bounds"][0], osm2world[0][0]["bounds"][2])
        ascend, = reader_batches(CASES["ascend"])
        self.assertEqual([row["name"] for row in ascend],
                         [f"{i}-{layer}" for i in range(3) for layer in ("places", "water", "roads")])

    def test_pixel_raster_has_distinct_ordered_early_exit_queries(self):
        case = CASES["world2pixel"]
        rows = list(pixel_rows(case))
        self.assertEqual([len(line) for line in rows], [12] * 12)
        self.assertLess(rows[0][0][0], rows[0][1][0])
        self.assertGreater(rows[0][0][1], rows[1][0][1])
        queries, = reader_batches(case)
        self.assertEqual(len({tuple(q["bounds"]) for q in queries}), 144)
        self.assertEqual([q["name"] for q in queries],
                         [f"pixel-{y}-{x}" for y in range(12) for x in range(12)])
        self.assertTrue(all(q["limit"] == 1 for q in queries))

    def test_each_application_has_traceable_sources_and_a_multistep_session(self):
        self.assertEqual(len(CASES), 10)
        for case in CASES.values():
            with self.subTest(case=case["id"]):
                self.assertGreater(sum(map(len, reader_batches(case))), 1)
                for source in case["sources"]:
                    self.assertRegex(source["url"], r"/blob/[0-9a-f]{40}/.*#L[0-9]+")
                    self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")

    def test_readers_only_replays_the_matched_pair_for_each_of_the_ten(self):
        for case in CASES.values():
            engines = job_engines(case, "readers")
            self.assertEqual(engines, ["rust", "python"] if case["replay"] == "reader"
                             else ["rust-reader", "python-reader"])

    def test_rounds_preserve_sessions_and_counterbalance_engines(self):
        jobs = [(case, engine) for case in ("a", "b", "c") for engine in ("rust", "python")]
        for n in range(3):
            self.assertCountEqual(round_order(jobs, n), jobs)
        for n in (0, 2):
            self.assertEqual(round_order(jobs, n), round_order(jobs, n + 1)[::-1])

    def test_two_job_rounds_alternate_and_three_job_rounds_cover_every_order(self):
        pair = ["native", "rust"]
        self.assertEqual([round_order(pair, n) for n in range(4)],
                         [pair, pair[::-1], pair, pair[::-1]])
        three = [*pair, "python"]
        self.assertEqual(len({tuple(round_order(three, n)) for n in range(6)}), 6)

    def test_cli_comparison_selects_both_executables_and_has_all_pins(self):
        pins = json.loads((ROOT / "benchmarks/application-export-signatures.json").read_text())
        for case in CASES.values():
            if case["replay"] == "cli":
                self.assertEqual(job_engines(case, "cli"), ["cli", "rust-cli"])
                self.assertEqual(job_engines(case, "rust"), ["rust-reader", "rust-cli"])
                self.assertEqual(set(pins[case["id"]]), {name for name, _ in case["polygons"]})
            else:
                self.assertFalse(job_engines(case, "cli"))

    def test_export_signature_checks_ids_coordinates_tags_and_ordered_members(self):
        data = dataset()
        root = ET.Element("osm", version="0.6")
        for kind in ("relation", "way", "node"):
            for obj in reversed(data[kind + "s"]):
                element = ET.SubElement(root, kind, id=str(obj["id"]))
                if kind == "node":
                    for coord in ("lat", "lon"):
                        element.set(coord, f"{obj[coord]:.8f}")
                elif kind == "way":
                    for ref in obj["nodes"]:
                        ET.SubElement(element, "nd", ref=str(ref))
                else:
                    for member_kind, ref, role in obj["members"]:
                        ET.SubElement(element, "member", type=member_kind, ref=str(ref), role=role)
                for key, value in reversed(list(obj["tags"].items())):
                    ET.SubElement(element, "tag", k=key, v=value)
        with tempfile.TemporaryDirectory() as scratch:
            pbf = Path(scratch) / "result.pbf"
            xml = Path(scratch) / "result.xml"
            pbf.write_bytes(encode(data))
            ET.ElementTree(root).write(xml, encoding="utf-8")
            expected = export_signature(pbf)
            self.assertEqual(export_signature(xml), expected)
            for xpath, attribute, value in [
                ("node", "id", "1000"),
                ("node", "lon", "0.000000001"),
                ("node/tag", "v", "different tag"),
                ("way/nd", "ref", "1000"),
                ("relation/member", "role", "different role"),
                ("relation/member", "type", "way"),
            ]:
                with self.subTest(xpath=xpath, attribute=attribute):
                    element = root.find(xpath)
                    original = element.get(attribute)
                    element.set(attribute, value)
                    ET.ElementTree(root).write(xml, encoding="utf-8")
                    self.assertNotEqual(export_signature(xml), expected)
                    element.set(attribute, original)
            for kind in ("way", "relation"):
                with self.subTest(reorder=kind):
                    element = root.find(kind)
                    element.append(element[0])
                    del element[0]
                    ET.ElementTree(root).write(xml, encoding="utf-8")
                    self.assertNotEqual(export_signature(xml), expected)
                    element.insert(0, element[-1])
                    del element[-1]

    def test_invalid_existing_input_is_preserved(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "input.gol"
            path.write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "different SHA-256"):
                fetch(path, "", "", "")
            self.assertEqual(path.read_bytes(), b"other")

    def test_native_layout_hash_masks_ids_only_after_validating_the_crc(self):
        self.assertEqual(crc32c(b"123456789"), 0xe3069283)

        def make_file(store_id, tile_id, payload=b"tile"):
            metadata = bytearray(504)
            metadata[8:16] = store_id
            metadata[64:80] = tile_id
            return metadata + crc32c(metadata).to_bytes(4, "little") + payload

        with tempfile.TemporaryDirectory() as scratch:
            reference = Path(scratch) / "reference.gol"
            native = Path(scratch) / "native.gol"
            reference_data = b"reference input"
            reference.write_bytes(reference_data)
            native.write_bytes(make_file(b"1" * 8, b"2" * 16))
            original_exact, original_layout = native_digests(native)
            pins = dict(layouts={"original": dict(bytes=native.stat().st_size,
                                                  layout_sha256=original_layout)})
            reference_spec = dict(input_bytes=len(reference_data),
                                  input_sha256=hashlib.sha256(reference_data).hexdigest())
            with patch("benchmark_application_sessions.SPEC", reference_spec):
                native.write_bytes(make_file(b"3" * 8, b"4" * 16))
                inputs = verified_inputs(reference, native, pins)
                self.assertEqual(inputs["native"]["layout_sha256"], original_layout)
                self.assertEqual(inputs["native"]["layout"], "original")
                self.assertNotEqual(inputs["native"]["sha256"], original_exact)
                native.write_bytes(make_file(b"1" * 8, b"2" * 16, b"data"))
                with self.assertRaisesRegex(ValueError, "layout SHA-256"):
                    verified_inputs(reference, native, pins)
                _, alternate_layout = native_digests(native)
                pins["layouts"]["alternate"] = dict(bytes=native.stat().st_size,
                                                    layout_sha256=alternate_layout)
                self.assertEqual(verified_inputs(reference, native, pins)["native"]["layout"],
                                 "alternate")
                data = make_file(b"1" * 8, b"2" * 16)
                data[8] ^= 1
                native.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "header checksum"):
                    verified_inputs(reference, native, pins)

    def test_pbf_cache_hit_verifies_size_and_sha256(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.osm.pbf"
            manifest = Path(scratch) / "source.json"
            data = b"pbf content"
            manifest.write_text(json.dumps(dict(pbf_bytes=len(data),
                                                pbf_sha256=hashlib.sha256(data).hexdigest())))
            with patch.object(fetch_application_pbf, "PROVENANCE", manifest):
                path.write_bytes(data)
                fetch_application_pbf.fetch(path)
                path.write_bytes(bytes(len(data)))
                with self.assertRaisesRegex(ValueError, "PBF SHA-256"):
                    fetch_application_pbf.fetch(path)
                path.write_bytes(b"truncated")
                with self.assertRaisesRegex(ValueError, "PBF size"):
                    fetch_application_pbf.fetch(path)

    def test_pbf_download_verifies_content_before_publish(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.osm.pbf"
            manifest = Path(scratch) / "source.json"
            data = b"pbf content"
            manifest.write_text(json.dumps(dict(pbf_bytes=len(data),
                                                pbf_sha256=hashlib.sha256(data).hexdigest(),
                                                source_url="https://example.test/source.pbf")))

            def write_download(command, **_):
                self.assertEqual(command[-1], "https://example.test/source.pbf")
                command[command.index("--output") + 1].write_bytes(payload)

            with patch.object(fetch_application_pbf, "PROVENANCE", manifest), patch.object(
                    fetch_application_pbf.subprocess, "run", side_effect=write_download):
                payload = bytes(len(data))
                with self.assertRaisesRegex(ValueError, "PBF SHA-256"):
                    fetch_application_pbf.fetch(path)
                self.assertFalse(path.exists())
                payload = data
                fetch_application_pbf.fetch(path)
                self.assertEqual(path.read_bytes(), data)

    def test_pbf_lfs_uses_the_original_source_pin(self):
        source = json.loads(fetch_application_pbf.PROVENANCE.read_bytes())
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "source.osm.pbf"
            with patch.dict("os.environ", {
                "GITHUB_SERVER_URL": "https://forge.example.test",
                "GITHUB_REPOSITORY": "example/regol",
                "BENCHMARK_DOWNLOAD_TOKEN": "test-token",
            }), patch.object(fetch_application_pbf, "fetch_lfs") as download:
                fetch_application_pbf.fetch(path, lfs=True)
                download.assert_called_once_with(
                    path, "https://forge.example.test", "example/regol", "test-token",
                    sha256=source["pbf_sha256"], size=source["pbf_bytes"],
                    tag=fetch_application_pbf.LFS_TAG, parts_path=fetch_application_pbf.LFS_PARTS,
                )

    def test_native_control_verified_before_publication(self):
        original = b'reference data'
        digest = hashlib.sha256(original).hexdigest()
        record = {'bytes': len(original), 'sha256': digest, 'parts': []}
        manifest = {'tag': 'test', 'default': record, 'delta': record, 'control': record}
        with tempfile.TemporaryDirectory() as scratch:
            dest = Path(scratch) / 'control.gol'
            def reconstruct(command, *, check):
                command[-1].write_bytes(b'x' * len(original))
            with patch.object(fetch_native_snapshot, 'fetch'), \
                 patch.object(fetch_native_snapshot.subprocess, 'run', side_effect=reconstruct):
                with self.assertRaisesRegex(ValueError, 'wrong checksum'):
                    fetch_native_snapshot.download(dest, 'merged-standard', manifest, '', '', '')
            self.assertFalse(dest.exists())
            self.assertEqual(list(dest.parent.iterdir()), [])
            dest.write_bytes(original)
            with patch.object(fetch_native_snapshot, 'fetch') as download:
                fetch_native_snapshot.download(dest, 'merged-standard', manifest, '', '', '')
                download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
