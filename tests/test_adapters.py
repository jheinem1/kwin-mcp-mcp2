from __future__ import annotations

import sys
import types
import unittest
from typing import Any, cast
from unittest import mock

# Match the native dependency stubs used by the other unit tests.
dbus = types.ModuleType("dbus")
dbus_bus = types.ModuleType("dbus.bus")
dbus.__dict__["bus"] = dbus_bus
sys.modules.setdefault("dbus", dbus)
sys.modules.setdefault("dbus.bus", dbus_bus)

from kwin_mcp import adapters  # noqa: E402


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = mock.Mock()
        self.iface = mock.Mock()
        patches = [
            mock.patch.object(
                adapters.dbus.bus, "BusConnection", return_value=self.bus, create=True
            ),
            mock.patch.object(adapters.dbus, "Interface", return_value=self.iface, create=True),
            mock.patch.object(
                adapters.dbus, "Array", side_effect=lambda values, signature: values, create=True
            ),
        ]
        self.connection, self.interface, self.array = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def test_uri_escapes_without_expansion_or_resolution(self) -> None:
        self.assertEqual(
            adapters.local_path_uri('/tmp/a b#%?";$(id).txt'),
            "file:///tmp/a%20b%23%25%3F%22%3B%24%28id%29.txt",
        )
        self.assertEqual(adapters.local_path_uri("/tmp/é"), "file:///tmp/%C3%A9")
        self.assertEqual(adapters.local_path_uri("/"), "file:///")

    def test_invalid_paths(self) -> None:
        for value in (
            "",
            "relative",
            "~/file",
            "file:///tmp/a",
            "smb://host/a",
            "//host/a",
            "/tmp/../x",
            "/tmp/./x",
            "/tmp/a\n",
            "/tmp/\0",
            "/tmp/\x7f",
            "/tmp/\ud800",
            "/" + "a" * 4096,
            None,
            4,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapters.local_path_uri(cast("Any", value))

    def test_show_items_reports_dispatch_only(self) -> None:
        result = adapters.show_local_paths("unix:path=/bus", ["/tmp/selected file"])
        self.connection.assert_called_once_with("unix:path=/bus")
        self.bus.get_object.assert_called_once_with(
            "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1", introspect=False
        )
        self.iface.ShowItems.assert_called_once_with(
            ["file:///tmp/selected%20file"], "", timeout=5.0
        )
        self.array.assert_called_once_with(["file:///tmp/selected%20file"], signature="s")
        self.assertEqual(result["dispatch"], "accepted")
        self.assertEqual(result["observation"], "not_verified")
        self.assertFalse(result["ui_verified"])
        self.bus.close.assert_called_once_with()

    def test_show_folders(self) -> None:
        result = adapters.show_local_paths("unix:path=/bus", ["/tmp"], action="show_folders")
        self.iface.ShowFolders.assert_called_once_with(["file:///tmp"], "", timeout=5.0)
        self.iface.ShowItems.assert_not_called()
        self.assertEqual(result["dispatch"], "accepted")

    def test_invalid_request_never_connects(self) -> None:
        for paths in ([], ["/tmp"] * 33, "/tmp", ["/tmp", "relative"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                adapters.show_local_paths("unix:path=/bus", cast("Any", paths))
        with self.assertRaises(ValueError):
            adapters.show_local_paths("unix:path=/bus", ["/tmp"], action="Delete")
        with self.assertRaises(ValueError):
            adapters.show_local_paths("", ["/tmp"])
        self.connection.assert_not_called()

    def test_dispatch_timeout_is_unknown_and_not_retried(self) -> None:
        self.iface.ShowItems.side_effect = TimeoutError("reply timed out")
        result = adapters.show_local_paths("unix:path=/bus", ["/tmp"])
        self.assertEqual(result["dispatch"], "unknown")
        self.assertFalse(result["ui_verified"])
        self.iface.ShowItems.assert_called_once()
        self.bus.close.assert_called_once()

    def test_connection_failure_is_not_attempted(self) -> None:
        self.connection.side_effect = RuntimeError("no bus")
        result = adapters.show_local_paths("unix:path=/bus", ["/tmp"])
        self.assertEqual(result["dispatch"], "not_attempted")
        self.iface.ShowItems.assert_not_called()

    def test_cleanup_failure_does_not_erase_dispatch(self) -> None:
        self.bus.close.side_effect = RuntimeError("close failed")
        result = adapters.show_local_paths("unix:path=/bus", ["/tmp"])
        self.assertEqual(result["dispatch"], "accepted")


if __name__ == "__main__":
    unittest.main()
