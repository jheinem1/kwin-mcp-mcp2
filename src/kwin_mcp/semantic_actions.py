"""Internal authorization for bounded, one-shot, staged semantic actions."""

from __future__ import annotations

import hmac
import json
import secrets
import time
import unicodedata
from typing import Any

TTL_SECONDS = 30
MAX_SNAPSHOTS = 4
OBSERVABLE_STATES = frozenset({"checked", "expanded", "selected", "enabled"})
BUTTON_ROLES = frozenset({"push button", "button", "toggle button", "check box", "radio button"})
ENTRY_ROLES = frozenset({"text", "entry"})
BLOCKED_STATES = frozenset({"protected", "defunct", "stale"})
PRIVATE_ANCESTORS = frozenset(
    {
        "document",
        "document text",
        "document web",
        "document email",
        "terminal",
        "password text",
        "editable text",
        "edit bar",
    }
)


def report(status: str, dispatched: bool | None, reason: str) -> dict:
    return {
        "status": status,
        "dispatched": dispatched,
        "verified": status == "verified",
        "reason": reason,
    }


def valid_text(text: object) -> bool:
    return (
        isinstance(text, str)
        and 1 <= len(text) <= 256
        and all(unicodedata.category(c)[0] != "C" and c not in "\u2028\u2029" for c in text)
    )


def valid_request(action, text, verify_handle, verify_state, verify_present) -> bool:
    if action == "press":
        return (
            text is None
            and isinstance(verify_handle, str)
            and 1 <= len(verify_handle) <= 100
            and isinstance(verify_state, str)
            and verify_state in OBSERVABLE_STATES
            and type(verify_present) is bool
        )
    return (
        action == "set_text"
        and valid_text(text)
        and verify_handle is None
        and verify_state is None
        and verify_present is None
    )


class SnapshotHandles:
    """Opaque in-process handles, valid for one invocation of one bounded snapshot."""

    def __init__(self) -> None:
        self._session: object | None = None
        self._key = secrets.token_bytes(32)
        self._snapshots: dict[str, tuple[float, object, dict, dict[str, dict]]] = {}

    def _prune(self) -> None:
        for key, (expiry, _, _, _) in tuple(self._snapshots.items()):
            if expiry <= time.monotonic():
                del self._snapshots[key]

    def clear(self) -> None:
        self._snapshots.clear()
        self._session = None
        self._key = secrets.token_bytes(32)

    def issue(self, tree: dict, session: object, target: dict) -> bool:
        if session is not self._session:
            self.clear()
            self._session = session
        self._prune()
        key = secrets.token_urlsafe(16)
        records: dict[str, dict] = {}

        def visit(node: dict[str, Any]) -> None:
            descriptor = node.pop("_action_ref", None)
            if isinstance(descriptor, dict):
                token = f"{key}.{secrets.token_urlsafe(18)}"
                records[token] = descriptor
                node["action_handle"] = token
                fingerprint = [target, {k: v for k, v in descriptor.items() if k != "actions"}]
                node["action_key"] = hmac.digest(
                    self._key, json.dumps(fingerprint, sort_keys=True).encode(), "sha256"
                ).hex()
                node["semantic_actions"] = descriptor.get("actions", [])
            for child in node.get("children", []):
                visit(child)

        visit(tree)
        if not records:
            return False
        if len(self._snapshots) >= MAX_SNAPSHOTS:
            del self._snapshots[next(iter(self._snapshots))]
        self._snapshots[key] = (time.monotonic() + TTL_SECONDS, session, target, records)
        return True

    def take(self, token: str, verify_token: str | None, session: object, window_id: str):
        self._prune()
        if not isinstance(token, str) or not 1 <= len(token) <= 100:
            return None
        key = token.partition(".")[0]
        stored = self._snapshots.get(key)
        if stored is None:
            return None
        _expiry, selected, target, records = stored
        if (
            selected is not session
            or target.get("window_id") != window_id
            or token not in records
            or (verify_token is not None and verify_token not in records)
        ):
            return None
        del self._snapshots[key]
        return target, records[token], records.get(verify_token) if verify_token else None
