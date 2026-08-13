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
gi.require_version = mock.Mock()
repository = types.ModuleType("gi.repository")
atspi = types.ModuleType("Atspi")
atspi.StateType = StateType
atspi.CoordType = types.SimpleNamespace(SCREEN=0)
repository.Atspi = atspi
gi.repository = repository
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
        self.assertEqual(result, "Invoked 'click' on button \"Save\"")

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
