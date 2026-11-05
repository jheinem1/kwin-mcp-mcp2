from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kwin_mcp.session import (
    Session,
    SessionConfig,
    _find_at_spi_bus_launcher,
    _remove_tree_stably,
    _sanitized_environment,
)


class AtSpiLauncherTests(unittest.TestCase):
    def test_prefers_path_lookup(self) -> None:
        with mock.patch("kwin_mcp.session.shutil.which", return_value="/custom/launcher"):
            self.assertEqual(_find_at_spi_bus_launcher(), "/custom/launcher")

    def test_fedora_libexec_path_is_supported(self) -> None:
        def is_file(path: object) -> bool:
            return str(path) == "/usr/libexec/at-spi-bus-launcher"

        with (
            mock.patch("kwin_mcp.session.shutil.which", return_value=None),
            mock.patch("kwin_mcp.session.Path.is_file", autospec=True, side_effect=is_file),
        ):
            self.assertEqual(_find_at_spi_bus_launcher(), "/usr/libexec/at-spi-bus-launcher")

    def test_missing_launcher_has_actionable_error(self) -> None:
        with (
            mock.patch("kwin_mcp.session.shutil.which", return_value=None),
            mock.patch("kwin_mcp.session.Path.is_file", return_value=False),
            self.assertRaisesRegex(RuntimeError, "install at-spi2-core"),
        ):
            _find_at_spi_bus_launcher()


class WrapperTests(unittest.TestCase):
    def test_wrapper_checks_kwin_liveness_and_timeout(self) -> None:
        script = Session()._build_wrapper_script(
            SessionConfig(), "/usr/libexec/at-spi-bus-launcher"
        )
        self.assertIn('kill -0 "$KWIN_PID"', script)
        self.assertIn('if [ "$attempt" -ge 150 ]', script)
        self.assertIn("/usr/libexec/at-spi-bus-launcher --launch-immediately", script)


class EnvironmentTests(unittest.TestCase):
    def test_ambient_credentials_are_not_inherited(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "GITHUB_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
        }
        with mock.patch.dict("kwin_mcp.session.os.environ", env, clear=True):
            sanitized = _sanitized_environment()
        self.assertEqual(sanitized["PATH"], "/usr/bin")
        self.assertEqual(sanitized["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertNotIn("GITHUB_TOKEN", sanitized)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", sanitized)


class StartupTests(unittest.TestCase):
    def test_partial_line_does_not_defeat_deadline(self) -> None:
        session = Session()
        session._process = subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.write(1,b'partial'); time.sleep(5)"],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "No READY"):
                session._read_startup_status(0.1)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            session.stop()

    def test_multiple_lines_in_one_write_are_consumed(self) -> None:
        session = Session()
        session._process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,time; "
                "os.write(1,b'DBUS_SESSION_BUS_ADDRESS=unix:test\\nREADY\\n'); time.sleep(5)",
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self.assertEqual(session._read_startup_status(1), "unix:test")
        finally:
            session.stop()

    def test_invalid_socket_name_rejected_before_spawning(self) -> None:
        with self.assertRaisesRegex(ValueError, "simple filename"):
            Session().start(SessionConfig(socket_name="../../host"))

    def test_missing_dependency_is_actionable(self) -> None:
        with (
            mock.patch("kwin_mcp.session.shutil.which", return_value=None),
            self.assertRaisesRegex(RuntimeError, "Required executable not found: dbus-run-session"),
        ):
            Session().start()

    def test_existing_socket_is_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            socket = Path(runtime) / "existing"
            socket.write_text("owned by another session")
            with (
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}),
                mock.patch("kwin_mcp.session.shutil.which", return_value="/bin/true"),
                mock.patch("kwin_mcp.session.subprocess.Popen") as popen,
                self.assertRaisesRegex(RuntimeError, "socket already exists"),
            ):
                Session().start(SessionConfig(socket_name="existing"))
            popen.assert_not_called()
            self.assertEqual(socket.read_text(), "owned by another session")

    def test_spawn_failure_cleans_resources_and_allows_retry(self) -> None:
        session = Session()
        with (
            tempfile.TemporaryDirectory() as runtime,
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}),
            mock.patch("kwin_mcp.session.shutil.which", return_value="/bin/true"),
            mock.patch("kwin_mcp.session.subprocess.Popen", side_effect=OSError("spawn failed")),
            mock.patch(
                "kwin_mcp.session._remove_tree_stably", wraps=_remove_tree_stably
            ) as cleanup,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                    session.start(SessionConfig(isolate_home=True))
                self.assertIsNone(session._home_dir)
                self.assertIsNone(session._startup_log)
                self.assertFalse(session.is_running)
            self.assertEqual(cleanup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
