from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

dbus = types.ModuleType("dbus")
dbus_bus = types.ModuleType("dbus.bus")
dbus.__dict__["bus"] = dbus_bus
sys.modules.setdefault("dbus", dbus)
sys.modules.setdefault("dbus.bus", dbus_bus)

from kwin_mcp import screenshot  # noqa: E402


class ScreenshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unix_fd = mock.patch.object(
            screenshot.dbus, "types", types.SimpleNamespace(UnixFd=lambda fd: fd), create=True
        )
        self.unix_fd.start()
        self.addCleanup(self.unix_fd.stop)

    def test_large_frame_drains_before_dbus_reply(self) -> None:
        data = bytes(1024 * 1024)

        def capture(options: dict, fd: int, *, timeout: float) -> dict:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            return {"width": 512, "height": 512, "stride": 2048}

        iface = types.SimpleNamespace(CaptureActiveScreen=capture)
        actual, _ = screenshot._capture_raw(iface, {}, timeout=2)
        self.assertEqual(actual, data)

    def test_dbus_failure_closes_both_descriptors(self) -> None:
        pipe = os.pipe()
        iface = mock.Mock()
        iface.CaptureActiveScreen.side_effect = RuntimeError("denied")
        with (
            mock.patch.object(screenshot.os, "pipe", return_value=pipe),
            self.assertRaisesRegex(RuntimeError, "denied"),
        ):
            screenshot._capture_raw(iface, {}, timeout=0.1)
        for fd in pipe:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_writer_that_stays_open_has_deadline(self) -> None:
        held: list[int] = []

        def capture(options: dict, fd: int, *, timeout: float) -> dict:
            held.append(os.dup(fd))
            return {"width": 1, "height": 1, "stride": 4}

        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                screenshot._capture_raw(
                    types.SimpleNamespace(CaptureActiveScreen=capture), {}, timeout=0.05
                )
        finally:
            for fd in held:
                os.close(fd)
        self.assertLess(time.monotonic() - started, 1)

    def test_incomplete_data_is_rejected(self) -> None:
        iface = mock.Mock()
        iface.CaptureActiveScreen.return_value = {"width": 1, "height": 1, "stride": 4}
        with self.assertRaisesRegex(RuntimeError, "incomplete pixel data"):
            screenshot._capture_raw(iface, {})

    def test_metadata_does_not_guess_coordinate_transform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.png"
            Image.new("RGB", (800, 600)).save(path)
            metadata = screenshot.screenshot_metadata(path)
        self.assertEqual(metadata["pixel_width"], 800)
        self.assertEqual(metadata["pixel_height"], 600)
        self.assertFalse(metadata["transform_available"])
        self.assertIsNone(metadata["scale"])
        self.assertIsNone(metadata["origin"])
