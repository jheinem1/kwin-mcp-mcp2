from __future__ import annotations

import unittest
from unittest.mock import Mock

from kwin_mcp.window_steps import MAX_STEPS, run_window_steps

WID = "35b34383-4354-481e-b227-a4956e1a2e01"
INPUT = "inputkey_" + "a" * 24
BUTTON = "buttonkey_" + "b" * 24
STATE = "statekey_" + "c" * 24
TXT = {"op": "set_text", "key": INPUT, "text": "sample text"}
PRESS = {"op": "press", "key": BUTTON, "verify_key": STATE, "state": "checked", "present": True}
ASSERT = {"op": "assert_state", "key": STATE, "state": "checked", "present": True}
VERIFIED = {"status": "verified", "dispatched": True, "verified": True}


def fixture_handle(character):
    return character * 22 + "." + character * 24


def node(k, handle, *, states=(), redacted=False):
    return {
        "action_key": k,
        "action_handle": fixture_handle(handle),
        "states": list(states),
        "redacted": redacted,
        "children": [],
    }


def snapshot(*items, wid=WID, match="compositor_registered_qt_root"):
    return {"status": "ok", "match": match, "window_id": wid, "tree": {"children": list(items)}}


class TestScopedWindowSteps(unittest.TestCase):
    def test_rebinds_after_every_action(self):
        snapshots = Mock(
            side_effect=[
                snapshot(node(INPUT, "a"), node(BUTTON, "b"), node(STATE, "q")),
                snapshot(node(INPUT, "c"), node(BUTTON, "d"), node(STATE, "r")),
                snapshot(node(STATE, "s", states=["checked"])),
            ]
        )
        action = Mock(return_value=VERIFIED)
        reply = run_window_steps(WID, [TXT, PRESS, ASSERT], snapshot=snapshots, action=action)
        self.assertEqual(reply["status"], "verified")
        self.assertEqual(reply["steps_completed"], 3)
        self.assertEqual(snapshots.call_count, 3)
        self.assertEqual(action.call_count, 2)
        self.assertEqual(action.call_args_list[0].args, (WID, fixture_handle("a"), "set_text"))
        self.assertEqual(action.call_args_list[1].args, (WID, fixture_handle("d"), "press"))
        self.assertEqual(action.call_args_list[1].kwargs["verify_handle"], fixture_handle("r"))
        self.assertNotIn(TXT["text"], str(reply))

    def test_full_syntax_checked_before_snapshot(self):
        for script in (
            [TXT, {**TXT, "script": "anything"}],
            [TXT] * (MAX_STEPS + 1),
            [{"op": "command", "key": INPUT}],
            [{**TXT, "text": "x\ny"}],
        ):
            with self.subTest(script=script):
                before, action = Mock(), Mock()
                with self.assertRaises(ValueError):
                    run_window_steps(WID, script, snapshot=before, action=action)
                before.assert_not_called()
                action.assert_not_called()

    def test_wrong_window_heuristic_redacted_missing_and_ambiguous(self):
        cases = (
            snapshot(node(INPUT, "a"), wid="00000000-0000-0000-0000-000000000000"),
            snapshot(node(INPUT, "a"), match="pid_and_exact_title"),
            snapshot(node(INPUT, "a", redacted=True)),
            snapshot(node(BUTTON, "a")),
            snapshot(node(INPUT, "a"), node(INPUT, "b")),
        )
        for case in cases:
            action = Mock(return_value=VERIFIED)
            reply = run_window_steps(WID, [TXT], snapshot=Mock(return_value=case), action=action)
            self.assertEqual(reply["status"], "stopped")
            self.assertEqual(reply["steps_completed"], 0)
            action.assert_not_called()

    def test_private_values_stay_redacted_even_if_entry_is_eligible(self):
        edit = node(INPUT, "a", states=["editable"], redacted=True)
        edit.update(role="text", semantic_actions=["set_text"])
        action = Mock(return_value=VERIFIED)
        result = run_window_steps(
            WID, [TXT], snapshot=Mock(return_value=snapshot(edit)), action=action
        )
        self.assertEqual(result["steps_completed"], 1)
        self.assertNotIn(TXT["text"], str(result))
        edit["states"].append("protected")
        action.reset_mock()
        result = run_window_steps(
            WID, [TXT], snapshot=Mock(return_value=snapshot(edit)), action=action
        )
        self.assertEqual(result["steps_completed"], 0)
        action.assert_not_called()

    def test_stale_target_after_write_stops_and_reports_partial(self):
        before = Mock(side_effect=[snapshot(node(INPUT, "a")), snapshot(node(STATE, "b"))])
        action = Mock(return_value=VERIFIED)
        reply = run_window_steps(WID, [TXT, PRESS, ASSERT], snapshot=before, action=action)
        self.assertEqual(reply["steps_completed"], 1)
        self.assertEqual(reply["steps"][1]["reason"], "fresh_binding_unavailable")
        self.assertEqual(before.call_count, 2)
        action.assert_called_once()

    def test_action_outcomes_never_retried(self):
        for result, expected in (
            ({"status": "refused", "dispatched": False}, False),
            ({"status": "unverified", "dispatched": True}, True),
            (RuntimeError("connection lost"), None),
            ({"status": "verified", "dispatched": False}, False),
        ):
            with self.subTest(result=result):
                before = Mock(return_value=snapshot(node(INPUT, "a")))
                action = (
                    Mock(side_effect=result)
                    if isinstance(result, Exception)
                    else Mock(return_value=result)
                )
                reply = run_window_steps(WID, [TXT, TXT], snapshot=before, action=action)
                self.assertEqual(reply["status"], "stopped")
                self.assertIs(reply["steps"][0]["dispatched"], expected)
                self.assertEqual(reply["steps_completed"], 0)
                action.assert_called_once()

    def test_deadline_before_effect(self):
        action = Mock()
        time = Mock(side_effect=[0, 0, 31])
        reply = run_window_steps(
            WID,
            [TXT],
            snapshot=Mock(return_value=snapshot(node(INPUT, "a"))),
            action=action,
            monotonic=time,
        )
        self.assertEqual(reply["steps"][0]["reason"], "batch_deadline")
        action.assert_not_called()
