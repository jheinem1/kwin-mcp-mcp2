"""AT-SPI2 accessibility tree reader.

Can be run as a subprocess CLI for isolated D-Bus session support.
Reads a JSON request from stdin and writes a JSON response to stdout.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

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


def focus_window(app_name: str) -> str:
    """Focus a window by application name.

    Args:
        app_name: Application name substring (case-insensitive).

    Returns:
        Result message.
    """
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        name = app.get_name() or ""
        if app_name.lower() in name.lower():
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                try:
                    component = win.get_component_iface()
                    if component is not None:
                        component.grab_focus()
                        return f"Focused: {_quoted_label(name)}"
                except Exception:
                    continue
            return f"Found {_quoted_label(name)} but could not focus it"
    return f"No application matching '{app_name}' found"


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

        time.sleep(interval)


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


def invoke_action(query: str, action: str, app_name: str = "", match_index: int = 0) -> str:
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
    action_iface = element.get_action_iface()
    if action_iface is None:
        return f"Action {action!r} disappeared before invocation"
    for index in range(action_iface.get_n_actions()):
        action_name = action_iface.get_action_name(index) or ""
        if action_name.lower() == action.lower():
            if not action_iface.do_action(index):
                return f"Action {action!r} was rejected"
            return f"Invoked {action!r} on {info.role} {_quoted_label(info.name)}"
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

    if op == "list_windows":
        return {"ok": True, "result": list_windows()}

    if op == "focus_window":
        result = focus_window(app_name=request.get("app_name", ""))
        return {"ok": True, "result": result}

    if op == "invoke_action":
        result = invoke_action(
            query=request.get("query", ""),
            action=request.get("action", "click"),
            app_name=request.get("app_name", ""),
            match_index=request.get("match_index", 0),
        )
        return {"ok": True, "result": result}

    return {"ok": False, "error": f"Unknown operation: {op}"}


if __name__ == "__main__":
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"Invalid JSON: {exc}"}, sys.stdout)
        sys.exit(1)

    resp = _handle_request(req)
    json.dump(resp, sys.stdout)
