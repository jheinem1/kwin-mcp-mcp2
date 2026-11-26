"""Staging-only transport for a compositor-authoritative Qt AT-SPI root."""

from __future__ import annotations

import re
import uuid

_TEST_RUNTIME = re.compile(r"/tmp/kax-[A-Za-z0-9]{6}/r\Z")


def explicit_test_runtime_requested(env: dict[str, str]) -> bool:
    """Require explicit staging opt-in; never enable this on the live socket."""
    return (
        env.get("KWIN_MCP_EXPLICIT_AX_TEST") == "1"
        and env.get("XDG_RUNTIME_DIR") == env.get("AXLINK_PRIVATE_RUNTIME")
        and bool(_TEST_RUNTIME.fullmatch(env.get("XDG_RUNTIME_DIR", "")))
    )


def root_from_binding(binding: dict, *, window_id: str, pid: int) -> int | None:
    expected = str(uuid.UUID(window_id))
    value = binding.get("root")
    registered_pid = binding.get("pid")
    if (
        binding.get("window_id") != expected
        or type(registered_pid) is not int
        or registered_pid <= 0
        or registered_pid != pid
        or type(value) is not int
        or not 1 <= value <= (2**32 - 1)
    ):
        return None
    return value
