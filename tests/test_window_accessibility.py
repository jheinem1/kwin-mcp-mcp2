from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from test_accessibility import Element

from kwin_mcp import accessibility
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import SessionType
from kwin_mcp.window_accessibility import match_window_peer

FIRST = "397524a0-4b41-455f-ad26-2751c4a229af"
SECOND = "20a871b8-5f35-47ac-ae64-1fd1b18b6f43"


def target(
    first: tuple[int, int] = (300, 200),
    second: tuple[int, int] | None = (420, 280),
) -> dict:
    def peer(window_id: str, size: tuple[int, int]) -> dict:
        return {"window_id": window_id, "client_geometry": {"width": size[0], "height": size[1]}}

    peers = [peer(FIRST, first)]
    if second:
        peers.append(peer(SECOND, second))
    return {"window_id": FIRST, "pid": 101, "caption": "Same title", "peers": peers}


class BridgeElement(Element):
    def __init__(
        self,
        name: str,
        role: str,
        children: list[Element] | None = None,
        size: tuple[int, int] = (0, 0),
        pid: int = 101,
    ) -> None:
        super().__init__(name, role, children)
        self.pid = pid
        self.size = size

    def get_process_id(self) -> int:
        return self.pid

    def get_toolkit_name(self) -> str:
        return "gtk"

    def get_toolkit_version(self) -> str:
        return "3.24.52"

    def get_component_iface(self) -> mock.Mock:
        return mock.Mock(
            get_extents=mock.Mock(
                return_value=SimpleNamespace(width=self.size[0], height=self.size[1])
            )
        )


class MatchingTests(unittest.TestCase):
    def test_complete_exact_size_cohort(self) -> None:
        self.assertEqual(
            match_window_peer(target(), [(420, 280), (300, 200)], "gtk3"),
            (1, "pid_title_and_unique_gtk3_client_size"),
        )
        self.assertEqual(
            match_window_peer(target(second=None), [None], "unknown"), (0, "pid_and_exact_title")
        )

    def test_ieee_representation_is_not_a_geometric_tolerance(self) -> None:
        k = target()
        rect = k["peers"][1]["client_geometry"]
        rect["width"] = 419.99999999999994
        self.assertEqual(
            match_window_peer(k, [(300, 200), (420, 280)], "gtk3"),
            (0, "pid_title_and_unique_gtk3_client_size"),
        )
        for non_exact in (419.99999999, 419.99, 419, True):
            with self.subTest(non_exact=non_exact):
                rect["width"] = non_exact
                self.assertIsNone(match_window_peer(k, [(300, 200), (420, 280)], "gtk3"))

    def test_fail_closed_on_equal_size_other_toolkit_or_partial(self) -> None:
        for w, sizes, toolkit in (
            (target(second=(300, 200)), [(300, 200), (300, 200)], "gtk3"),
            (target(), [(300, 200), (420, 280)], "qt6"),
            (target(), [(300, 200)], "gtk3"),
            (target(), [(300, 201), (420, 280)], "gtk3"),
            (target(), [None, (420, 280)], "gtk3"),
        ):
            with self.subTest(sizes=sizes, toolkit=toolkit):
                self.assertIsNone(match_window_peer(w, sizes, toolkit))

    def test_exact_subtree_only_redacts_by_default(self) -> None:
        password = BridgeElement(
            "SECRET password", "password text", [Element("SECRET child", "button")]
        )
        own = BridgeElement(
            "Same title", "frame", [Element("Correct target"), password], (300, 200)
        )
        sibling = BridgeElement("Same title", "frame", [Element("Wrong sibling")], (420, 280))
        other_pid = BridgeElement(
            "Different process",
            "application",
            [BridgeElement("Same title", "frame", [Element("Other app probe")], pid=102)],
            pid=102,
        )
        desktop = BridgeElement(
            "desktop",
            "desktop",
            [other_pid, BridgeElement("app", "application", [sibling, own])],
        )
        with (
            mock.patch.object(
                accessibility.Atspi, "get_desktop", return_value=desktop, create=True
            ),
            mock.patch.object(accessibility.Atspi.CoordType, "WINDOW", 1, create=True),
        ):
            default = accessibility.get_window_accessibility(
                target(), max_depth=4, max_nodes=10, include_labels=False
            )
            labeled = accessibility.get_window_accessibility(
                target(), max_depth=4, max_nodes=10, include_labels=True
            )
            clipped = accessibility.get_window_accessibility(
                target(), max_depth=4, max_nodes=1, include_labels=True
            )
        self.assertEqual(default["status"], "ok")
        self.assertNotIn("Correct target", json.dumps(default))
        self.assertEqual(labeled["tree"]["children"][0]["label"], "Correct target")
        self.assertTrue(labeled["tree"]["children"][1]["redacted"])
        for value in ("Wrong sibling", "Other app probe", "SECRET", "Same title"):
            self.assertNotIn(value, json.dumps(labeled))
        self.assertTrue(clipped["truncated"])

    def test_incomplete_atspi_cohort_does_not_read_known_sibling(self) -> None:
        window = BridgeElement("Same title", "frame", [Element("wrong")], (300, 200))
        desktop = BridgeElement(
            "desktop", "desktop", [BridgeElement("app", "application", [window])]
        )
        with mock.patch.object(
            accessibility.Atspi, "get_desktop", return_value=desktop, create=True
        ):
            result = accessibility.get_window_accessibility(
                target(), max_depth=4, max_nodes=10, include_labels=True
            )
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("tree", result)


class EngineTests(unittest.TestCase):
    def test_live_window_rechecks_identity_and_lock_before_returning(self) -> None:
        engine = AutomationEngine()
        engine._session = mock.Mock(
            is_running=True, info=SimpleNamespace(session_type=SessionType.LIVE)
        )
        result = {"status": "ok", "tree": {"role": "frame"}}
        with (
            mock.patch.object(engine, "_session_env", return_value={}),
            mock.patch.object(
                engine, "_run_atspi", return_value={"ok": True, "result": json.dumps(result)}
            ),
            mock.patch(
                "kwin_mcp.windows.run_window_operation",
                side_effect=[target(), {**target(), "pid": 200}],
            ) as query,
        ):
            response = engine.window_accessibility(FIRST)
        self.assertEqual(query.call_count, 2)
        for call in query.call_args_list:
            self.assertEqual(
                call, mock.call("atspi_target", window_id=FIRST, env={}, require_unlocked=True)
            )
        self.assertIn("identity changed", response)
        self.assertNotIn("frame", response)

    def test_locked_check_refusal_does_not_query_atspi(self) -> None:
        engine = AutomationEngine()
        engine._session = mock.Mock(
            is_running=True, info=SimpleNamespace(session_type=SessionType.LIVE)
        )
        with (
            mock.patch.object(engine, "_session_env", return_value={}),
            mock.patch.object(engine, "_run_atspi") as atspi,
            mock.patch(
                "kwin_mcp.windows.run_window_operation",
                side_effect=RuntimeError("locked"),
            ) as query,
        ):
            response = engine.window_accessibility(FIRST)
        query.assert_called_once_with(
            "atspi_target", window_id=FIRST, env={}, require_unlocked=True
        )
        atspi.assert_not_called()
        self.assertIn("could not be verified", response)
