from __future__ import annotations

import unittest

from mwbc.geometry import Point, Size, entry_position, local_exit_position, should_activate, should_exit


class GeometryTests(unittest.TestCase):
    def test_right_edge_activation(self) -> None:
        self.assertTrue(should_activate("right", Point(1919, 500), Size(1920, 1080), 2))
        self.assertFalse(should_activate("right", Point(1900, 500), Size(1920, 1080), 2))

    def test_entry_position_scales_cross_axis(self) -> None:
        position = entry_position("right", Point(1919, 540), Size(1920, 1080), Size(1280, 720))
        self.assertEqual(position.x, 1)
        self.assertAlmostEqual(position.y, 360, delta=1)

    def test_exit_returns_to_local_edge(self) -> None:
        remote_size = Size(1280, 720)
        self.assertTrue(should_exit("right", Point(0, 300), Point(-5, 0), remote_size))
        local = local_exit_position("right", Point(0, 300), Size(1920, 1080), remote_size)
        self.assertEqual(local.x, 1917)
        self.assertAlmostEqual(local.y, 450, delta=1)


if __name__ == "__main__":
    unittest.main()

