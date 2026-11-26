"""AT-SPI2 accessibility tree reader.

Can be run as a subprocess CLI for isolated D-Bus session support.
Reads a JSON request from stdin and writes a JSON response to stdout.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, cast

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

from kwin_mcp.semantic_actions import (  # noqa: E402
    BLOCKED_STATES,
    BUTTON_ROLES,
    ENTRY_ROLES,
    PRIVATE_ANCESTORS,
    report,
    valid_request,
)
from kwin_mcp.window_accessibility import (  # noqa: E402
    MAX_APPLICATIONS,
    MAX_TOP_LEVELS,
    match_window_peer,
)

_DEFAULT_MAX_DEPTH = 8
_DEFAULT_MAX_NODES = 200
_DEFAULT_RESULT_LIMIT = 50
_COMPACT_STATES = {"active", "checked", "disabled", "expanded", "focused", "selected"}


def _quoted_label(text: str, limit: int = 160) -> str:
    """Quote and bound application-controlled labels for one-line output."""
    suffix = "…" if len(text) > limit else ""
    return json.dumps(text[:limit] + suffix, ensure_ascii=False)


@dataclass
class ElementInfo:
    """Information about a single UI element."""

    role: str
    name: str
    description: str
    states: list[str]
    x: int
    y: int
    width: int
    height: int
    actions: list[str]
    children_count: int
    depth: int


@dataclass
class TraversalBudget:
    max_nodes: int
    visited: int = 0
    truncated: bool = False


def get_accessibility_tree(
    app_name: str = "",
    max_depth: int = _DEFAULT_MAX_DEPTH,
    role: str = "",
    max_nodes: int = _DEFAULT_MAX_NODES,
) -> str:
    """Get the accessibility tree as a formatted text string.

    Args:
        app_name: Filter to a specific application (empty = all apps).
        max_depth: Maximum tree depth to traverse.
        role: Filter to elements with this role (empty = all roles).
            Non-matching elements are hidden but their children are still traversed.

    Returns:
        Formatted text representation of the accessibility tree.
    """
    desktop = Atspi.get_desktop(0)
    lines: list[str] = []
    total = 0
    role_filter = role.lower()
    budget = TraversalBudget(min(max(1, max_nodes), 1000))
    max_depth = min(max(0, max_depth), 32)

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        count = _format_element(
            app,
            lines,
            depth=0,
            max_depth=max_depth,
            role_filter=role_filter,
            budget=budget,
        )
        total += count
        if budget.truncated:
            break

    if not lines:
        return "(no accessible applications found)"

    suffix = "; truncated" if budget.truncated else ""
    header = f"AT-SPI: {total} shown, {budget.visited} scanned{suffix}\n"
    return header + "\n".join(lines)


def find_elements(
    query: str,
    app_name: str = "",
    states: list[str] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
) -> list[ElementInfo]:
    """Find elements matching a query string and/or required states.

    Searches element names, roles, and descriptions. Optionally filters
    by AT-SPI2 states.

    Args:
        query: Search string (case-insensitive). Empty string matches all elements.
        app_name: Filter to a specific application.
        states: If provided, only return elements that have ALL of these states.

    Returns:
        List of matching ElementInfo objects.
    """
    desktop = Atspi.get_desktop(0)
    results: list[ElementInfo] = []
    query_lower = query.lower()
    limit = min(max(1, limit), 200)
    budget = TraversalBudget(1000)

    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue

        name = app.get_name() or ""
        if app_name and app_name.lower() not in name.lower():
            continue

        _search_element(
            app,
            query_lower,
            results,
            depth=0,
            max_depth=15,
            required_states=states,
            limit=limit,
            budget=budget,
        )
        if len(results) >= limit or budget.truncated:
            break

    return results


def list_windows() -> str:
    """List accessible application windows with titles and active/focused state.

    Returns:
        Formatted list of apps with per-window title and state markers.
    """
    desktop = Atspi.get_desktop(0)
    lines: list[str] = []
    app_count = 0
    window_count = 0
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        app_name = app.get_name() or "(unnamed)"
        child_count = app.get_child_count()
        app_count += 1
        lines.append(f"- {_quoted_label(app_name)} ({child_count} windows)")
        for j in range(child_count):
            win = app.get_child_at_index(j)
            if win is None:
                continue
            win_title = win.get_name() or "(untitled)"
            state_set = win.get_state_set()
            markers: list[str] = []
            if state_set.contains(Atspi.StateType.ACTIVE):
                markers.append("active")
            if state_set.contains(Atspi.StateType.FOCUSED):
                markers.append("focused")
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            lines.append(f"    - {_quoted_label(win_title)}{marker_str}")
            window_count += 1
            if window_count >= 200:
                lines.append("… truncated")
                return f"Applications ({app_count}+):\n" + "\n".join(lines)
        if app_count >= 200:
            lines.append("… truncated")
            return f"Applications ({app_count}+):\n" + "\n".join(lines)
    if not lines:
        return "(no accessible applications found)"
    return f"Applications ({app_count}):\n" + "\n".join(lines)


def focus_window(app_name: str, window_title: str = "") -> str:
    """Focus one unambiguous accessible window, optionally by exact title.

    AT-SPI titles are not KWin window IDs. Reject ambiguous matches instead of
    guessing, and report the provider's acceptance separately from focus state.
    """
    if not app_name:
        return "An application name is required"
    desktop = Atspi.get_desktop(0)
    matches: list[tuple[str, Atspi.Accessible]] = []
    for i in range(min(desktop.get_child_count(), 200)):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        name = app.get_name() or ""
        if app_name.lower() not in name.lower():
            continue
        for j in range(min(app.get_child_count(), 200)):
            win = app.get_child_at_index(j)
            if win is not None and (not window_title or win.get_name() == window_title):
                matches.append((name, win))
    if not matches:
        return f"No window matching {app_name!r}, title={window_title!r} found"
    if len(matches) != 1:
        return f"Ambiguous target: {len(matches)} windows match; specify an exact window title"
    name, win = matches[0]
    component = win.get_component_iface()
    if component is None or not component.grab_focus():
        return f"Focus request rejected for {_quoted_label(name)}"
    states = win.get_state_set()
    if states.contains(Atspi.StateType.ACTIVE) or states.contains(Atspi.StateType.FOCUSED):
        return f"Focused: {_quoted_label(name)} (state verified)"
    return f"Focus request accepted for {_quoted_label(name)}; focus state not verified"


def wait_for_elements(
    query: str,
    app_name: str = "",
    timeout_ms: int = 5000,
    poll_interval_ms: int = 200,
    states: list[str] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
) -> list[ElementInfo]:
    """Poll for elements matching a query and/or states until found or timeout.

    Args:
        query: Search string (case-insensitive). Empty string matches all elements.
        app_name: Filter to a specific application.
        timeout_ms: Maximum wait time in milliseconds.
        poll_interval_ms: Polling interval in milliseconds.
        states: If provided, only match elements that have ALL of these states.

    Returns:
        List of matching elements.

    Raises:
        TimeoutError: If no elements found within timeout.
    """
    if timeout_ms < 0 or poll_interval_ms <= 0:
        raise ValueError("timeout_ms must be nonnegative and poll_interval_ms must be positive")
    deadline = time.monotonic() + timeout_ms / 1000.0
    interval = poll_interval_ms / 1000.0

    while True:
        elements = find_elements(query, app_name=app_name, states=states, limit=limit)
        if elements:
            return elements

        if time.monotonic() >= deadline:
            criteria = f"query='{query}'"
            if states:
                criteria += f", states={states}"
            msg = f"Timeout after {timeout_ms}ms: no elements matching {criteria}"
            raise TimeoutError(msg)

        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _format_element(
    element: Atspi.Accessible,
    lines: list[str],
    depth: int,
    max_depth: int,
    role_filter: str = "",
    budget: TraversalBudget | None = None,
) -> int:
    """Recursively format an element and its children. Returns element count.

    When role_filter is set, only elements with a matching role are displayed,
    but children of non-matching elements are still traversed.
    """
    if depth > max_depth:
        return 0
    if budget is None:
        budget = TraversalBudget(_DEFAULT_MAX_NODES)
    if budget.visited >= budget.max_nodes:
        budget.truncated = True
        return 0
    budget.visited += 1

    info = _extract_info(element, depth)
    role_match = not role_filter or role_filter == info.role.lower()

    count = 0
    if role_match:
        indent = "  " * depth
        states = [state for state in info.states if state in _COMPACT_STATES]
        states_str = f" [{','.join(states)}]" if states else ""
        actions_str = f" {{{','.join(info.actions)}}}" if info.actions else ""
        line = (
            f"{indent}{info.role} {_quoted_label(info.name)} "
            f"{info.x},{info.y},{info.width}x{info.height}{states_str}{actions_str}"
        )
        lines.append(line)
        count = 1

    # Always traverse children even when the current element is filtered out
    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            count += _format_element(child, lines, depth + 1, max_depth, role_filter, budget)
            if budget.truncated:
                break

    return count


def _search_element(
    element: Atspi.Accessible,
    query: str,
    results: list[ElementInfo],
    depth: int,
    max_depth: int,
    required_states: list[str] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
    budget: TraversalBudget | None = None,
) -> None:
    """Recursively search for elements matching the query and/or required states."""
    if budget is None:
        budget = TraversalBudget(1000)
    if depth > max_depth or len(results) >= limit:
        return
    if budget.visited >= budget.max_nodes:
        budget.truncated = True
        return
    budget.visited += 1

    info = _extract_info(element, depth)

    # Check if element matches query (empty query matches everything)
    query_match = (
        query in info.name.lower()
        or query in info.role.lower()
        or query in info.description.lower()
    )

    # Check if element matches required states
    states_match = required_states is None or all(s in info.states for s in required_states)

    if query_match and states_match:
        results.append(info)

    # Search children
    for i in range(info.children_count):
        child = element.get_child_at_index(i)
        if child is not None:
            _search_element(
                child,
                query,
                results,
                depth + 1,
                max_depth,
                required_states,
                limit,
                budget,
            )
            if len(results) >= limit or budget.truncated:
                break


def invoke_action(
    query: str,
    action: str,
    app_name: str = "",
    match_index: int = 0,
    required_states: list[str] | None = None,
    verify_states: list[str] | None = None,
) -> str:
    """Invoke an AT-SPI action on a matching element without pointer input."""
    desktop = Atspi.get_desktop(0)
    matches: list[tuple[Atspi.Accessible, ElementInfo]] = []
    query_lower = query.lower()
    visited = 0

    def collect(element: Atspi.Accessible, depth: int = 0) -> None:
        nonlocal visited
        if depth > 15 or visited >= 1000 or len(matches) >= _DEFAULT_RESULT_LIMIT:
            return
        visited += 1
        info = _extract_info(element, depth)
        if (
            query_lower in info.name.lower()
            or query_lower in info.role.lower()
            or query_lower in info.description.lower()
        ) and action.lower() in {item.lower() for item in info.actions}:
            matches.append((element, info))
        for index in range(info.children_count):
            child = element.get_child_at_index(index)
            if child is not None:
                collect(child, depth + 1)

    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        if app_name and app_name.lower() not in (app.get_name() or "").lower():
            continue
        collect(app)
        if visited >= 1000 or len(matches) >= _DEFAULT_RESULT_LIMIT:
            break

    if not matches:
        return f"No element matching {query!r} exposes action {action!r}"
    if match_index < 0 or match_index >= len(matches):
        return f"match_index must be 0..{len(matches) - 1}"

    element, info = matches[match_index]
    current = _extract_info(element, info.depth)
    if current.name != info.name or current.role != info.role:
        return "Target changed before invocation; action not sent"
    if required_states and not all(state in current.states for state in required_states):
        return "Required target states are absent; action not sent"
    action_iface = element.get_action_iface()
    if action_iface is None:
        return f"Action {action!r} disappeared before invocation"
    for index in range(action_iface.get_n_actions()):
        action_name = action_iface.get_action_name(index) or ""
        if action_name.lower() == action.lower():
            if not action_iface.do_action(index):
                return f"Action {action!r} was rejected"
            result = f"Invoked {action!r} on {info.role} {_quoted_label(info.name)}"
            if verify_states:
                try:
                    after = _extract_info(element, info.depth)
                except Exception:
                    return result + "; action accepted, postcondition unavailable (do not retry)"
                verified = all(state in after.states for state in verify_states)
                return result + (
                    "; requested states verified"
                    if verified
                    else "; action accepted, requested states not verified (do not retry)"
                )
            return result + "; action accepted, effect not verified"
    return f"Action {action!r} disappeared before invocation"


def _extract_info(element: Atspi.Accessible, depth: int) -> ElementInfo:
    """Extract information from an AT-SPI accessible element."""
    role = (element.get_role_name() or "unknown")[:80]
    name = (element.get_name() or "")[:512]
    description = (element.get_description() or "")[:512]

    # Get states
    state_set = element.get_state_set()
    states: list[str] = []
    for state in Atspi.StateType:
        if state_set.contains(state):
            state_name = state.value_nick
            if state_name:
                states.append(state_name)

    # Get position and size
    x, y, width, height = 0, 0, 0, 0
    try:
        component = element.get_component_iface()
        if component is not None:
            rect = component.get_extents(Atspi.CoordType.SCREEN)
            x, y, width, height = rect.x, rect.y, rect.width, rect.height
    except TimeoutError:
        raise
    except Exception:
        pass

    # Get available actions
    actions: list[str] = []
    try:
        action_iface = element.get_action_iface()
        if action_iface is not None:
            for i in range(action_iface.get_n_actions()):
                action_name = action_iface.get_action_name(i)
                if action_name:
                    actions.append(" ".join(action_name.split())[:80])
    except TimeoutError:
        raise
    except Exception:
        pass

    return ElementInfo(
        role=role,
        name=name,
        description=description,
        states=states,
        x=x,
        y=y,
        width=width,
        height=height,
        actions=actions,
        children_count=element.get_child_count(),
        depth=depth,
    )


_WINDOW_ROOT_ROLES = {"frame", "window", "dialog", "alert"}
_WINDOW_LABEL_ROLES = {
    "button",
    "push button",
    "check box",
    "radio button",
    "toggle button",
    "menu item",
    "check menu item",
    "radio menu item",
    "label",
    "heading",
}
_WINDOW_PRIVATE_ROLES = {
    "edit bar",
    "entry",
    "password text",
    "text",
    "terminal",
    "document",
    "document text",
    "document web",
    "document email",
}


def _window_root_size(window: Atspi.Accessible) -> tuple[int, int] | None:
    component = window.get_component_iface()
    if component is None:
        return None
    extent = component.get_extents(Atspi.CoordType.WINDOW)
    if extent.width <= 0 or extent.height <= 0:
        return None
    return int(extent.width), int(extent.height)


def _resolve_registered_root(target: dict[str, Any]) -> Atspi.Accessible | None:
    """Find an exact top-level Qt provider ref, independently checking its bus owner."""
    import dbus
    from dbus.bus import BusConnection

    pid, root_id = target.get("pid"), target.get("wayland_ax_root")
    if type(pid) is not int or pid <= 0 or type(root_id) is not int or not 1 <= root_id < 2**32:
        return None
    path = f"/org/a11y/atspi/accessible/{root_id}"
    session_bus = dbus.SessionBus(private=True)
    a11y_bus = None
    try:
        get_address = dbus.Interface(
            session_bus.get_object("org.a11y.Bus", "/org/a11y/bus", introspect=False),
            "org.a11y.Bus",
        ).GetAddress
        a11y_bus = BusConnection(str(get_address(timeout=1.0)))
        bus_daemon = dbus.Interface(
            a11y_bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False),
            "org.freedesktop.DBus",
        )
        desktop = Atspi.get_desktop(0)
        if desktop is None or not 0 <= desktop.get_child_count() <= MAX_APPLICATIONS:
            return None
        found = []
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                return None
            if app.get_process_id() != pid:
                continue
            if not 0 <= app.get_child_count() <= MAX_TOP_LEVELS:
                return None
            for j in range(app.get_child_count()):
                candidate = app.get_child_at_index(j)
                if candidate is None:
                    return None
                if candidate.path != path or candidate.get_process_id() != pid:
                    continue
                owner = candidate.app.bus_name
                if not isinstance(owner, str) or not owner.startswith(":"):
                    return None
                if int(bus_daemon.GetConnectionUnixProcessID(owner, timeout=1.0)) != pid:
                    return None
                found.append(candidate)
        return found[0] if len(found) == 1 else None
    except (dbus.DBusException, AttributeError, TypeError, ValueError):
        return None
    finally:
        if a11y_bus is not None:
            a11y_bus.close()
        session_bus.close()


def _resolve_exact_window(target: dict[str, Any]) -> tuple[Atspi.Accessible, str] | None:
    if "wayland_ax_root" in target:
        registered = _resolve_registered_root(target)
        return (registered, "compositor_registered_qt_root") if registered is not None else None
    pid, title = target.get("pid"), target.get("caption")
    if type(pid) is not int or pid <= 0 or not isinstance(title, str) or not 0 < len(title) <= 2048:
        return None
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        return None
    n_apps = desktop.get_child_count()
    if not 0 <= n_apps <= MAX_APPLICATIONS:
        return None
    apps = []
    for i in range(n_apps):
        app = desktop.get_child_at_index(i)
        if app is None:
            return None
        if app.get_process_id() == pid:
            apps.append(app)
    if len(apps) != 1:
        return None
    app = apps[0]
    n_windows = app.get_child_count()
    if not 0 <= n_windows <= MAX_TOP_LEVELS:
        return None
    candidates = []
    for j in range(n_windows):
        window = app.get_child_at_index(j)
        if window is None:
            return None
        if (
            (window.get_role_name() or "").casefold() in _WINDOW_ROOT_ROLES
            and window.get_name() == title
            and window.get_process_id() == pid
        ):
            candidates.append(window)
    peers = target.get("peers")
    if not isinstance(peers, list) or len(candidates) != len(peers):
        return None
    toolkit = ""
    sizes: list[tuple[int, int] | None] = [None] * len(candidates)
    if len(candidates) > 1:
        name = (app.get_toolkit_name() or "").casefold()
        version = app.get_toolkit_version() or ""
        if name in {"gtk", "gtk+"} and version.startswith("3."):
            toolkit = "gtk3"
            sizes = [_window_root_size(w) for w in candidates]
    result = match_window_peer(target, sizes, toolkit)
    return (candidates[result[0]], result[1]) if result is not None else None


def _snapshot_window_node(
    node: Atspi.Accessible,
    *,
    depth: int,
    max_depth: int,
    budget: TraversalBudget,
    include_labels: bool,
    action_indices: tuple[int, ...] | None = None,
) -> dict:
    budget.visited += 1
    role = (node.get_role_name() or "unknown").casefold()[:64]
    state_set = node.get_state_set()
    states = sorted(state.value_nick for state in Atspi.StateType if state_set.contains(state))
    meta: dict[str, Any] = {
        "index": budget.visited,
        "role": role,
        "states": states,
        "children": [],
    }
    if action_indices:
        _attach_semantic_ref(node, role, set(states), action_indices, meta)
    # No values, text interfaces or descriptions. Omit the entire subtree of
    # editable/secret/document nodes; names may themselves contain contents.
    if role in _WINDOW_PRIVATE_ROLES or "editable" in states or "protected" in states:
        meta["redacted"] = True
        return meta
    if include_labels and depth > 0 and role in _WINDOW_LABEL_ROLES:
        name = node.get_name()
        if name:
            meta["label"] = name[:120]
    children = node.get_child_count()
    if children < 0:
        raise RuntimeError("AT-SPI window subtree became unavailable")
    if depth >= max_depth:
        budget.truncated |= children > 0
        return meta
    for i in range(children):
        if budget.visited >= budget.max_nodes:
            budget.truncated = True
            break
        child = node.get_child_at_index(i)
        if child is None:
            raise RuntimeError("AT-SPI window subtree changed during collection")
        meta["children"].append(
            _snapshot_window_node(
                child,
                depth=depth + 1,
                max_depth=max_depth,
                budget=budget,
                include_labels=include_labels,
                action_indices=(*action_indices, i) if action_indices is not None else None,
            )
        )
    return meta


def get_window_accessibility(
    target: dict[str, Any],
    *,
    max_depth: int,
    max_nodes: int,
    include_labels: bool,
    include_action_handles: bool = False,
) -> dict:
    """Resolve an exact accessible top-level and inspect only its read-only subtree."""
    match = _resolve_exact_window(target)
    if match is None:
        return {"status": "unsupported", "reason": "Exact accessible window could not be proven"}
    window, method = match
    budget = TraversalBudget(max_nodes)
    tree = _snapshot_window_node(
        window,
        depth=0,
        max_depth=max_depth,
        budget=budget,
        include_labels=include_labels,
        action_indices=()
        if include_action_handles and method == "compositor_registered_qt_root"
        else None,
    )
    afterward = _resolve_exact_window(target)
    if afterward is None or afterward[0] != window or afterward[1] != method:
        return {"status": "unsupported", "reason": "Accessible window identity changed"}
    return {
        "status": "ok",
        "window_id": target["window_id"],
        "match": method,
        "labels_included": include_labels,
        "truncated": budget.truncated,
        "tree": tree,
    }


def _semantic_state(node) -> tuple[str, set[str]]:
    node.clear_cache()
    role = (node.get_role_name() or "").casefold()
    states = node.get_state_set()
    return role, {state.value_nick for state in Atspi.StateType if states.contains(state)}


def _semantic_ref(node, role, indices):
    return {
        "root_path_indices": list(indices),
        "path": node.path,
        "owner": node.app.bus_name,
        "role": role,
    }


def _eligible_semantic_entry(node, role, states) -> bool:
    if (
        role not in ENTRY_ROLES
        or "editable" not in states
        or "multi-line" in states
        or states & BLOCKED_STATES
    ):
        return False
    if "single-line" in states:
        return True
    # Qt 6.11's Linux bridge does not export the SingleLine state. A cooperating
    # app can explicitly opt in this exact field instead of guessing its class.
    return node.get_accessible_id() == "jamesos.ax.single-line.v1"


def _attach_semantic_ref(node, role, states, indices, meta):
    if role in PRIVATE_ANCESTORS or states & BLOCKED_STATES:
        return
    allowed = []
    if role in BUTTON_ROLES:
        allowed.append("press")
    if _eligible_semantic_entry(node, role, states):
        allowed.append("set_text")
    if not allowed and (role in _WINDOW_PRIVATE_ROLES or "editable" in states):
        return
    descriptor = _semantic_ref(node, role, indices)
    if isinstance(descriptor["path"], str) and isinstance(descriptor["owner"], str):
        meta["_action_ref"] = {**descriptor, "actions": allowed}


def _find_bound_node(root, ref):
    if not isinstance(ref, dict):
        return None
    indices = ref.get("root_path_indices")
    if not isinstance(indices, list) or not 1 <= len(indices) <= 12:
        return None
    owner = root.app.bus_name
    if ref.get("owner") != owner:
        return None
    node = root
    for index in indices:
        role, states = _semantic_state(node)
        if (
            role in PRIVATE_ANCESTORS
            or role in ENTRY_ROLES
            or "editable" in states
            or states & BLOCKED_STATES
            or type(index) is not int
            or not 0 <= index < min(node.get_child_count(), 1000)
        ):
            return None
        parent, node = node, node.get_child_at_index(index)
        if node is None or node.app.bus_name != owner or node.get_parent() != parent:
            return None
    role, states = _semantic_state(node)
    if (
        node.path != ref.get("path")
        or role != ref.get("role")
        or role in PRIVATE_ANCESTORS
        or states & BLOCKED_STATES
    ):
        return None
    return node, role, states


def _private_stage_unlocked() -> bool:
    # A disposable compositor normally has no locker service. If it does, an
    # active or unverifiable locker blocks semantic dispatch.
    import os

    import dbus

    from kwin_mcp.ax_link import explicit_test_runtime_requested

    if not explicit_test_runtime_requested(dict(os.environ)):
        return False
    try:
        bus = dbus.SessionBus(private=True)
        try:
            if not bus.name_has_owner("org.freedesktop.ScreenSaver"):
                return True
            owner = bus.get_name_owner("org.freedesktop.ScreenSaver")
            saver = dbus.Interface(
                bus.get_object(owner, "/ScreenSaver", introspect=False),
                "org.freedesktop.ScreenSaver",
            )
            return not bool(saver.GetActive(timeout=0.5))
        finally:
            bus.close()
    except (dbus.DBusException, OSError):
        return False


def run_window_semantic_action(request: dict) -> dict:
    def refused(reason: str) -> dict:
        return report("refused", False, reason)

    action, text = request.get("action"), request.get("text")
    state, present = request.get("verify_state"), request.get("verify_present")
    verify = request.get("verify_ref")
    target = request.get("target")
    if (
        not valid_request(
            action, text, "approved" if isinstance(verify, dict) else None, state, present
        )
        or not isinstance(target, dict)
        or not _private_stage_unlocked()
    ):
        return refused("Staging, lock, or action eligibility could not be established")
    root = _resolve_registered_root(target)
    if root is None:
        return refused("Compositor-registered accessibility root is unavailable")
    bound = _find_bound_node(root, request.get("element_ref"))
    if bound is None:
        return refused("Snapshot element identity changed")
    node, role, states = bound
    if not {"enabled", "sensitive", "showing", "visible"}.issubset(states):
        return refused("Snapshot element is not enabled and visible")

    if action == "press":
        observer = _find_bound_node(root, verify)
        if observer is None or ("editable" in observer[2] or observer[1] in _WINDOW_PRIVATE_ROLES):
            return refused("Observable verification target changed or is private")
        if (state in observer[2]) is present:
            return refused("Verification condition was already satisfied before dispatch")
        interface = node.get_action_iface() if role in BUTTON_ROLES else None
        if interface is None or not 0 <= interface.get_n_actions() <= 12:
            return refused("No supported button action")
        # Qt normalizes these action names to press/toggle. No name, index,
        # focus, coordinates or generic invoke fallback from the caller.
        candidates = [
            i
            for i in range(interface.get_n_actions())
            if interface.get_action_name(i).casefold() in {"press", "click", "toggle"}
        ]
        if len(candidates) != 1:
            return refused("No unique semantic button action")

        def dispatch():
            return interface.do_action(candidates[0])

        def observed():
            got = _find_bound_node(root, verify)
            return got is not None and (state in got[2]) is present
    else:
        if not isinstance(text, str) or not _eligible_semantic_entry(node, role, states):
            return refused("Only non-private single-line edit fields are supported")
        interface = node.get_editable_text_iface()
        if interface is None or node.get_text_iface() is None or node.get_character_count() != 0:
            return refused("Only initially empty edit fields are supported")

        def dispatch():
            return interface.set_text_contents(text)

        def observed():
            got = _find_bound_node(root, request.get("element_ref"))
            if got is None or not _eligible_semantic_entry(*got):
                return False
            candidate = got[0]
            return (
                candidate.get_character_count() in {len(text), len(text.encode("utf-16-le")) // 2}
                and Atspi.Text.get_text(candidate, 0, -1) == text
            )

    import os

    from kwin_mcp.windows import run_window_operation

    def compositor_binding_matches() -> bool:
        try:
            return (
                run_window_operation(
                    "atspi_explicit_target",
                    window_id=target["window_id"],
                    env=dict(os.environ),
                    timeout=2,
                )
                == target
            )
        except (RuntimeError, ValueError, OSError, KeyError):
            return False

    if (
        not compositor_binding_matches()
        or not _private_stage_unlocked()
        or _resolve_registered_root(target) != root
    ):
        return refused("Selected root or unlocked staging session changed before dispatch")
    latest = _find_bound_node(root, request.get("element_ref"))
    if latest is None or latest[0] != node or latest[2] != states or not _private_stage_unlocked():
        return refused("Snapshot target changed before dispatch")
    if action == "press":
        prior = _find_bound_node(root, verify)
        if prior is None or (state in prior[2]) is present:
            return refused("Verification condition changed before dispatch")
    elif not _eligible_semantic_entry(*latest) or node.get_character_count() != 0:
        return refused("Edit field changed before dispatch")
    did_send = None
    try:
        accepted = dispatch()
        if not accepted:
            return report("unverified", None, "Provider did not confirm dispatch")
        did_send = True
        for _ in range(6):
            if not _private_stage_unlocked() or _resolve_registered_root(target) != root:
                break
            if observed() and compositor_binding_matches() and _private_stage_unlocked():
                return report("verified", True, "Effect observed in the registered root")
            time.sleep(0.05)
    except Exception:
        pass
    return report("unverified", did_send, "Effect could not be verified; inspect without retrying")


# ── CLI entrypoint for subprocess execution ──────────────────────────────


def _handle_request(request: dict) -> dict:
    """Dispatch a JSON request to the appropriate function."""
    op = request.get("op", "")

    if op == "tree":
        result = get_accessibility_tree(
            app_name=request.get("app_name", ""),
            max_depth=request.get("max_depth", _DEFAULT_MAX_DEPTH),
            role=request.get("role", ""),
            max_nodes=request.get("max_nodes", _DEFAULT_MAX_NODES),
        )
        return {"ok": True, "result": result}

    if op == "find":
        elements = find_elements(
            query=request.get("query", ""),
            app_name=request.get("app_name", ""),
            states=request.get("states"),
            limit=request.get("limit", _DEFAULT_RESULT_LIMIT),
        )
        return {"ok": True, "result": [asdict(e) for e in elements]}

    if op == "wait":
        try:
            elements = wait_for_elements(
                query=request.get("query", ""),
                app_name=request.get("app_name", ""),
                timeout_ms=request.get("timeout_ms", 5000),
                poll_interval_ms=request.get("poll_interval_ms", 200),
                states=request.get("states"),
                limit=request.get("limit", _DEFAULT_RESULT_LIMIT),
            )
        except TimeoutError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": [asdict(e) for e in elements]}

    if op == "window_semantic_action":
        return {"ok": True, "result": json.dumps(run_window_semantic_action(request))}

    if op == "window_accessibility":
        target = request.get("target")
        max_depth = request.get("max_depth")
        max_nodes = request.get("max_nodes")
        include_labels = request.get("include_labels")
        include_handles = request.get("include_action_handles", False)
        if (
            not isinstance(target, dict)
            or type(max_depth) is not int
            or not 0 <= max_depth <= 12
            or type(max_nodes) is not int
            or not 1 <= max_nodes <= 200
            or type(include_labels) is not bool
            or type(include_handles) is not bool
        ):
            return {"ok": False, "error": "Invalid bounded window accessibility request"}
        result = get_window_accessibility(
            target,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_labels=include_labels,
            include_action_handles=include_handles,
        )
        return {"ok": True, "result": json.dumps(result)}

    if op == "list_windows":
        return {"ok": True, "result": list_windows()}

    if op == "focus_window":
        result = focus_window(
            app_name=request.get("app_name", ""), window_title=request.get("window_title", "")
        )
        return {"ok": True, "result": result}

    if op == "invoke_action":
        result = invoke_action(
            query=request.get("query", ""),
            action=request.get("action", "click"),
            app_name=request.get("app_name", ""),
            match_index=request.get("match_index", 0),
            required_states=request.get("required_states"),
            verify_states=request.get("verify_states"),
        )
        return {"ok": True, "result": result}

    return {"ok": False, "error": f"Unknown operation: {op}"}


def _execute_request(req: object) -> dict:
    """Apply worker-local deadlines; call only in the disposable main process."""

    # This process is disposable: bound native D-Bus calls as well as Python
    # traversal. The engine also enforces an outer subprocess deadline because
    # Python signals can be delayed while a native library call is blocked.
    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError("AT-SPI request deadline exceeded")

    if not isinstance(req, dict):
        return {"ok": False, "error": "Request must be a JSON object"}

    request = cast("dict[str, Any]", req)
    try:
        timeout_ms = int(request.get("request_timeout_ms", 10000))
        if not 1 <= timeout_ms <= 120000:
            raise ValueError("request_timeout_ms must be between 1 and 120000")
        Atspi.set_timeout(min(timeout_ms, 1000), min(timeout_ms, 1000))
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000)
        resp = _handle_request(request)
    except Exception as exc:
        error = str(exc)
        if request.get("op") in ("invoke_action", "focus_window"):
            error += "; mutation outcome unknown; inspect current state before retrying"
        resp = {"ok": False, "error": error}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return resp


if __name__ == "__main__":
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"Invalid JSON: {exc}"}, sys.stdout)
        sys.exit(1)

    json.dump(_execute_request(req), sys.stdout)
