from __future__ import annotations

import enum
import sys
import types
import unittest
from unittest import mock


class StateType(enum.Enum):
    ACTIVE = "active"
    FOCUSED = "focused"

    @property
    def value_nick(self) -> str:
        return self.value


gi = types.ModuleType("gi")
gi.__dict__["require_version"] = mock.Mock()
repository = types.ModuleType("gi.repository")
atspi = types.ModuleType("Atspi")
atspi.__dict__["StateType"] = StateType
atspi.__dict__["CoordType"] = types.SimpleNamespace(SCREEN=0)
repository.__dict__["Atspi"] = atspi
gi.__dict__["repository"] = repository
sys.modules.setdefault("gi", gi)
sys.modules.setdefault("gi.repository", repository)

from kwin_mcp import accessibility  # noqa: E402


class StateSet:
    def contains(self, state: StateType) -> bool:
        return state is StateType.FOCUSED


class Action:
    def __init__(self, name: str = "click") -> None:
        self.name = name
        self.invoked = False

    def get_n_actions(self) -> int:
        return 1

    def get_action_name(self, _index: int) -> str:
        return self.name

    def do_action(self, _index: int) -> bool:
        self.invoked = True
        return True


class Element:
    def __init__(
        self, name: str, role: str = "button", children: list[Element] | None = None
    ) -> None:
        self.name = name
        self.role = role
        self.children = children or []
        self.action = Action() if role == "button" else None

    def get_name(self) -> str:
        return self.name

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return ""

    def get_state_set(self) -> StateSet:
        return StateSet()

    def get_component_iface(self) -> None:
        return None

    def get_action_iface(self) -> Action | None:
        return self.action

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> Element:
        return self.children[index]


class AccessibilityTests(unittest.TestCase):
    def test_tree_stops_at_node_budget(self) -> None:
        desktop = Element(
            "desktop",
            "desktop",
            [Element("app", "application", [Element("one"), Element("two")])],
        )
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.get_accessibility_tree(max_nodes=2)
        self.assertIn("2 scanned; truncated", result)
        self.assertNotIn('button "two"', result)

    def test_semantic_action_does_not_require_focus_or_pointer_input(self) -> None:
        save = Element("Save")
        desktop = Element("desktop", "desktop", [Element("kate", "application", [save])])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.invoke_action("Save", "click", app_name="kate")
        self.assertTrue(save.action and save.action.invoked)
        self.assertIn("effect not verified", result)

    def test_metadata_timeout_cannot_be_swallowed_before_mutation(self) -> None:
        for method in ("get_component_iface", "get_action_iface"):
            with self.subTest(method=method):
                save = Element("Save")
                desktop = Element("desktop", "desktop", [save])
                with (
                    mock.patch.object(
                        accessibility.Atspi, "get_desktop", return_value=desktop, create=True
                    ),
                    mock.patch.object(save, method, side_effect=TimeoutError("deadline")),
                    self.assertRaises(TimeoutError),
                ):
                    accessibility.invoke_action("Save", "click")
                self.assertFalse(save.action and save.action.invoked)

    def test_worker_mutation_error_warns_of_unknown_outcome(self) -> None:
        with (
            mock.patch.object(accessibility.Atspi, "set_timeout", create=True),
            mock.patch.object(accessibility.signal, "signal"),
            mock.patch.object(accessibility.signal, "setitimer"),
            mock.patch.object(
                accessibility, "_handle_request", side_effect=TimeoutError("expired")
            ),
        ):
            result = accessibility._execute_request({"op": "invoke_action"})
        self.assertFalse(result["ok"])
        self.assertIn("outcome unknown", result["error"])

    def test_worker_error_handles_unhashable_operation(self) -> None:
        with (
            mock.patch.object(accessibility.signal, "setitimer"),
        ):
            result = accessibility._execute_request({"op": [], "request_timeout_ms": "invalid"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid", result["error"])

    def test_worker_rejects_non_object_json(self) -> None:
        for value in (None, [], 1, "tree"):
            with self.subTest(value=value):
                result = accessibility._execute_request(value)
                self.assertEqual(result, {"ok": False, "error": "Request must be a JSON object"})

    def test_worker_configures_and_cancels_deadline(self) -> None:
        with (
            mock.patch.object(accessibility.Atspi, "set_timeout", create=True) as native_timeout,
            mock.patch.object(accessibility.signal, "signal"),
            mock.patch.object(accessibility.signal, "setitimer") as timer,
            mock.patch.object(accessibility, "_handle_request", return_value={"ok": True}),
        ):
            result = accessibility._execute_request({"request_timeout_ms": 2000})
        self.assertTrue(result["ok"])
        native_timeout.assert_called_once_with(1000, 1000)
        self.assertEqual(timer.call_args_list[0].args[1], 2)
        self.assertEqual(timer.call_args_list[-1].args[1], 0)

    def test_worker_timeout_is_structured_and_timer_is_cancelled(self) -> None:
        with (
            mock.patch.object(accessibility.Atspi, "set_timeout", create=True),
            mock.patch.object(accessibility.signal, "signal"),
            mock.patch.object(accessibility.signal, "setitimer") as timer,
            mock.patch.object(
                accessibility, "_handle_request", side_effect=TimeoutError("expired")
            ),
        ):
            result = accessibility._execute_request({"request_timeout_ms": 2000})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "expired")
        self.assertEqual(timer.call_args_list[-1].args[1], 0)

    def test_action_precondition_prevents_input(self) -> None:
        save = Element("Save")
        desktop = Element("desktop", "desktop", [Element("kate", "application", [save])])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.invoke_action("Save", "click", required_states=["selected"])
        self.assertFalse(save.action and save.action.invoked)
        self.assertIn("action not sent", result)

    def test_action_postcondition_distinguishes_acceptance(self) -> None:
        save = Element("Save")
        desktop = Element("desktop", "desktop", [Element("kate", "application", [save])])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.invoke_action("Save", "click", verify_states=["selected"])
        self.assertTrue(save.action and save.action.invoked)
        self.assertIn("not verified (do not retry)", result)

    def test_action_postcondition_can_be_verified(self) -> None:
        save = Element("Save")
        desktop = Element("desktop", "desktop", [Element("kate", "application", [save])])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.invoke_action("Save", "click", verify_states=["focused"])
        self.assertIn("requested states verified", result)

    def test_focus_does_not_guess_between_windows(self) -> None:
        windows = [Element("first", "frame"), Element("second", "frame")]
        desktop = Element("desktop", "desktop", [Element("kate", "application", windows)])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.focus_window("kate")
        self.assertIn("Ambiguous target", result)

    def test_focus_checks_provider_rejection(self) -> None:
        win = Element("first", "frame")
        component = mock.Mock()
        component.grab_focus.return_value = False
        desktop = Element("desktop", "desktop", [Element("kate", "application", [win])])
        with (
            mock.patch.object(
                accessibility.Atspi, "get_desktop", return_value=desktop, create=True
            ),
            mock.patch.object(win, "get_component_iface", return_value=component),
        ):
            result = accessibility.focus_window("kate", window_title="first")
        self.assertIn("rejected", result)

    def test_wait_never_sleeps_past_remaining_deadline(self) -> None:
        with (
            mock.patch.object(accessibility, "find_elements", return_value=[]),
            mock.patch.object(accessibility.time, "monotonic", side_effect=[0, 0.9, 0.9, 1]),
            mock.patch.object(accessibility.time, "sleep") as sleep,
            self.assertRaises(TimeoutError),
        ):
            accessibility.wait_for_elements("missing", timeout_ms=1000, poll_interval_ms=500)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.1)

    def test_wait_rejects_invalid_poll_interval(self) -> None:
        with self.assertRaises(ValueError):
            accessibility.wait_for_elements("missing", poll_interval_ms=0)

    def test_application_labels_are_single_line_json_strings(self) -> None:
        desktop = Element("desktop", "desktop", [Element("app", "application", [Element("a\nb")])])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.get_accessibility_tree()
        self.assertIn('button "a\\nb"', result)
        self.assertNotIn('button "a\nb"', result)

    def test_find_results_are_limited(self) -> None:
        buttons = [Element(f"Save {index}") for index in range(5)]
        desktop = Element("desktop", "desktop", [Element("kate", "application", buttons)])
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            results = accessibility.find_elements("Save", limit=2)
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
