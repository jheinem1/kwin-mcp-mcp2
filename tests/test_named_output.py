from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Native dependencies are stubbed by the same fixture used for input unit tests.
import test_input  # noqa: F401

from kwin_mcp.core import AutomationEngine


class NamedOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = AutomationEngine()
        self.engine._session = mock.Mock(
            is_running=True,
            info=SimpleNamespace(
                dbus_address="private", wayland_socket="private", screenshot_dir=Path("/tmp")
            ),
        )
        self.input = mock.Mock()
        self.engine._input = self.input

    def test_named_capture_changed_layout_has_no_mapping_receipt(self) -> None:
        output = {
            "name": "DP-1",
            "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
            "scale": 1,
        }
        changed = {**output, "scale": 2}
        with (
            mock.patch.object(self.engine, "_session_env", return_value={}),
            mock.patch(
                "kwin_mcp.windows.run_window_operation",
                side_effect=[{"outputs": [output]}, {"outputs": [changed]}],
            ),
            mock.patch("kwin_mcp.core.capture_output_dbus") as capture,
            mock.patch(
                "kwin_mcp.core.screenshot_metadata",
                return_value={"pixel_width": 800, "pixel_height": 600},
            ),
        ):
            result = self.engine.screenshot_output("DP-1")
        metadata = json.loads(result.split("\n", 1)[1])
        self.assertFalse(metadata["transform_available"])
        self.assertEqual(self.engine._captures, {})
        capture.assert_called_once()

    def test_mapping_rejects_expired_capture_without_query(self) -> None:
        self.engine._captures["old"] = {
            "session": self.engine._session,
            "created": -100,
        }
        with (
            mock.patch("kwin_mcp.windows.run_window_operation") as run,
            self.assertRaisesRegex(ValueError, "expired"),
        ):
            self.engine.map_screenshot_point("old", 0, 0)
        run.assert_not_called()

    def test_mapping_rejects_previous_session(self) -> None:
        self.engine._captures["old"] = {
            "session": object(),
            "created": 1e20,
        }
        with self.assertRaisesRegex(ValueError, "previous-session"):
            self.engine.map_screenshot_point("old", 0, 0)

    def test_mapping_uses_current_layout_without_input(self) -> None:
        output = {
            "name": "DP-1",
            "geometry": {"x": 100, "y": 0, "width": 800, "height": 600},
            "scale": 2,
        }
        self.engine._captures["valid"] = {
            "session": self.engine._session,
            "created": 1e20,
            "width": 1600,
            "height": 1200,
            "output": output,
        }
        with (
            mock.patch.object(self.engine, "_session_env", return_value={}),
            mock.patch("kwin_mcp.windows.run_window_operation", return_value={"outputs": [output]}),
        ):
            result = json.loads(self.engine.map_screenshot_point("valid", 799.5, 599.5))
        self.assertEqual(result["x"], 500)
        self.assertEqual(result["y"], 300)
        self.assertFalse(result["input_dispatched"])
        self.input.assert_not_called()
