from __future__ import annotations

import json
import subprocess
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

    def test_focus_does_not_retry_unknown_outcome(self) -> None:
        with (
            mock.patch.object(self.engine, "_session_env", return_value={}),
            mock.patch(
                "kwin_mcp.core.subprocess.run", side_effect=subprocess.TimeoutExpired("atspi", 1)
            ) as run,
            self.assertRaisesRegex(RuntimeError, "outcome unknown"),
        ):
            self.engine.focus_window("kate", "exact title")
        run.assert_called_once()
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["window_title"], "exact title")
        self.assertLessEqual(run.call_args.kwargs["timeout"], 6)

    def test_semantic_error_response_is_not_key_error(self) -> None:
        with mock.patch.object(
            self.engine, "_run_atspi", return_value={"ok": False, "error": "state missing"}
        ) as run:
            result = self.engine.invoke_ui_action("Save", required_states=["enabled"])
        self.assertIn("state missing", result)
        self.assertFalse(run.call_args.kwargs["retry"])
        self.assertEqual(run.call_args.kwargs["required_states"], ["enabled"])

    def test_malformed_worker_response_is_bounded_diagnostic(self) -> None:
        for payload in ["null", "[]", '{"ok": true}', '{"ok": "yes", "result": "x"}']:
            with (
                self.subTest(payload=payload),
                mock.patch.object(self.engine, "_session_env", return_value={}),
                mock.patch(
                    "kwin_mcp.core.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=payload, stderr=""),
                ) as run,
                self.assertRaisesRegex(RuntimeError, "invalid response shape"),
            ):
                self.engine.focus_window("kate")
            run.assert_called_once()

    def test_invalid_deadline_is_rejected_before_worker(self) -> None:
        with (
            mock.patch.object(self.engine, "_session_env", return_value={}),
            mock.patch("kwin_mcp.core.subprocess.run") as run,
            self.assertRaises(ValueError),
        ):
            self.engine._run_atspi("wait", timeout_ms=float("nan"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
