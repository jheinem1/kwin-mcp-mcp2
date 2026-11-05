from __future__ import annotations

import copy
import unittest
from typing import Any

from kwin_mcp.coordinates import map_point, output_geometry


class CoordinateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output: dict[str, Any] = {
            "name": "DP-1",
            "geometry": {"x": -1280, "y": 200, "width": 1280, "height": 720},
            "scale": 1.5,
        }

    def test_fractional_scale_and_negative_origin(self) -> None:
        result = map_point(959.5, 539.5, 1920, 1080, self.output, [self.output])
        self.assertEqual(result["x"], -640)
        self.assertEqual(result["y"], 560)
        self.assertFalse(result["input_dispatched"])

    def test_other_outputs_do_not_affect_named_capture(self) -> None:
        other = copy.deepcopy(self.output)
        other["name"] = "HDMI-1"
        other["scale"] = 2
        result = map_point(0, 0, 1920, 1080, self.output, [other, self.output])
        self.assertAlmostEqual(result["x"], -1280 + 1 / 3)

    def test_changed_geometry_scale_or_disconnect_rejected(self) -> None:
        for field in ("x", "width", "scale", "name"):
            changed = copy.deepcopy(self.output)
            if field in ("x", "width"):
                changed["geometry"][field] += 1
            elif field == "scale":
                changed[field] = 2
            else:
                changed[field] = "different"
            with self.subTest(field=field), self.assertRaises(ValueError):
                map_point(0, 0, 1920, 1080, self.output, [changed])

    def test_outside_and_nonfinite_points_rejected(self) -> None:
        for point in (-1, 1919.9, 1920, float("inf"), float("nan"), True):
            with self.subTest(point=point), self.assertRaises(ValueError):
                map_point(point, 0, 1920, 1080, self.output, [self.output])

    def test_ambiguous_or_missing_output_rejected(self) -> None:
        for outputs in ([], [self.output, self.output]):
            with self.assertRaises(ValueError):
                output_geometry(outputs, "DP-1")

    def test_invalid_geometry_rejected(self) -> None:
        for value in (None, float("nan"), True, "1280"):
            changed = copy.deepcopy(self.output)
            changed["geometry"]["width"] = value
            with self.assertRaises(ValueError):
                output_geometry([changed], "DP-1")
