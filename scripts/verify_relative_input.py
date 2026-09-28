import json
import shlex
import sys
import tempfile
import time
from pathlib import Path

from kwin_mcp.core import AutomationEngine

base = Path(tempfile.mkdtemp(prefix="kwin-relative-probe-"))
e = AutomationEngine()


def wait_for(pred):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (base / "result.json").exists():
            result = json.loads((base / "result.json").read_text())
            if pred(result):
                return result
        time.sleep(0.05)
    raise RuntimeError("Probe timeout: " + str(base))


try:
    print(e.session_start(isolate_home=True, screen_width=800, screen_height=600), flush=True)
    assert e._input
    print(
        e.launch_app(
            shlex.join([sys.executable, str(Path(__file__).with_name("glfw_input_probe.py"))]),
            env={"PROBE_DIR": str(base)},
        ),
        flush=True,
    )
    wait_for(lambda r: r["focused"])
    time.sleep(0.5)
    for phase, raw in [(1, False), (2, True)]:
        (base / "command.json").write_text(json.dumps({"phase": phase, "raw": raw}))
        before = wait_for(lambda r, phase=phase: r["phase"] == phase)
        assert not raw or before["raw_supported"], "Raw mouse motion unsupported"
        print(e.mouse_move_relative(120, -40, 120), flush=True)
        print(e.mouse_button_down(), flush=True)
        print(e.mouse_button_up(), flush=True)
        after = wait_for(lambda r: len(r["buttons"]) >= 2 and bool(r["positions"]))
        delta = [after["positions"][-1][i] - before["initial"][i] for i in range(2)]
        assert all(abs(a - b) < 0.05 for a, b in zip(delta, [120, -40], strict=True)), (
            raw,
            delta,
            after,
        )
        assert after["buttons"] == [[0, 1], [0, 0]], after
        print(
            json.dumps({"raw": raw, "delta": delta, "buttons": after["buttons"], "PASS": True}),
            flush=True,
        )
    (base / "command.json").write_text('{"stop":true}')
finally:
    if e._session and e._session.info:
        for app in e._session.info.apps.values():
            print(app.log_path.read_text(), flush=True)
    print(e.session_stop(), flush=True)
