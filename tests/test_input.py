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
        client._owned_devices = {42}
        client._ready_devices = {42}
        client._relative_pointer = 42
        client._pointer = 42
        client._keyboard = 42
        client._eis_iface = None
        client._cookie = 0
        client._ei = 0

        libei = mock.Mock()
        with mock.patch.object(input_module, "_libei", libei):
            client.close()

        libei.ei_device_unref.assert_called_once_with(42)


class RelativePointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = EISClient.__new__(EISClient)
        self.client._pointer = self.client._relative_pointer = 0
        self.client._keyboard = self.client._touch_device = 0
        self.client._owned_devices = set()
        self.client._ready_devices = set()
        self.client._disconnected = False
        self.client._ei = 1
        self.libei = mock.Mock()
        self.patch = mock.patch.object(input_module, "_libei", self.libei)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.libei.ei_event_get_device.return_value = 42
        self.libei.ei_dispatch.return_value = 0
        self.libei.ei_get_event.return_value = 0

    def register(self, capabilities: set[int]) -> None:
        self.libei.ei_device_has_capability.side_effect = lambda d, c: c in capabilities
        self.client._register_device(7)

    def test_relative_only_device_is_retained_and_waits_for_resume(self) -> None:
        self.register({input_module._EI_CAP_POINTER})
        self.assertEqual(self.client._relative_pointer, 42)
        self.assertEqual(self.client._pointer, 0)
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self.client.pointer_move_relative(10, -5)
        self.libei.ei_device_pointer_motion.assert_not_called()
        self.client._handle_event(7, input_module._EI_EVENT_DEVICE_RESUMED)
        self.client.pointer_move_relative(10, -5)
        self.libei.ei_device_pointer_motion.assert_called_once_with(42, 10, -5)
        self.libei.ei_device_pointer_motion_absolute.assert_not_called()
        self.libei.ei_device_frame.assert_called_once()

    def test_multicapability_device_gets_one_reference(self) -> None:
        self.register(
            {
                input_module._EI_CAP_POINTER,
                input_module._EI_CAP_POINTER_ABSOLUTE,
                input_module._EI_CAP_KEYBOARD,
            }
        )
        self.libei.ei_device_ref.assert_called_once_with(42)
        self.assertEqual(self.client._relative_pointer, self.client._pointer)

    def test_pause_remove_and_disconnect_fail_without_motion(self) -> None:
        self.register({input_module._EI_CAP_POINTER})
        self.client._handle_event(7, input_module._EI_EVENT_DEVICE_RESUMED)
        self.client._handle_event(7, input_module._EI_EVENT_DEVICE_PAUSED)
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self.client.pointer_move_relative(1, 2)
        self.client._handle_event(7, input_module._EI_EVENT_DEVICE_REMOVED)
        self.assertEqual(self.client._relative_pointer, 0)
        self.assertFalse(self.client._owned_devices)
        self.libei.ei_device_unref.assert_called_once_with(42)
        self.client._handle_event(7, input_module._EI_EVENT_DISCONNECT)
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.client.pointer_move_relative(1, 2)
        self.libei.ei_device_pointer_motion.assert_not_called()


class GameMouseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InputBackend.__new__(InputBackend)
        self.backend._client = mock.Mock()

    def test_interpolation_preserves_total_delta(self) -> None:
        with mock.patch.object(input_module.time, "sleep"):
            self.backend.mouse_move_relative(11.25, -7.75, 35)
        calls = self.backend._client.pointer_move_relative.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(sum(c.args[0] for c in calls), 11.25)
        self.assertAlmostEqual(sum(c.args[1] for c in calls), -7.75)
        self.backend._client.pointer_move_absolute.assert_not_called()

    def test_invalid_motion_never_injects(self) -> None:
        for dx, dy, duration in [
            (float("nan"), 0, 0),
            (0, float("inf"), 0),
            (1, 1, -1),
            (1, 1, 5001),
            (1, 1, 0.5),
        ]:
            with self.assertRaises(ValueError):
                self.backend.mouse_move_relative(dx, dy, duration)
        self.backend._client.pointer_move_relative.assert_not_called()

    def test_buttons_without_coordinates_do_not_warp(self) -> None:
        self.backend.mouse_button_down()
        self.backend.mouse_button_up()
        self.backend._client.pointer_move_absolute.assert_not_called()
        self.assertEqual(
            self.backend._client.pointer_button.call_args_list,
            [mock.call(0x110, 1), mock.call(0x110, 0)],
        )

    def test_partial_coordinates_fail_before_input(self) -> None:
        for operation in [self.backend.mouse_button_down, self.backend.mouse_button_up]:
            with self.assertRaises(ValueError):
                operation(x=5)
        self.backend._client.pointer_button.assert_not_called()
        self.backend._client.pointer_move_absolute.assert_not_called()


class LazyLoadingTests(unittest.TestCase):
    def test_libei_is_not_loaded_at_import_time(self) -> None:
        lazy = input_module._LazyLibEI()
        with mock.patch("kwin_mcp.input._load_libei") as load:
            self.assertIsNone(lazy._library)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
