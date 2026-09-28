import ctypes as c
import json
import os
import time
from pathlib import Path

base = Path(os.environ["PROBE_DIR"])
g = c.CDLL("libglfw.so.3")
g.glfwInitHint.argtypes = [c.c_int, c.c_int]
g.glfwInitHint(0x00050003, 0x00060003)  # GLFW_PLATFORM_WAYLAND
assert g.glfwInit(), "GLFW init failed"

g.glfwCreateWindow.argtypes = [c.c_int, c.c_int, c.c_char_p, c.c_void_p, c.c_void_p]
g.glfwCreateWindow.restype = c.c_void_p
w = g.glfwCreateWindow(640, 480, b"Isolated relative mouse probe", None, None)
assert w
g.glfwMakeContextCurrent.argtypes = [c.c_void_p]
g.glfwSwapBuffers.argtypes = [c.c_void_p]
g.glfwMakeContextCurrent(w)
g.glfwSwapBuffers(w)
for name, args in [
    ("glfwSetInputMode", [c.c_void_p, c.c_int, c.c_int]),
    ("glfwGetWindowAttrib", [c.c_void_p, c.c_int]),
    ("glfwDestroyWindow", [c.c_void_p]),
]:
    getattr(g, name).argtypes = args
cbtype = c.CFUNCTYPE(None, c.c_void_p, c.c_double, c.c_double)
positions = []


@cbtype
def cursor(win, x, y):
    positions.append([x, y])


g.glfwSetCursorPosCallback.argtypes = [c.c_void_p, cbtype]
g.glfwSetCursorPosCallback(w, cursor)
buttons = []
button_type = c.CFUNCTYPE(None, c.c_void_p, c.c_int, c.c_int, c.c_int)


@button_type
def button(win, btn, action, mods):
    buttons.append([btn, action])


g.glfwSetMouseButtonCallback.argtypes = [c.c_void_p, button_type]
g.glfwSetMouseButtonCallback(w, button)
g.glfwSetInputMode(w, 0x00033001, 0x00034003)  # cursor disabled
phase = -1
try:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        g.glfwSwapBuffers(w)
        g.glfwPollEvents()
        cmd = (
            json.loads((base / "command.json").read_text())
            if (base / "command.json").exists()
            else {"phase": 0, "raw": False}
        )
        if cmd.get("stop"):
            break
        if cmd["phase"] != phase:
            phase = cmd["phase"]
            positions.clear()
            buttons.clear()
            raw = bool(g.glfwRawMouseMotionSupported())
            if raw:
                g.glfwSetInputMode(w, 0x00033005, int(cmd["raw"]))
            x, y = c.c_double(), c.c_double()
            g.glfwGetCursorPos.argtypes = [c.c_void_p, c.POINTER(c.c_double), c.POINTER(c.c_double)]
            g.glfwGetCursorPos(w, c.byref(x), c.byref(y))
            initial = [x.value, y.value]
        result = {
            "phase": phase,
            "raw_supported": raw,
            "initial": initial,
            "positions": positions,
            "buttons": buttons,
            "focused": g.glfwGetWindowAttrib(w, 0x00020001),
        }
        (base / "result.tmp").write_text(json.dumps(result))
        (base / "result.tmp").replace(base / "result.json")
        time.sleep(0.005)
finally:
    g.glfwDestroyWindow(w)
    g.glfwTerminate()
