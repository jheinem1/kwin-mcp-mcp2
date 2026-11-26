import unittest

from kwin_mcp.ax_link import explicit_test_runtime_requested, root_from_binding

WIN = "35b34383-4354-481e-b227-a4956e1a2e01"


class TestExplicitAxLink(unittest.TestCase):
    def test_invalid_root(self):
        for value in [True, 0, -1, 2**32, "2147483650", None]:
            with self.subTest(value=value):
                binding = {"window_id": WIN, "pid": 5, "root": value}
                self.assertIsNone(root_from_binding(binding, window_id=WIN, pid=5))

    def test_exact_uuid_pid_and_32bit_root(self):
        good = {"window_id": WIN, "pid": 5, "root": 2147483650}
        self.assertEqual(root_from_binding(good, window_id=WIN, pid=5), 2147483650)
        self.assertIsNone(root_from_binding(good, window_id=WIN, pid=6))
        self.assertIsNone(
            root_from_binding(dict(good, window_id="not-canonical"), window_id=WIN, pid=5)
        )

    def test_stage_must_be_explicit(self):
        key = "XDG_RUNTIME_DIR"
        path = "/tmp/kax-abcdef/r"
        env = {key: path, "AXLINK_PRIVATE_RUNTIME": path, "KWIN_MCP_EXPLICIT_AX_TEST": "1"}
        self.assertTrue(explicit_test_runtime_requested(env))
        self.assertFalse(
            explicit_test_runtime_requested(dict(env, XDG_RUNTIME_DIR="/run/user/1000"))
        )
        self.assertFalse(explicit_test_runtime_requested(dict(env, KWIN_MCP_EXPLICIT_AX_TEST="0")))
