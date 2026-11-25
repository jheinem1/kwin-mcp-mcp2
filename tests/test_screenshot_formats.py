from __future__ import annotations

import unittest
from unittest import mock

# Native D-Bus is stubbed by the shared fixture; Pillow decodes actual byte swatches.
import test_input  # noqa: F401

from kwin_mcp import screenshot


class ScreenshotFormatTests(unittest.TestCase):
    def test_decode_honors_qimage_formats_and_alpha(self) -> None:
        for byte_order, format_code, raw, expected in (
            ("little", 4, (25, 40, 230, 255), (230, 40, 25, 255)),
            ("big", 5, (255, 230, 40, 25), (230, 40, 25, 255)),
            ("little", 6, (0, 0, 114, 128), (227, 0, 0, 128)),
            ("big", 6, (128, 114, 0, 0), (227, 0, 0, 128)),
            ("little", 16, (230, 40, 25, 255), (230, 40, 25, 255)),
            ("big", 17, (230, 40, 25, 128), (230, 40, 25, 128)),
            ("little", 18, (114, 0, 0, 128), (227, 0, 0, 128)),
        ):
            with (
                self.subTest(byte_order=byte_order, format_code=format_code),
                mock.patch.object(screenshot.sys, "byteorder", byte_order),
            ):
                # Include row padding to exercise the stride used by ScreenShot2.
                image = screenshot._decode_capture(
                    bytes((*raw, 0, 0, 0, 0)),
                    {"format": format_code, "width": 1, "height": 1, "stride": 8},
                )
                self.assertEqual(image.convert("RGBA").getpixel((0, 0)), expected)

    def test_decode_rejects_unknown_format_and_truncated_bytes(self) -> None:
        meta = {"format": 16, "width": 1, "height": 2, "stride": 4}
        with self.assertRaisesRegex(RuntimeError, "inconsistent raw image"):
            screenshot._decode_capture(bytes(4), meta)
        with self.assertRaisesRegex(RuntimeError, "unsupported QImage format 31"):
            screenshot._decode_capture(bytes(8), {**meta, "format": 31})
        del meta["format"]
        with self.assertRaisesRegex(RuntimeError, "invalid image metadata"):
            screenshot._decode_capture(bytes(8), meta)
