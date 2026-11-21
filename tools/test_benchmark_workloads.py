"""Check benchmark normalization independently of either GOL reader."""

import itertools
import unittest

from benchmark_workloads import least_rotation, path_hash, sequence_hash


class GeometryFingerprintTests(unittest.TestCase):
    def test_rotations_match_exhaustive_reference(self):
        for size in range(1, 8):
            for points in itertools.product([(-1, 0), (0, 0), (1, 2)], repeat=size):
                rotations = [points[i:] + points[:i] for i in range(size)]
                self.assertEqual(rotations[least_rotation(points)], min(rotations))

    def test_ring_adjacency_and_repeated_vertices_are_preserved(self):
        ring = [(0, 0), (2, 0), (0, 0), (0, 2)]
        expected = path_hash(ring + ring[:1], ring=True)
        for direction in [ring, list(reversed(ring))]:
            for i in range(len(ring)):
                shifted = direction[i:] + direction[:i]
                self.assertEqual(path_hash(shifted + shifted[:1], ring=True), expected)
        a = [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]
        b = [(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)]
        self.assertNotEqual(path_hash(a, ring=True), path_hash(b, ring=True))
        self.assertNotEqual(path_hash(a, ring=True), path_hash(a))

    def test_shared_rust_golden_and_fractional_coordinate_rejection(self):
        self.assertEqual(sequence_hash(b"P", [(-17, 29)]), 13595356716078976439)
        with self.assertRaises(AssertionError):
            sequence_hash(b"P", [(-17.5, 29)])


if __name__ == "__main__":
    unittest.main()
