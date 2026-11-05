"""KWin Wayland session management.

Manages the lifecycle of KWin Wayland sessions:
- Virtual sessions: isolated via dbus-run-session + kwin_wayland --virtual
- Live sessions: connecting to an existing KWin compositor (real desktop or container)
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO

_STARTUP_TIMEOUT_SECONDS = 15.0
_AT_SPI_LAUNCHER_CANDIDATES = (
    "/usr/libexec/at-spi-bus-launcher",  # Fedora/Bazzite
    "/usr/lib/at-spi-bus-launcher",  # Arch/Debian variants
)
_SAFE_ENV_NAMES = {
    "HOME",
    "LANG",
    "LANGUAGE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TZ",
    "USER",
}
_SAFE_ENV_PREFIXES = ("GTK_", "KDE_", "LC_", "QT_", "XDG_")


def _sanitized_environment() -> dict[str, str]:
    """Copy desktop/runtime settings without ambient credential variables."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_NAMES or key.startswith(_SAFE_ENV_PREFIXES)
    }


def _find_at_spi_bus_launcher() -> str:
    """Return the installed AT-SPI bus launcher path."""
    launcher = shutil.which("at-spi-bus-launcher")
    if launcher:
        return launcher
    for candidate in _AT_SPI_LAUNCHER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    msg = "at-spi-bus-launcher not found; install at-spi2-core"
    raise RuntimeError(msg)


def _remove_tree_stably(path: Path, attempts: int = 20, delay: float = 0.1) -> None:
    """Remove a session-owned tree and prove it is not being recreated."""
    consecutive_absent = 0
    for _ in range(attempts):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        time.sleep(delay)
        if path.exists():
            consecutive_absent = 0
            continue
        consecutive_absent += 1
        if consecutive_absent >= 2:
            return
    msg = f"Failed to remove session-owned directory after {attempts} attempts: {path}"
    raise OSError(msg)


class SessionType(Enum):
    """Type of KWin session."""

    VIRTUAL = "virtual"
    LIVE = "live"


@dataclass
class SessionConfig:
    """Configuration for an isolated KWin session."""

    socket_name: str = ""
    screen_width: int = 1920
    screen_height: int = 1080
    enable_clipboard: bool = False
    keep_screenshots: bool = False
    isolate_home: bool = False
    keep_home: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class AppInfo:
    """Tracking info for a launched application."""

    pid: int
    command: str
    log_path: Path
    process: subprocess.Popen[bytes]


@dataclass
class SessionInfo:
    """Runtime information about a running session."""

    dbus_address: str
    wayland_socket: str
    kwin_pid: int
    screenshot_dir: Path = field(default_factory=lambda: Path("/tmp"))
    home_dir: Path | None = None
    app_pid: int | None = None
    wrapper_pid: int | None = None
    apps: dict[int, AppInfo] = field(default_factory=dict)
    session_type: SessionType = SessionType.VIRTUAL


class Session:
    """An isolated KWin Wayland session.

    Uses dbus-run-session to create an isolated D-Bus session bus,
    then starts kwin_wayland --virtual inside it. Apps launched in
    this session are completely isolated from the host desktop.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._info: SessionInfo | None = None
        self._socket_name: str = ""
        self._app_counter: int = 0
        self._config: SessionConfig | None = None
        self._home_dir: Path | None = None
        self._startup_log: BinaryIO | None = None
        self._runtime_dir: Path | None = None
        self._owns_socket = False

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def info(self) -> SessionInfo | None:
        return self._info

    @property
    def wayland_socket(self) -> str:
        return self._socket_name

    def _xdg_isolation_env(self) -> dict[str, str]:
        """Build XDG environment overrides for home directory isolation."""
        if self._home_dir is None:
            return {}
        home = str(self._home_dir)
        return {
            "HOME": home,
            "XDG_CONFIG_HOME": str(self._home_dir / ".config"),
            "XDG_DATA_HOME": str(self._home_dir / ".local" / "share"),
            "XDG_CACHE_HOME": str(self._home_dir / ".cache"),
            "XDG_STATE_HOME": str(self._home_dir / ".local" / "state"),
        }

    def start(self, config: SessionConfig | None = None) -> SessionInfo:
        """Start an isolated KWin Wayland session.

        Returns SessionInfo with connection details.
        """
        if self.is_running:
            msg = "Session is already running"
            raise RuntimeError(msg)

        if config is None:
            config = SessionConfig()
        if config.screen_width <= 0 or config.screen_height <= 0:
            raise ValueError("Screen dimensions must be positive")
        if config.socket_name and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", config.socket_name
        ):
            raise ValueError("Socket name must be a simple filename (letters, digits, _, . or -)")
        for executable in (
            "dbus-run-session",
            "bash",
            "kwin_wayland",
            "dbus-update-activation-environment",
        ):
            if shutil.which(executable) is None:
                raise RuntimeError(
                    f"Required executable not found: {executable}; install it before retrying"
                )
        at_spi_launcher = _find_at_spi_bus_launcher()
        # Dispose of any resources from a compositor that exited between calls.
        self.stop()
        self._config = config
        self._socket_name = config.socket_name or f"wayland-mcp-{uuid.uuid4().hex}"
        env = self._build_env(config)
        self._runtime_dir = Path(env["XDG_RUNTIME_DIR"])
        if not self._runtime_dir.is_dir():
            raise RuntimeError(f"XDG_RUNTIME_DIR is not a directory: {self._runtime_dir}")
        socket_path = self._runtime_dir / self._socket_name
        if any(p.exists() or p.is_symlink() for p in (socket_path, Path(f"{socket_path}.lock"))):
            raise RuntimeError(
                f"Wayland socket already exists: {socket_path}; choose another socket_name"
            )

        try:
            if config.isolate_home:
                self._home_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-home-"))
                for subdir in (".config", ".local/share", ".local/state", ".cache", ".screenshots"):
                    (self._home_dir / subdir).mkdir(parents=True, exist_ok=True)
            env = self._build_env(config)
            self._startup_log = tempfile.TemporaryFile()  # noqa: SIM115 - closed by stop()
            self._process = subprocess.Popen(
                [
                    "dbus-run-session",
                    "bash",
                    "-c",
                    self._build_wrapper_script(config, at_spi_launcher),
                ],
                stdout=subprocess.PIPE,
                stderr=self._startup_log,
                env=env,
                start_new_session=True,
            )
            self._owns_socket = True
            dbus_address = self._read_startup_status(_STARTUP_TIMEOUT_SECONDS)
            if not socket_path.is_socket():
                raise RuntimeError("READY received without a Wayland socket")
            screenshot_dir = (
                self._home_dir / ".screenshots"
                if self._home_dir
                else Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))
            )
            self._info = SessionInfo(
                dbus_address=dbus_address,
                wayland_socket=self._socket_name,
                kwin_pid=self._process.pid,
                screenshot_dir=screenshot_dir,
                home_dir=self._home_dir,
            )
            return self._info
        except Exception as exc:
            stderr = self._read_startup_log()
            returncode = self._process.poll() if self._process else None
            try:
                self.stop()
            except OSError as cleanup_error:
                raise RuntimeError(
                    f"Session startup failed: {exc}; exit={returncode}; stderr: {stderr}; "
                    f"cleanup failed: {cleanup_error}"
                ) from exc
            raise RuntimeError(
                f"Session startup failed: {exc}; exit={returncode}; stderr: {stderr}"
            ) from exc

    def _read_startup_status(self, timeout: float) -> str:
        """Read bounded chunks, never a blocking readline from an incomplete line."""
        assert self._process is not None and self._process.stdout is not None
        deadline = time.monotonic() + timeout
        pending = b""
        dbus_address = ""
        fd = self._process.stdout.fileno()
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if not readable:
                if self._process.poll() is not None:
                    break
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode(errors="replace").strip()
                if text.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    dbus_address = text.split("=", 1)[1]
                elif text == "READY":
                    if not dbus_address:
                        raise RuntimeError("READY received without a D-Bus address")
                    if self._process.poll() is not None:
                        raise RuntimeError("Session exited during startup")
                    return dbus_address
            if len(pending) > 65536:
                raise RuntimeError("Startup output exceeded the line length limit")
        raise RuntimeError(f"No READY signal within {timeout:g}s (or session exited early)")

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application inside the isolated session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self.is_running or self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env = {
            **_sanitized_environment(),
            "WAYLAND_DISPLAY": self._socket_name,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        env.update(self._xdg_isolation_env())
        if extra_env:
            env.update(extra_env)
        if self._runtime_dir is not None:
            env["XDG_RUNTIME_DIR"] = str(self._runtime_dir)
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address

        # Create log file for stdout/stderr capture
        app_name = Path(command[0]).stem if command else "unknown"
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        log_file = log_path.open("ab")

        proc = subprocess.Popen(
            command,
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        # Close the fd in the parent; child has inherited it
        log_file.close()

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app.

        Args:
            pid: PID of the app (from launch_app).
            last_n_lines: Number of trailing lines to return (0 = all).

        Returns:
            The app's stdout/stderr output.
        """
        if self._info is None:
            msg = "Session is not running"
            raise RuntimeError(msg)

        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def stop(self) -> None:
        """Stop the isolated session and clean up all processes."""
        if self._process is not None:
            # Send SIGTERM to the entire process group (all children)
            try:
                pgid = os.getpgid(self._process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill the entire process group
                try:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=3)

            if self._process.stdout is not None:
                self._process.stdout.close()

        # Clean up home directory and/or screenshot directory
        if self._home_dir is not None:
            keep_home = self._config is not None and self._config.keep_home
            keep_screenshots = self._config is not None and self._config.keep_screenshots
            if not keep_home:
                # Remove the entire home and ensure late writers do not recreate it.
                _remove_tree_stably(self._home_dir)
            elif not keep_screenshots:
                # Keep home but remove screenshots subdirectory
                screenshots = self._home_dir / ".screenshots"
                if screenshots.exists():
                    shutil.rmtree(screenshots, ignore_errors=True)
        else:
            # No isolated home — use original screenshot cleanup logic
            keep = self._config is not None and self._config.keep_screenshots
            if not keep and self._info and self._info.screenshot_dir.exists():
                shutil.rmtree(self._info.screenshot_dir, ignore_errors=True)

        # Only remove socket files belonging to a successfully spawned wrapper.
        if self._owns_socket and self._runtime_dir is not None:
            for suffix in ("", ".lock"):
                (self._runtime_dir / f"{self._socket_name}{suffix}").unlink(missing_ok=True)
        self._owns_socket = False
        self._runtime_dir = None

        self._process = None
        self._info = None
        self._home_dir = None
        if self._startup_log is not None:
            self._startup_log.close()
            self._startup_log = None

    def _read_startup_log(self) -> str:
        """Read the compositor startup log without blocking."""
        if self._startup_log is None:
            return ""
        self._startup_log.flush()
        self._startup_log.seek(0, os.SEEK_END)
        self._startup_log.seek(max(0, self._startup_log.tell() - 16384))
        return self._startup_log.read().decode(errors="replace").strip()

    def _build_wrapper_script(self, config: SessionConfig, at_spi_launcher: str) -> str:
        """Build the bash script that runs inside dbus-run-session."""
        launcher = shlex.quote(at_spi_launcher)
        return f"""\
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"

# Ensure all child processes are cleaned up on exit
cleanup() {{
    kill ${{KWIN_PID:-}} ${{AT_SPI_PID:-}} 2>/dev/null
    wait ${{KWIN_PID:-}} ${{AT_SPI_PID:-}} 2>/dev/null
}}
trap cleanup EXIT TERM INT HUP

# Start the AT-SPI accessibility bus.
# ATSPI_DBUS_IMPLEMENTATION is set in _build_env() to force dbus-daemon
# instead of dbus-broker (which reuses the host's AT-SPI bus).
{launcher} --launch-immediately >&2 &
AT_SPI_PID=$!
sleep 0.2
if ! kill -0 "$AT_SPI_PID" 2>/dev/null; then
    echo "AT-SPI launcher exited before session startup" >&2
    exit 1
fi

# Pre-set D-Bus activation environment BEFORE starting KWin.
# When KWin triggers portal auto-activation, portal-kde will get
# WAYLAND_DISPLAY pointing to our isolated compositor socket.
# The socket doesn't exist yet, but portal-kde will be activated
# only after KWin creates it.
dbus-update-activation-environment WAYLAND_DISPLAY={self._socket_name} \\
    QT_QPA_PLATFORM=wayland >&2 || exit 1

# Start KWin WITHOUT WAYLAND_DISPLAY to prevent nesting attempt.
# KWin with --virtual creates its own compositor, it must not try
# to connect to another compositor as a client.
# Explicitly pass KWIN_ permission env vars to ensure they reach the
# KWin process (environment inheritance through dbus-run-session can be unreliable).
env -u WAYLAND_DISPLAY -u QT_QPA_PLATFORM \
    KWIN_WAYLAND_NO_PERMISSION_CHECKS=1 \
    KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 \
    kwin_wayland --virtual --no-lockscreen \
    --width {config.screen_width} --height {config.screen_height} \
    --socket {self._socket_name} >&2 &
KWIN_PID=$!

# Wait for KWin socket to appear
attempt=0
while [ ! -e "$XDG_RUNTIME_DIR/{self._socket_name}" ]; do
    if ! kill -0 "$KWIN_PID" 2>/dev/null; then
        wait "$KWIN_PID"
        exit $?
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 150 ]; then
        echo "Timed out waiting for KWin socket" >&2
        exit 1
    fi
    sleep 0.1
done
sleep 0.3

# Signal parent that setup is complete
echo "READY"

# Block until kwin exits
wait $KWIN_PID
"""

    def _build_env(self, config: SessionConfig) -> dict[str, str]:
        """Build the environment for the isolated session."""
        env = {
            **_sanitized_environment(),
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "KDE_FULL_SESSION": "true",
            "KDE_SESSION_VERSION": "6",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
            # Force dbus-daemon for the AT-SPI bus instead of dbus-broker.
            # dbus-broker with --scope=user reuses the host's existing AT-SPI bus,
            # breaking accessibility isolation. Verified as REQUIRED.
            "ATSPI_DBUS_IMPLEMENTATION": "dbus-daemon",
            # Allow direct D-Bus screenshot capture without portal authorization.
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
            # Allow clients to bind restricted Wayland protocols (e.g. plasma_window_management).
            # Safe in isolated virtual sessions where there is no user desktop to protect.
            "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
        }
        # Remove host display references to avoid kwin connecting to host
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)

        env.update(self._xdg_isolation_env())
        env.update(config.extra_env)
        return env

    def _wait_for_socket(self, socket_path: Path, timeout: float) -> bool:
        """Wait for the Wayland socket file to appear."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if socket_path.exists():
                return True
            # Check if process died
            if self._process and self._process.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class LiveSession:
    """Connection to an existing (non-virtual) KWin session.

    Attaches to a KWin compositor that is already running, such as
    the user's real desktop or a KWin instance inside a container.
    Does NOT manage the compositor lifecycle — stop() only disconnects.
    """

    def __init__(
        self,
        dbus_address: str,
        wayland_socket: str,
        screenshot_dir: Path,
    ) -> None:
        self._info = SessionInfo(
            dbus_address=dbus_address,
            wayland_socket=wayland_socket,
            kwin_pid=0,
            screenshot_dir=screenshot_dir,
            session_type=SessionType.LIVE,
        )
        self._running = True
        self._app_counter: int = 0
        self._keep_screenshots: bool = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def info(self) -> SessionInfo | None:
        return self._info if self._running else None

    @property
    def wayland_socket(self) -> str:
        return self._info.wayland_socket

    def launch_app(self, command: list[str], extra_env: dict[str, str] | None = None) -> AppInfo:
        """Launch an application in the live session.

        Returns AppInfo with pid, command, and log_path.
        """
        if not self._running:
            msg = "Session is not running"
            raise RuntimeError(msg)

        env = {
            **_sanitized_environment(),
            "WAYLAND_DISPLAY": self._info.wayland_socket,
            "QT_QPA_PLATFORM": "wayland",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
        }
        if self._info.dbus_address:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._info.dbus_address
        if extra_env:
            env.update(extra_env)

        app_name = Path(command[0]).stem if command else "unknown"
        self._app_counter += 1
        log_path = self._info.screenshot_dir / f"app_{app_name}_{self._app_counter}.log"
        log_file = log_path.open("ab")

        proc = subprocess.Popen(
            command,
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        log_file.close()

        app_info = AppInfo(
            pid=proc.pid,
            command=" ".join(command),
            log_path=log_path,
            process=proc,
        )
        self._info.app_pid = proc.pid
        self._info.apps[proc.pid] = app_info
        return app_info

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read the log output of a launched app."""
        app = self._info.apps.get(pid)
        if app is None:
            available = list(self._info.apps.keys())
            msg = f"No app with PID {pid}. Available PIDs: {available}"
            raise ValueError(msg)

        if not app.log_path.exists():
            return "(no log output yet)"

        text = app.log_path.read_text(errors="replace")
        if last_n_lines > 0:
            lines = text.splitlines()
            text = "\n".join(lines[-last_n_lines:])
        return text or "(no log output yet)"

    def stop(self, *, keep_screenshots: bool = False) -> None:
        """Disconnect from the live session.

        Only cleans up screenshot directory. Does NOT kill KWin or any apps
        that were already running before the connection.
        """
        if not self._running:
            return
        self._running = False

        # Terminate apps launched by us
        for app in self._info.apps.values():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                app.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                app.process.wait(timeout=3)
            if app.process.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    app.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    app.process.wait(timeout=3)

        if not keep_screenshots and self._info.screenshot_dir.exists():
            shutil.rmtree(self._info.screenshot_dir, ignore_errors=True)
