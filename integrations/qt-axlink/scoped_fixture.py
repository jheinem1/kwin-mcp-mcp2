"""Own-window end-to-end batch checks driven through the ordinary MCP CLI."""

import json


def verify_scoped_fixture(cli, snap, find, observer, state, target_id, twin_id, tree):
    def button(v):
        return find(v, "push button") or find(v, "button")

    edit = find(tree, "text") or find(tree, "entry")
    control = button(tree)
    check = find(tree, "check box")
    secret = find(tree, "password text")
    assert control and edit and check and secret
    assert secret.get("redacted") and not secret.get("action_key")
    assert "set_text" in edit.get("semantic_actions", []), edit
    assert "press" in control.get("semantic_actions", []), control
    value = "Scoped fixture: Tea ☕"
    expected_check = {"key": check["action_key"], "state": "checked", "present": True}
    plan = [
        {"op": "set_text", "key": edit["action_key"], "text": value},
        {
            "op": "press",
            "key": control["action_key"],
            "verify_key": check["action_key"],
            "state": "checked",
            "present": True,
        },
        {"op": "assert_state", **expected_check},
    ]
    wrong = cli("window_run_steps", window_id=twin_id, steps=plan)
    assert wrong["status"] == "stopped" and wrong["steps_completed"] == 0
    assert wrong["steps"][0]["dispatched"] is False
    before = observer()
    assert before == state
    result = cli("window_run_steps", window_id=target_id, steps=plan)
    assert result["status"] == "verified" and result["steps_completed"] == 3, result
    assert [x["dispatched"] for x in result["steps"]] == [True, True, False]
    assert value not in json.dumps(result)
    target, twin = snap(target_id), snap(twin_id)
    assert "checked" in find(target, "check box")["states"]
    assert "checked" not in find(twin, "check box")["states"]
    remaining_twin = find(twin, "text") or find(twin, "entry")
    assert "set_text" in remaining_twin.get("semantic_actions", [])
    refused = cli(
        "window_run_steps",
        window_id=target_id,
        steps=[{**plan[2], "present": False}, {**plan[1], "present": False}],
    )
    assert refused["status"] == "stopped" and refused["steps_completed"] == 0
    assert len(refused["steps"]) == 1 and refused["steps"][0]["dispatched"] is False
    assert "checked" in find(snap(target_id), "check box")["states"]
    assert observer() == state
    print(
        "SCOPED CLI PASS: fresh handles verified two effects and one assertion; "
        "wrong window and false precondition stopped before dispatch; sibling, "
        "focus and pointer unchanged",
        flush=True,
    )
