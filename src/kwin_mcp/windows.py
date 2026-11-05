"""Exact compositor window IDs via transient KWin 6 scripts.

API references (Plasma/6.3): KDE/kwin src/scripting/{scripting.cpp,
workspace_wrapper.h,org.kde.kwin.Script.xml}, src/window.h and src/core/output.h.
Scripts use windowList/internalId, activeWindow, frameGeometry, and screens.
Output scale is exposed as devicePixelRatio, not a JavaScript `scale` property.
No lock-screen or input-security settings are changed.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

_MAX_RESPONSE = 1024 * 1024
_INTERFACE = "org.kwin_mcp.Result"
_PATH = "/org/kwin_mcp/Result"


def _validate(op: str, window_id: str, timeout: float) -> None:
    if op not in {"list", "activate", "geometry"}:
        raise ValueError("Unknown KWin window operation")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout must be finite and between 0 and 30 seconds")
    if op != "list":
        try:
            uuid.UUID(window_id.strip("{}"))
        except (ValueError, AttributeError) as exc:
            raise ValueError("window_id must be an exact KWin internal UUID") from exc


def _script(op: str, window_id: str, destination: str) -> str:
    # Serialize parameters, never interpolate application text into JavaScript.
    params = json.dumps({"op": op, "id": window_id, "destination": destination})
    return (
        "const request = "
        + params
        + ";\n"
        + r"""
(function () {
    function rect(r) { return {x:r.x, y:r.y, width:r.width, height:r.height}; }
    function id(w) { return String(w.internalId).replace(/[{}]/g, "").toLowerCase(); }
    function windowInfo(w) {
        return {window_id:id(w), caption:String(w.caption).slice(0, 2048),
            resource_class:String(w.resourceClass).slice(0, 512), active:!!w.active,
            minimized:!!w.minimized, geometry:rect(w.frameGeometry),
            output:w.output ? String(w.output.name) : null};
    }
    var result;
    try {
        var windows = workspace.windowList();
        var outputs = workspace.screens.map(function (o) {
            return {name:String(o.name), geometry:rect(o.geometry), scale:o.devicePixelRatio};
        });
        var data = {coordinate_space:"global_logical", outputs:outputs};
        if (request.op === "list") {
            data.windows = windows.slice(0, 512).map(windowInfo);
            data.truncated = windows.length > 512;
        } else {
            var wanted = request.id.replace(/[{}]/g, "").toLowerCase();
            var matches = windows.filter(function (w) { return id(w) === wanted; });
            if (matches.length !== 1) {
                throw new Error("Exact window ID not found; refresh window list");
            }
            var w = matches[0];
            if (request.op === "activate") {
                workspace.activeWindow = w;
                data.action_status = "dispatched";
                data.verified_active = workspace.activeWindow === w;
            }
            data.window = windowInfo(w);
        }
        result = {ok:true, result:data};
    } catch (e) { result = {ok:false, error:String(e).slice(0, 2048)}; }
    callDBus(request.destination, "/org/kwin_mcp/Result", "org.kwin_mcp.Result",
             "Complete", JSON.stringify(result));
})();
"""
    )


def run_window_operation(
    op: str, *, window_id: str = "", env: dict[str, str], timeout: float = 5.0
) -> dict:
    """Return compositor state; never retry an activation with uncertain outcome.

    The subprocess isolates GLib and session-bus state. Its own operation and
    cleanup deadlines fit within the outer watchdog. Titles are untrusted data.
    """
    _validate(op, window_id, timeout)
    if op != "list":
        window_id = str(uuid.UUID(window_id.strip("{}")))
    with tempfile.TemporaryDirectory(prefix="kwin-mcp-window-") as directory:
        payload = {
            "op": op,
            "window_id": window_id,
            "timeout": timeout,
            "script_path": str(Path(directory) / "query.js"),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "kwin_mcp.windows"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "KWin operation timed out; outcome and script cleanup unknown. "
                "Inspect current state before retrying activation."
            ) from exc
    if proc.returncode:
        raise RuntimeError(f"KWin worker failed: {proc.stderr[:2048]}")
    if len(proc.stdout) > _MAX_RESPONSE:
        raise RuntimeError("KWin response exceeds size limit")
    try:
        response = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError("KWin worker returned invalid JSON") from exc
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise RuntimeError("Invalid KWin response shape")
    if not response["ok"]:
        raise RuntimeError(str(response.get("error", "KWin operation failed"))[:2048])
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Invalid KWin result shape")
    return result


def _execute(request: dict) -> dict:
    # Import only inside the isolated worker: unit tests and callers need no GLib.
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    _validate(request["op"], request["window_id"], request["timeout"])
    deadline = time.monotonic() + request["timeout"]
    bus = dbus.SessionBus(private=True, mainloop=DBusGMainLoop())
    loop = GLib.MainLoop()
    response: list[dict] = []
    owner = bus.get_name_owner("org.kde.KWin")

    class Receiver(dbus.service.Object):
        @dbus.service.method(
            _INTERFACE, in_signature="s", out_signature="", sender_keyword="sender"
        )
        def Complete(self, value: str, sender: str = "") -> None:  # noqa: N802
            if sender != owner or response:
                return
            try:
                if len(value) > _MAX_RESPONSE:
                    raise ValueError("KWin response exceeds size limit")
                response.append(json.loads(value))
            except ValueError as exc:
                response.append({"ok": False, "error": str(exc)})
            loop.quit()

    receiver = Receiver(bus, _PATH)
    name = "kwin-mcp-" + uuid.uuid4().hex
    scripting = dbus.Interface(
        bus.get_object("org.kde.KWin", "/Scripting", introspect=False), "org.kde.kwin.Scripting"
    )
    path = Path(request["script_path"])
    path.write_text(_script(request["op"], request["window_id"], bus.get_unique_name()))
    timer = None
    try:
        script_id = int(scripting.loadScript(str(path), name, timeout=_remaining(deadline)))
        if script_id < 0:
            raise RuntimeError("KWin refused to load transient window script")
        script = dbus.Interface(
            bus.get_object("org.kde.KWin", f"/Scripting/Script{script_id}", introspect=False),
            "org.kde.kwin.Script",
        )
        script.run(timeout=_remaining(deadline))
        timer = GLib.timeout_add(
            max(1, int(_remaining(deadline) * 1000)), lambda: (loop.quit(), True)[1]
        )
        if not response:
            loop.run()
        if not response:
            raise TimeoutError("KWin callback timed out; activation outcome unknown")
        return response[0]
    finally:
        if timer is not None:
            GLib.source_remove(timer)
        try:
            scripting.unloadScript(name, timeout=1.0)
        except Exception as exc:
            # Do not turn a completed activation into an apparent failed action.
            if response and isinstance(response[0], dict) and response[0].get("ok"):
                response[0]["result"]["cleanup_warning"] = str(exc)[:512]
        finally:
            receiver.remove_from_connection()
            bus.close()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("KWin operation deadline exceeded; activation outcome unknown")
    return remaining


def main() -> None:
    request = {}
    try:
        request = json.loads(sys.stdin.read(65536))
        response = _execute(request)
    except Exception as exc:
        error = str(exc)[:2048]
        if isinstance(request, dict) and request.get("op") == "activate":
            error += "; activation outcome unknown. Inspect current state before retrying."
        response = {"ok": False, "error": error}
    print(json.dumps(response))


if __name__ == "__main__":
    main()
