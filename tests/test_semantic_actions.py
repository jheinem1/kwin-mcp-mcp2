import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kwin_mcp import accessibility as ax
from kwin_mcp.core import AutomationEngine, SessionType
from kwin_mcp.semantic_actions import SnapshotHandles, valid_text

WINDOW = "5292d985-6d4f-40da-9fe6-0fdd5c3832dc"
TARGET = {"window_id": WINDOW, "pid": 17, "wayland_ax_root": 123, "wayland_ax_generation": "a"}
REF = {
    "owner": ":1.5",
    "path": "/qt/test",
    "role": "push button",
    "root_path_indices": [3, 2],
    "actions": ["press"],
}


class SnapshotTests(unittest.TestCase):
    def test_fresh_tokens_stable_keys_session_bound_and_single_use(self):
        store = SnapshotHandles()
        session = object()

        def make(bound_session):
            tree = {"children": [{"_action_ref": dict(REF)}]}
            self.assertTrue(store.issue(tree, bound_session, TARGET))
            node = tree["children"][0]
            self.assertNotIn("_action_ref", node)
            self.assertNotIn(REF["path"], json.dumps(tree))
            return node["action_handle"], node["action_key"]

        a, key = make(session)
        b, refresh = make(session)
        self.assertNotEqual(a, b)
        self.assertEqual(key, refresh)
        self.assertIsNone(store.take(a, b, session, WINDOW))
        self.assertIsNone(store.take(key, None, session, WINDOW))
        self.assertIsNone(store.take(a, a, session, "another-window"))
        self.assertEqual(store.take(a, a, session, WINDOW), (TARGET, REF, REF))
        self.assertIsNone(store.take(a, None, session, WINDOW))
        other_session = object()
        _, changed = make(other_session)
        self.assertNotEqual(key, changed)
        self.assertIsNone(store.take(b, None, session, WINDOW))

    @patch("kwin_mcp.semantic_actions.time.monotonic")
    def test_expiry(self, clock):
        clock.return_value = 1
        store = SnapshotHandles()
        tree = {"_action_ref": dict(REF)}
        session = object()
        store.issue(tree, session, TARGET)
        clock.return_value = 31
        self.assertIsNone(store.take(tree["action_handle"], None, session, WINDOW))

    def test_text_rejects_control_characters_multiline_and_oversize(self):
        for text in ("", "a\nb", "a\u2028b", "a\u202eb", "a" * 257):
            self.assertFalse(valid_text(text))
        self.assertTrue(valid_text("Owned text 🎉"))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = object()
        self.node = Mock()
        self.node.get_action_iface().get_n_actions.return_value = 1
        self.node.get_action_iface().get_action_name.return_value = "press"
        self.node.get_action_iface().do_action.return_value = True
        self.observer = Mock()
        self.state = {"enabled", "sensitive", "showing", "visible"}
        self.action_ref = {"id": "action"}
        self.observer_ref = {"id": "observer"}
        self.request = {
            "action": "press",
            "target": TARGET,
            "element_ref": self.action_ref,
            "verify_ref": self.observer_ref,
            "verify_state": "checked",
            "verify_present": True,
        }
        self.lock = self.enterContext(
            patch.object(ax, "_private_stage_unlocked", return_value=True)
        )
        self.registry = self.enterContext(
            patch.object(ax, "_resolve_registered_root", return_value=self.root)
        )
        self.kwin = self.enterContext(
            patch("kwin_mcp.windows.run_window_operation", return_value=TARGET)
        )
        self.lookup = self.enterContext(patch.object(ax, "_find_bound_node", side_effect=self.find))
        self.already_checked = False

    def find(self, _root, descriptor):
        if descriptor == self.action_ref:
            return self.node, "push button", self.state
        checked = self.already_checked or self.node.get_action_iface().do_action.called
        return self.observer, "check box", {"checked"} if checked else set()

    def check_dispatched(self, expected=True):
        self.assertEqual(self.node.get_action_iface().do_action.called, expected)

    def test_semantic_press_has_a_state_transition(self):
        result = ax.run_window_semantic_action(self.request)
        self.assertTrue(result["verified"])
        self.assertTrue(result["dispatched"])
        self.node.get_action_iface().do_action.assert_called_once_with(0)

    def test_locked_unbound_stale_refuse_without_dispatch(self):
        self.lock.return_value = False
        self.assertFalse(ax.run_window_semantic_action(self.request)["dispatched"])
        self.lock.return_value = True
        self.registry.return_value = None
        self.assertFalse(ax.run_window_semantic_action(self.request)["dispatched"])
        self.registry.return_value = self.root
        self.kwin.return_value = {**TARGET, "wayland_ax_generation": "different"}
        self.assertFalse(ax.run_window_semantic_action(self.request)["dispatched"])
        self.check_dispatched(False)

    def test_preexisting_verification_is_not_proof(self):
        self.already_checked = True
        result = ax.run_window_semantic_action(self.request)
        self.assertEqual(result["status"], "refused")
        self.check_dispatched(False)

    def test_uncertain_action_is_never_repeated(self):
        self.node.get_action_iface().do_action.side_effect = TimeoutError("private string")
        result = ax.run_window_semantic_action(self.request)
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["dispatched"])
        self.assertNotIn("private string", json.dumps(result))
        self.node.get_action_iface().do_action.assert_called_once()

    def test_secret_or_existing_value_refused_before_text_dispatch(self):
        self.request.update(
            action="set_text", text="owned", verify_ref=None, verify_state=None, verify_present=None
        )
        self.lookup.side_effect = None
        self.lookup.return_value = (
            self.node,
            "password text",
            self.state | {"editable", "single-line"},
        )
        self.assertFalse(ax.run_window_semantic_action(self.request)["dispatched"])
        self.lookup.return_value = self.node, "text", self.state | {"editable", "single-line"}
        self.node.get_character_count.return_value = 7
        self.assertFalse(ax.run_window_semantic_action(self.request)["dispatched"])
        self.node.get_editable_text_iface().set_text_contents.assert_not_called()
        self.node.get_text.assert_not_called()

    def test_marked_empty_field_verifies_with_no_value_leak(self):
        payload = "fixture 🎉"
        entry_states = self.state | {"editable"}
        self.lookup.side_effect = None
        self.lookup.return_value = self.node, "text", entry_states
        self.node.get_accessible_id.return_value = "jamesos.ax.single-line.v1"
        raw = self.node.get_editable_text_iface()
        raw.set_text_contents.return_value = True
        self.node.get_character_count.side_effect = lambda: (
            len(payload) if raw.set_text_contents.called else 0
        )
        self.request.update(
            action="set_text", text=payload, verify_ref=None, verify_state=None, verify_present=None
        )
        read_text = Mock(return_value=payload)
        with patch.object(ax.Atspi, "Text", SimpleNamespace(get_text=read_text), create=True):
            result = ax.run_window_semantic_action(self.request)
        self.assertEqual(result["status"], "verified")
        raw.set_text_contents.assert_called_once_with(payload)
        read_text.assert_called_once_with(self.node, 0, -1)
        self.assertNotIn(payload, json.dumps(result))


class EngineTests(unittest.TestCase):
    def test_live_session_refuses_even_if_mocked_staging_socket(self):
        engine = AutomationEngine()
        engine._session = SimpleNamespace(
            is_running=True,
            info=SimpleNamespace(
                session_type=SessionType.LIVE,
                wayland_socket="axlink-0",
                dbus_address=None,
                home_dir=None,
            ),
        )
        with (
            patch.object(engine, "_private_semantic_socket", return_value=True),
            patch.object(engine, "_run_atspi") as worker,
        ):
            result = engine.window_semantic_action(WINDOW, "handle", "set_text", text="owned")
        worker.assert_not_called()
        self.assertEqual(json.loads(result.split("\n", 1)[1])["status"], "refused")


class EntryEligibilityTests(unittest.TestCase):
    def test_qt_missing_single_line_requires_explicit_field_marker(self):
        node = Mock()
        node.get_accessible_id.return_value = ""
        self.assertFalse(ax._eligible_semantic_entry(node, "text", {"editable"}))
        node.get_accessible_id.return_value = "jamesos.ax.single-line.v1"
        self.assertTrue(ax._eligible_semantic_entry(node, "text", {"editable"}))
        self.assertFalse(ax._eligible_semantic_entry(node, "text", {"editable", "multi-line"}))
        self.assertFalse(ax._eligible_semantic_entry(node, "text", {"editable", "protected"}))
        self.assertFalse(ax._eligible_semantic_entry(node, "password text", {"editable"}))
