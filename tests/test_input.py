from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

# Keep the unit test independent of native dbus-python packages.
dbus = types.ModuleType("dbus")
dbus_bus = types.ModuleType("dbus.bus")
dbus_mainloop = types.ModuleType("dbus.mainloop")
dbus_glib = types.ModuleType("dbus.mainloop.glib")
dbus_glib.DBusGMainLoop = mock.Mock()
dbus.bus = dbus_bus
sys.modules.setdefault("dbus", dbus)
sys.modules.setdefault("dbus.bus", dbus_bus)
sys.modules.setdefault("dbus.mainloop", dbus_mainloop)
sys.modules.setdefault("dbus.mainloop.glib", dbus_glib)

from kwin_mcp import input as input_module  # noqa: E402
from kwin_mcp.input import EISClient, InputBackend  # noqa: E402


class KeyboardValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InputBackend.__new__(InputBackend)
        self.backend._client = mock.Mock()

    def test_unsupported_text_fails_before_typing(self) -> None:
        with self.assertRaisesRegex(ValueError, "index 1"):
            self.backend.keyboard_type("aé")
        self.backend._client.keyboard_key.assert_not_called()

    def test_unknown_key_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported key combination"):
            self.backend.keyboard_key("definitely-not-a-key")

    def test_unicode_backend_receives_target_session_environment(self) -> None:
        env = {
            "WAYLAND_DISPLAY": "wayland-mcp-test",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/private-bus",
        }
        completed = mock.Mock(returncode=0)
        with (
            mock.patch("kwin_mcp.input.shutil.which", return_value="/usr/bin/wtype"),
            mock.patch("kwin_mcp.input.subprocess.run", return_value=completed) as run,
        ):
            self.assertTrue(self.backend.keyboard_type_unicode("é", env))
        self.assertIs(run.call_args.kwargs["env"], env)


class CleanupTests(unittest.TestCase):
    def test_shared_pointer_keyboard_device_is_unrefed_once(self) -> None:
        client = EISClient.__new__(EISClient)
        client._active_touches = {}
        client._touch_device = 0
        client._pointer = 42
        client._keyboard = 42
        client._eis_iface = None
        client._cookie = 0
        client._ei = 0

        libei = mock.Mock()
        with mock.patch.object(input_module, "_libei", libei):
            client.close()

        libei.ei_device_unref.assert_called_once_with(42)


class LazyLoadingTests(unittest.TestCase):
    def test_libei_is_not_loaded_at_import_time(self) -> None:
        lazy = input_module._LazyLibEI()
        with mock.patch("kwin_mcp.input._load_libei") as load:
            self.assertIsNone(lazy._library)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
