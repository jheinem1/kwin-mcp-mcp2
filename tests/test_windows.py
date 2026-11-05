from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from kwin_mcp import windows

_ID = "01a8db17-e9d8-4d1f-8ba9-741e22b2ff14"


class WindowOperationTests(unittest.TestCase):
    def test_validates_before_starting_worker(self) -> None:
        for op, wid, timeout in [
            ("close", _ID, 1),
            ("activate", "title", 1),
            ("list", "", 0),
            ("list", "", float("nan")),
            ("list", "", True),
        ]:
            with (
                self.subTest(op=op, wid=wid, timeout=timeout),
                mock.patch("kwin_mcp.windows.subprocess.run") as run,
                self.assertRaises(ValueError),
            ):
                windows.run_window_operation(op, window_id=wid, env={}, timeout=timeout)
            run.assert_not_called()

    def test_worker_has_environment_and_bounded_deadline(self) -> None:
        result = {"windows": [], "outputs": []}
        with mock.patch("kwin_mcp.windows.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True, "result": result}), ""
            )
            self.assertEqual(windows.run_window_operation("list", env={"TEST": "yes"}), result)
        self.assertEqual(run.call_args.kwargs["env"], {"TEST": "yes"})
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_activation_timeout_does_not_retry(self) -> None:
        with mock.patch("kwin_mcp.windows.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired("worker", 7)
            with self.assertRaisesRegex(RuntimeError, "outcome.*unknown"):
                windows.run_window_operation("activate", window_id=_ID, env={})
        self.assertEqual(run.call_count, 1)

    def test_malformed_and_oversize_responses_rejected(self) -> None:
        for raw in [
            "not json",
            "[]",
            '{"ok":1}',
            '{"ok":true,"result":[]}',
            '"' + "a" * windows._MAX_RESPONSE + '"',
        ]:
            with (
                self.subTest(raw=raw[:20]),
                mock.patch(
                    "kwin_mcp.windows.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, raw, ""),
                ),
                self.assertRaises(RuntimeError),
            ):
                windows.run_window_operation("list", env={})

    def test_worker_errors_propagate(self) -> None:
        with (
            mock.patch(
                "kwin_mcp.windows.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({"ok": False, "error": "Exact window ID not found"}), ""
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "Exact window ID not found"),
        ):
            windows.run_window_operation("geometry", window_id=_ID, env={})

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise KWin script logic")
    def test_script_exact_identity_and_geometry(self) -> None:
        setup = """
const first = {internalId:"{01a8db17-e9d8-4d1f-8ba9-741e22b2ff14}",
caption:"Same title",resourceClass:"browser",active:false,minimized:false,
frameGeometry:{x:-800,y:10,width:600,height:400},output:{name:"HDMI-1"}};
const second = {...first, internalId:"{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}"};
const workspace = {windowList:()=>[first,second], activeWindow:second,
screens:[{name:"HDMI-1", geometry:{x:-1024,y:0,width:1024,height:768},devicePixelRatio:1.5}]};
function callDBus(dest,path,iface,method,value) {console.log(value);}
"""
        for op in ["list", "activate", "geometry"]:
            with self.subTest(op=op):
                proc = subprocess.run(
                    ["node", "-e", setup + windows._script(op, _ID, ":1.23")],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                response = json.loads(proc.stdout)
                self.assertTrue(response["ok"])
                data = response["result"]
                self.assertEqual(data["outputs"][0]["scale"], 1.5)
                if op == "list":
                    self.assertEqual(len(data["windows"]), 2)
                else:
                    self.assertEqual(data["window"]["window_id"], _ID)
                    self.assertEqual(data["window"]["geometry"]["x"], -800)
                if op == "activate":
                    self.assertTrue(data["verified_active"])
        proc = subprocess.run(
            [
                "node",
                "-e",
                setup
                + windows._script("activate", "11111111-2222-3333-4444-555555555555", ":1.23"),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertFalse(json.loads(proc.stdout)["ok"])

    def test_worker_callback_authentication_and_cleanup(self) -> None:
        for callback, cleanup_error in [(True, False), (False, False), (True, True)]:
            with self.subTest(callback=callback, cleanup_error=cleanup_error):
                exported = []

                class ServiceObject:
                    def __init__(self, bus, path, exported=exported):
                        exported.append(self)

                    def remove_from_connection(self):
                        pass

                bus = mock.Mock()
                bus.get_name_owner.return_value = ":1.10"
                bus.get_unique_name.return_value = ":1.20"
                scripting = mock.Mock()
                scripting.loadScript.return_value = 7
                if cleanup_error:
                    scripting.unloadScript.side_effect = RuntimeError("cleanup failed")
                script = mock.Mock()

                def run(exported=exported, callback=callback, **kwargs):
                    # Other session peers cannot forge the callback response.
                    exported[0].Complete('{"ok":true,"result":{"spoof":true}}', ":1.99")
                    if callback:
                        exported[0].Complete('{"ok":true,"result":{"windows":[]}}', ":1.10")

                script.run.side_effect = run
                dbus = types.ModuleType("dbus")
                service = types.ModuleType("dbus.service")
                service.__dict__["Object"] = ServiceObject
                service.__dict__["method"] = lambda *a, **kw: lambda fn: fn
                dbus.__dict__["service"] = service
                dbus.__dict__["SessionBus"] = mock.Mock(return_value=bus)
                dbus.__dict__["Interface"] = mock.Mock(side_effect=[scripting, script])
                glib_loop = types.ModuleType("dbus.mainloop.glib")
                glib_loop.__dict__["DBusGMainLoop"] = mock.Mock()
                repository = types.ModuleType("gi.repository")
                repository.__dict__["GLib"] = mock.Mock()
                modules = {
                    "dbus": dbus,
                    "dbus.service": service,
                    "dbus.mainloop.glib": glib_loop,
                    "gi.repository": repository,
                }
                with (
                    tempfile.TemporaryDirectory() as directory,
                    mock.patch.dict(sys.modules, modules),
                ):
                    request = {
                        "op": "activate",
                        "window_id": _ID,
                        "timeout": 1,
                        "script_path": str(Path(directory) / "query.js"),
                    }
                    if callback:
                        result = windows._execute(request)
                        self.assertEqual(result["result"]["windows"], [])
                        self.assertNotIn("spoof", result["result"])
                        self.assertEqual("cleanup_warning" in result["result"], cleanup_error)
                    else:
                        with self.assertRaisesRegex(TimeoutError, "outcome unknown"):
                            windows._execute(request)
                scripting.unloadScript.assert_called_once()
                self.assertEqual(scripting.unloadScript.call_args.kwargs["timeout"], 1.0)
                bus.close.assert_called_once()

    def test_script_parameters_are_json_escaped(self) -> None:
        text = windows._script("list", '"; evil(); //', ":1.23")
        first_line = text.splitlines()[0]
        params = json.loads(first_line.removeprefix("const request = ").removesuffix(";"))
        self.assertEqual(params["id"], '"; evil(); //')


if __name__ == "__main__":
    unittest.main()
