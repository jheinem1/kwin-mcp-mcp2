import pytest

from kwin_mcp.ax_link import explicit_test_runtime_requested, root_from_binding

WIN = "35b34383-4354-481e-b227-a4956e1a2e01"

@pytest.mark.parametrize("value", [True, 0, -1, 2**32, "2147483650", None])
def test_invalid_root(value):
    binding = {"window_id": WIN, "pid": 5, "root": value}
    assert root_from_binding(binding, window_id=WIN, pid=5) is None


def test_exact_uuid_pid_and_32bit_root():
    good = {"window_id": WIN, "pid": 5, "root": 2147483650}
    assert root_from_binding(good, window_id=WIN, pid=5) == 2147483650
    assert root_from_binding(good, window_id=WIN, pid=6) is None
    assert root_from_binding(dict(good, window_id="not-canonical"), window_id=WIN, pid=5) is None


def test_stage_must_be_explicit():
    key = "XDG_RUNTIME_DIR"
    path = "/tmp/kax-abcdef/r"
    env = {key: path, "AXLINK_PRIVATE_RUNTIME": path, "KWIN_MCP_EXPLICIT_AX_TEST": "1"}
    assert explicit_test_runtime_requested(env)
    assert not explicit_test_runtime_requested(dict(env, XDG_RUNTIME_DIR="/run/user/1000"))
    assert not explicit_test_runtime_requested(dict(env, KWIN_MCP_EXPLICIT_AX_TEST="0"))
