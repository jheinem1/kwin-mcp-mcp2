"""Execute bounded, exact-window accessibility steps, never arbitrary scripts."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_STEPS = 5
MAX_SECONDS = 30.0
_MAX_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_WRITE_HANDLE = re.compile(r"[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{24}\Z")
_SUPPORTED_STATES = frozenset(
    {"checked", "expanded", "indeterminate", "pressed", "selected", "showing", "visible"}
)
_EXACT_MATCH = "compositor_registered_qt_root"


@dataclass(frozen=True)
class Step:
    op: str
    key: str
    text: str | None = None
    verify_key: str | None = None
    state: str | None = None
    present: bool | None = None


def _key(value: object) -> str:
    if not isinstance(value, str) or not _MAX_KEY.fullmatch(value):
        raise ValueError("Expected an opaque key from an exact-window action snapshot")
    return value


def _handle(value: object) -> str:
    if not isinstance(value, str) or not _WRITE_HANDLE.fullmatch(value):
        raise ValueError("Expected a one-shot handle from the fresh snapshot")
    return value


def parse_steps(values: object) -> list[Step]:
    """Reject the whole plan on invalid syntax before reading or acting."""
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_STEPS:
        raise ValueError(f"Expected 1 to {MAX_STEPS} scoped window steps")
    steps: list[Step] = []
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("Each step must be an object")
        data = cast("dict[str, Any]", raw)
        op = data.get("op")
        key = _key(data.get("key"))
        if op == "set_text" and set(data) == {"op", "key", "text"}:
            text = data["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 256:
                raise ValueError("set_text expects 1 to 256 characters")
            if any(ord(c) < 32 or ord(c) == 127 or c in "\u0085\u2028\u2029" for c in text):
                raise ValueError("set_text accepts a single printable line")
            steps.append(Step(op, key, text=text))
            continue
        if op == "press" and set(data) == {"op", "key", "verify_key", "state", "present"}:
            verify_key = _key(data["verify_key"])
        elif op == "assert_state" and set(data) == {"op", "key", "state", "present"}:
            verify_key = key
        else:
            raise ValueError("Unsupported scoped step or unexpected fields")
        state, present = data["state"], data["present"]
        if (
            not isinstance(state, str)
            or state not in _SUPPORTED_STATES
            or type(present) is not bool
        ):
            raise ValueError("Expected a supported accessibility state and a boolean")
        steps.append(Step(op, key, verify_key=verify_key, state=state, present=present))
    return steps


def _snapshot_nodes(snapshot: object, window_id: str) -> dict[str, dict]:
    if not isinstance(snapshot, dict):
        raise ValueError("No exact accessibility snapshot for the intended window")
    payload = cast("dict[str, Any]", snapshot)
    if (
        payload.get("status") != "ok"
        or payload.get("match") != _EXACT_MATCH
        or payload.get("window_id") != window_id
        or not isinstance(payload.get("tree"), dict)
    ):
        raise ValueError("No exact accessibility snapshot for the intended window")
    stack: list[object] = [payload["tree"]]
    seen: set[int] = set()
    matches: dict[str, dict] = {}
    while stack:
        raw = stack.pop()
        if not isinstance(raw, dict) or id(raw) in seen or len(seen) >= 200:
            raise ValueError("Unverifiable accessibility snapshot shape")
        seen.add(id(raw))
        node = cast("dict[str, Any]", raw)
        children = node.get("children")
        if not isinstance(children, list) or len(children) > 200:
            raise ValueError("Unverifiable accessibility snapshot children")
        stack.extend(children)
        # Eligible non-secret entries are still redacted in the snapshot to
        # hide their contents. Only the primitive may read/verify those values.
        if "action_key" not in node:
            continue
        if node.get("redacted"):
            states, actions = node.get("states"), node.get("semantic_actions")
            if (
                node.get("role") not in {"text", "entry"}
                or not isinstance(states, list)
                or "editable" not in states
                or "protected" in states
                or not isinstance(actions, list)
                or "set_text" not in actions
            ):
                continue
        key = _key(node["action_key"])
        if key in matches:
            raise ValueError("Duplicate element key")
        matches[key] = node
    return matches


def run_window_steps(
    window_id: str,
    plan: object,
    *,
    snapshot: Callable[[str], dict[str, Any]],
    action: Callable[..., dict[str, Any]],
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Bind each step to a fresh snapshot and stop on the first uncertainty."""
    window_id = str(uuid.UUID(window_id))
    steps = parse_steps(plan)
    deadline = monotonic() + MAX_SECONDS
    receipts: list[dict] = []

    def finish(status: str) -> dict:
        return {
            "status": status,
            "window_id": window_id,
            "steps_completed": sum(item["status"] == "verified" for item in receipts),
            "steps_total": len(steps),
            "steps": receipts,
        }

    for n, step in enumerate(steps, 1):
        entry: dict[str, Any] = {"step": n, "op": step.op, "dispatched": False}
        receipts.append(entry)
        if monotonic() >= deadline:
            entry.update(status="refused", reason="batch_deadline")
            return finish("stopped")
        try:
            nodes = _snapshot_nodes(snapshot(window_id), window_id)
            node = nodes.get(step.key)
            if node is None:
                raise ValueError("Requested element key is absent")
            if step.op == "assert_state":
                states = node.get("states")
                if not isinstance(states, list) or (step.state in states) is not step.present:
                    entry.update(status="refused", reason="unexpected_state")
                    return finish("stopped")
                entry.update(status="verified")
                continue
            handle = _handle(node.get("action_handle"))
            kwargs: dict[str, Any] = {}
            if step.op == "set_text":
                kwargs["text"] = step.text
            else:
                verify_key = step.verify_key
                if verify_key is None or verify_key not in nodes:
                    raise ValueError("Verification element key is absent")
                verify = nodes[verify_key]
                kwargs.update(
                    verify_handle=_handle(verify.get("action_handle")),
                    verify_state=step.state,
                    verify_present=step.present,
                )
        except Exception:
            entry.update(status="refused", reason="fresh_binding_unavailable")
            return finish("stopped")
        if monotonic() >= deadline:
            entry.update(status="refused", reason="batch_deadline")
            return finish("stopped")
        try:
            result = action(window_id, handle, step.op, **kwargs)
        except Exception:
            entry.update(status="unverified", dispatched=None, reason="dispatch_outcome_unknown")
            return finish("stopped")
        if not isinstance(result, dict):
            entry.update(status="unverified", dispatched=None, reason="dispatch_outcome_unknown")
            return finish("stopped")
        sent = result.get("dispatched")
        if result.get("status") == "verified" and result.get("verified") is True and sent is True:
            entry.update(status="verified", dispatched=True)
        elif result.get("status") == "refused" and sent is False:
            entry.update(status="refused", reason="primitive_refused")
            return finish("stopped")
        else:
            entry.update(
                status="unverified",
                dispatched=sent if type(sent) is bool else None,
                reason="primitive_not_verified",
            )
            return finish("stopped")
    return finish("verified")
