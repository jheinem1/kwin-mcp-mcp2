from __future__ import annotations

import unittest
from unittest import mock

from kwin_mcp.session import (
    Session,
    SessionConfig,
    _find_at_spi_bus_launcher,
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


if __name__ == "__main__":
    unittest.main()
