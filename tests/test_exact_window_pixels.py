import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import dbus
from mcp.types import ImageContent, TextContent
from PIL import Image

from kwin_mcp.core import AutomationEngine
from kwin_mcp.screenshot import _capture_raw, capture_window_dbus
from kwin_mcp.session import SessionType

WINDOW = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OWNER = ":1.27"
RESULT = {
    "window_id": WINDOW,
    "compositor_owner": OWNER,
    "frame": {"x": 10, "y": 12, "width": 100, "height": 60},
}


class TestWindowScreenshot(unittest.TestCase):
    def setUp(self):
        self.engine = AutomationEngine()
        self.engine._session = MagicMock()
        self.engine._session.info.session_type = SessionType.LIVE
        self.engine._session.info.dbus_address = "private-address"
        patch.object(
            dbus, "DBusException", type("DBusTestError", (Exception,), {}), create=True
        ).start()
        self.env = patch.object(self.engine, "_session_env", return_value={}).start()
        self.addCleanup(patch.stopall)
        self.snapshot = patch("kwin_mcp.windows.run_window_operation", return_value=RESULT).start()
        self.capture = patch(
            "kwin_mcp.core.capture_window_dbus",
            return_value=({"pixel_width": 100, "pixel_height": 60}, b"PNG"),
        ).start()

    def test_exact_capture_without_issuing_an_input_receipt(self):
        state, png = self.engine.screenshot_window("{" + WINDOW.upper() + "}")
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["window_id"], WINDOW)
        self.assertEqual(state["coordinate_space"], "window_image_pixels")
        self.assertIs(state["transform_available"], False)
        self.assertEqual(png, b"PNG")
        self.assertEqual(self.engine._captures, {})
        self.capture.assert_called_once_with("private-address", WINDOW, owner=OWNER)
        self.assertEqual(self.snapshot.call_count, 2)
        self.snapshot.assert_called_with(
            "capture_target", window_id=WINDOW, env={}, timeout=3.0, require_unlocked=True
        )

    def test_title_or_numeric_input_does_not_enter_capture(self):
        for invalid in ("Fixture A", "123"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.engine.screenshot_window(invalid)
        self.snapshot.assert_not_called()
        self.capture.assert_not_called()

    def test_locked_or_unverified_before_capture_never_fetches_pixels(self):
        self.snapshot.side_effect = RuntimeError("Locked or cannot verify lock")
        state, png = self.engine.screenshot_window(WINDOW)
        self.assertEqual(state["status"], "unsupported")
        self.assertIsNone(png)
        self.capture.assert_not_called()

    def test_discards_pixels_on_post_capture_change(self):
        for after in (
            {**RESULT, "window_id": OTHER},
            {**RESULT, "compositor_owner": ":1.31"},
            {**RESULT, "frame": {}},
            {**RESULT, "cleanup_warning": "cannot unload"},
            RuntimeError("locked, minimized or closed"),
        ):
            with self.subTest(after=after):
                self.snapshot.side_effect = [RESULT, after]
                state, png = self.engine.screenshot_window(WINDOW)
                self.assertEqual(state["status"], "unsupported")
                self.assertIsNone(png)

    def test_no_fallback_if_dbus_denied(self):
        self.capture.side_effect = dbus.DBusException("Capture denied")
        self.assertEqual(self.engine.screenshot_window(WINDOW)[0]["status"], "unsupported")
        self.capture.assert_called_once()
        self.snapshot.assert_called_once()


class TestWindowTransport(unittest.TestCase):
    def setUp(self):
        self.bus = (
            patch("kwin_mcp.screenshot.dbus.bus.BusConnection", create=True).start().return_value
        )
        self.bus.call_blocking.return_value = OWNER
        self.guard = patch("kwin_mcp.screenshot.require_active_compositing").start()
        self.raw = patch("kwin_mcp.screenshot._capture_raw").start()
        self.interface = (
            patch("kwin_mcp.screenshot.dbus.Interface", create=True).start().return_value
        )
        self.addCleanup(patch.stopall)
        patch.object(dbus, "Boolean", bool, create=True).start()
        self.dict_type = patch.object(dbus, "Dictionary", create=True).start()
        patch.object(
            dbus,
            "types",
            SimpleNamespace(
                UnixFd=lambda fd: SimpleNamespace(take=lambda: os.dup(fd)),
            ),
            create=True,
        ).start()
        self.metadata = {
            "format": 16,
            "width": 1,
            "height": 1,
            "stride": 4,
            "windowId": "{" + WINDOW + "}",
        }

    def test_pins_owner_and_checks_returned_uuid(self):
        self.raw.return_value = (b"\xfa\x10\x00\xff", self.metadata)
        size, png = capture_window_dbus("bus", WINDOW, owner=OWNER)
        self.assertEqual(size, {"pixel_width": 1, "pixel_height": 1})
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.getpixel((0, 0)), (250, 16, 0))
        self.guard.assert_called_once_with("bus", owner=OWNER)
        self.bus.get_object.assert_called_once_with(
            OWNER, "/org/kde/KWin/ScreenShot2", introspect=False
        )
        self.assertEqual(self.dict_type.call_args.kwargs["signature"], "sv")
        self.raw.assert_called_once_with(
            self.interface, ANY, window_id="{" + WINDOW + "}", byte_limit=64 * 1024**2, timeout=4.0
        )
        self.bus.close.assert_called_once()

    def test_refuses_wrong_or_missing_metadata_and_transparent_pixels(self):
        for window_id, fmt in ((OTHER, 16), ("", 16), (WINDOW, 17)):
            with self.subTest(window_id=window_id, fmt=fmt):
                result = {**self.metadata, "windowId": window_id, "format": fmt}
                self.raw.return_value = (b"\0" * 4, result)
                with self.assertRaises((ValueError, RuntimeError)):
                    capture_window_dbus("bus", WINDOW, owner=OWNER)

    def test_never_retargets_changed_compositor(self):
        self.bus.call_blocking.return_value = ":1.12"
        with self.assertRaisesRegex(RuntimeError, "owner changed"):
            capture_window_dbus("bus", WINDOW, owner=OWNER)
        self.raw.assert_not_called()

    def test_window_pipe_only_calls_exact_capture_method(self):
        # Fake ScreenShot2: duplicate fd as the D-Bus receiver would and send one pixel.
        iface = MagicMock()

        def send(uuid, _options, fd, **_kwargs):
            out = fd.take()
            try:
                os.write(out, b"\0" * 4)
            finally:
                os.close(out)
            self.assertEqual(uuid, WINDOW)
            return self.metadata

        iface.CaptureWindow.side_effect = send
        self.assertEqual(_capture_raw(iface, {}, window_id=WINDOW)[0], b"\0" * 4)
        iface.CaptureActiveScreen.assert_not_called()
        iface.CaptureScreen.assert_not_called()

    def test_tool_emits_real_mcp_image_only_when_present(self):
        from kwin_mcp import server

        with patch.object(server._engine, "screenshot_window") as engine:
            engine.return_value = ({"status": "ok"}, b"image")
            result = server.screenshot_window(WINDOW)
            text, image = result
            assert isinstance(text, TextContent) and isinstance(image, ImageContent)
            self.assertEqual(json.loads(text.text)["status"], "ok")
            self.assertEqual(image.data, "aW1hZ2U=")
            engine.return_value = ({"status": "unsupported"}, None)
            self.assertEqual(len(server.screenshot_window(WINDOW)), 1)
