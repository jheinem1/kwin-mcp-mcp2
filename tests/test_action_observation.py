from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Native dependencies are stubbed by the same fixture used for input unit tests.
import test_input  # noqa: F401

from kwin_mcp.core import AutomationEngine


class ActionObservationTests(unittest.TestCase):
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

    def test_capture_error_preserves_dispatched_action(self) -> None:
        with mock.patch("kwin_mcp.core.capture_frame_burst", side_effect=RuntimeError("denied")):
            result = self.engine.mouse_click(10, 20, screenshot_after_ms=[0])
        self.input.mouse_click.assert_called_once()
        self.assertIn("Action status: dispatched", result)
        self.assertIn("Observation status: failed", result)
        self.assertIn("Do not repeat", result)

    def test_input_error_does_not_claim_dispatch_or_capture(self) -> None:
        self.input.mouse_click.side_effect = RuntimeError("input failed")
        with (
            mock.patch("kwin_mcp.core.capture_frame_burst") as capture,
            self.assertRaisesRegex(RuntimeError, "input failed"),
        ):
            self.engine.mouse_click(10, 20, screenshot_after_ms=[0])
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
