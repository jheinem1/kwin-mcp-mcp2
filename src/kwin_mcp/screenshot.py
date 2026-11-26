"""Screenshot capture via KWin ScreenShot2 D-Bus or spectacle CLI fallback."""

from __future__ import annotations

import contextlib
import io
import os
import selectors
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dbus
import dbus.bus

if TYPE_CHECKING:
    from PIL import Image

_CAPTURE_TIMEOUT = 10.0
_MAX_CAPTURE_BYTES = 256 * 1024 * 1024
_MAX_WINDOW_PNG_BYTES = 8 * 1024 * 1024
_MAX_WINDOW_RAW_BYTES = 64 * 1024 * 1024


class UnsupportedCaptureError(RuntimeError):
    """Capture was withheld because compositing is inactive or unverifiable."""


def require_active_compositing(dbus_address: str, *, owner: str = "org.kde.KWin") -> None:
    """Check the safe active property before asking KWin or Spectacle to capture.

    KWin 6.8.2 can accept Wayland/input clients without a render device, then
    crash inside ScreenShot2. Even querying compositingType can dereference its
    missing backend, so only query active. Never change renderer/security settings.
    A successful check is not a guarantee against unrelated compositor bugs.
    """
    bus = None
    try:
        bus = (
            dbus.bus.BusConnection(dbus_address) if dbus_address else dbus.SessionBus(private=True)
        )
        proxy = bus.get_object(owner, "/Compositor", introspect=False)
        properties = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
        active = bool(properties.Get("org.kde.kwin.Compositing", "active", timeout=3.0))
    except Exception as exc:
        raise UnsupportedCaptureError(
            "Screenshot not attempted: could not verify that KWin compositing is active. "
            "Use accessibility observation and inspect compositor startup diagnostics."
        ) from exc
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.close()
    if not active:
        raise UnsupportedCaptureError(
            "Screenshot not attempted: KWin compositing is inactive. "
            "The session may accept windows/input without a working render device; "
            "capture can crash KWin in that state. Use accessibility observation "
            "or a separately configured rendering-capable test environment."
        )


def screenshot_metadata(path: Path) -> dict:
    """Report image dimensions without inventing a global logical coordinate transform.

    Spectacle may capture multiple outputs with different scales. Active-screen
    D-Bus images do not by themselves establish that screen's global origin.
    Clients must obtain output geometry before converting pixels to input points.
    """
    from PIL import Image

    with Image.open(path) as img:
        width, height = img.size
    return {
        "pixel_width": width,
        "pixel_height": height,
        "coordinate_space": "image_pixels",
        "input_coordinate_space": "global_logical",
        "transform_available": False,
        "scale": None,
        "origin": None,
    }


def _capture_raw(
    iface: Any,
    options: dict,
    *,
    timeout: float = _CAPTURE_TIMEOUT,
    output_name: str | None = None,
    window_id: str | None = None,
    byte_limit: int = _MAX_CAPTURE_BYTES,
) -> tuple:
    """Drain concurrently: a compositor may fill the pipe before replying on D-Bus.

    The reader owns its descriptor and always closes it. Both D-Bus and pipe
    reads have deadlines, including a writer that never closes its descriptor.
    """
    if output_name is not None and window_id is not None:
        raise ValueError("Only one screenshot scope may be selected")
    read_fd, write_fd = os.pipe()
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    errors: list[Exception] = []

    def drain() -> None:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                total = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("KWin screenshot data timed out")
                    chunk = os.read(read_fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > byte_limit:
                        raise RuntimeError("KWin screenshot exceeds capture byte limit")
                    chunks.append(chunk)
        except Exception as exc:
            errors.append(exc)
        finally:
            os.close(read_fd)

    reader = threading.Thread(target=drain, daemon=True)
    try:
        reader.start()
    except Exception:
        os.close(read_fd)
        os.close(write_fd)
        raise
    try:
        if window_id is not None:
            results = iface.CaptureWindow(
                window_id, options, dbus.types.UnixFd(write_fd), timeout=timeout
            )
        elif output_name is None:
            results = iface.CaptureActiveScreen(
                options, dbus.types.UnixFd(write_fd), timeout=timeout
            )
        else:
            results = iface.CaptureScreen(
                output_name, options, dbus.types.UnixFd(write_fd), timeout=timeout
            )
    finally:
        os.close(write_fd)
        reader.join(max(0.0, deadline - time.monotonic()) + 0.1)
    if reader.is_alive():
        raise TimeoutError("KWin screenshot reader did not finish before deadline")
    if errors:
        raise errors[0]
    data = b"".join(chunks)
    width, height, stride = (int(results[key]) for key in ("width", "height", "stride"))
    if width <= 0 or height <= 0 or stride < width * 4 or len(data) != stride * height:
        raise RuntimeError("KWin screenshot returned invalid dimensions or incomplete pixel data")
    return data, results


def _decode_capture(data: bytes, results: dict) -> Image.Image:
    """Decode ScreenShot2's reported QImage format instead of guessing byte order.

    Qt formats 4 to 6 are native-endian packed pixels. Formats 16 to 18 are byte
    ordered on every architecture. Lowercase 'a' tells Pillow to undo alpha
    premultiplication when it converts into straight RGBA for PNG.
    """
    from PIL import Image

    if not data:
        raise RuntimeError("KWin ScreenShot2 returned no data")
    try:
        width = int(results["width"])
        height = int(results["height"])
        stride = int(results["stride"])
        format_code = int(results["format"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("KWin ScreenShot2 returned invalid image metadata") from exc
    if width <= 0 or height <= 0 or stride < 4 * width or len(data) != stride * height:
        raise RuntimeError("KWin ScreenShot2 returned inconsistent raw image dimensions")

    little = sys.byteorder == "little"
    modes = {
        4: ("RGB", "BGRX" if little else "XRGB"),  # Format_RGB32
        5: ("RGBA", "BGRA" if little else "ARGB"),  # Format_ARGB32
        6: ("RGBA", "BGRa") if little else ("RGBa", "aRGB"),  # Format_ARGB32_Premultiplied
        16: ("RGB", "RGBX"),  # Format_RGBX8888
        17: ("RGBA", "RGBA"),  # Format_RGBA8888
        18: ("RGBA", "RGBa"),  # Format_RGBA8888_Premultiplied
    }
    if format_code not in modes:
        raise RuntimeError(f"KWin ScreenShot2 returned unsupported QImage format {format_code}")
    mode, raw_mode = modes[format_code]
    image = Image.frombytes(mode, (width, height), data, "raw", raw_mode, stride)
    return image.convert("RGBA") if mode == "RGBa" else image


def capture_screenshot_to_file(
    dbus_address: str = "",
    wayland_socket: str = "",
    *,
    include_cursor: bool = False,
    output_dir: Path | None = None,
) -> Path:
    """Capture a screenshot and save to a file.

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        wayland_socket: Wayland socket name for the isolated session.
        include_cursor: Whether to include the mouse cursor.
        output_dir: Directory to save the screenshot. Uses /tmp if not specified.

    Returns:
        Absolute path of the saved PNG file.
    """
    if output_dir is None:
        output_dir = Path("/tmp")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"screenshot_{timestamp}_{uuid.uuid4().hex[:8]}.png"

    _capture_via_spectacle(
        dbus_address,
        wayland_socket,
        output_path=output_path,
        include_cursor=include_cursor,
    )
    return output_path


def capture_screenshot_dbus(
    dbus_address: str,
    output_path: Path,
    *,
    include_cursor: bool = False,
) -> Path:
    """Capture screenshot directly via KWin ScreenShot2 D-Bus interface.

    Much faster than spectacle CLI (~15-30ms vs ~200-300ms per frame)
    because it avoids process spawn overhead. Suitable for rapid
    frame capture at short intervals (e.g., every 50ms).

    Requires KWIN_SCREENSHOT_NO_PERMISSION_CHECKS=1 to be set in the
    KWin process environment (automatically set for isolated sessions).

    Args:
        dbus_address: D-Bus session bus address for the isolated session.
        output_path: Path to save the PNG file.
        include_cursor: Whether to include the mouse cursor.

    Returns:
        The output_path with the saved PNG file.
    """
    require_active_compositing(dbus_address)
    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")

    options = {"include-cursor": dbus.Boolean(include_cursor)}
    data, results = _capture_raw(iface, options)
    img = _decode_capture(data, results)
    img.save(output_path, "PNG")
    return output_path


def capture_frame_burst(
    dbus_address: str,
    output_dir: Path,
    delays_ms: list[int],
    *,
    include_cursor: bool = False,
    wayland_socket: str = "",
) -> list[Path]:
    """Capture multiple screenshots at specified delays after an action.

    Takes screenshots at each delay (in milliseconds) using the fast
    D-Bus capture method. Falls back to spectacle CLI if D-Bus authorization
    fails (e.g. in live sessions without KWIN_SCREENSHOT_NO_PERMISSION_CHECKS).

    Args:
        dbus_address: D-Bus session bus address for the session.
        output_dir: Directory to save the frame PNG files.
        delays_ms: List of delays in milliseconds (e.g., [0, 50, 100, 200, 500]).
        include_cursor: Whether to include the mouse cursor.
        wayland_socket: Wayland socket name (needed for spectacle fallback).

    Returns:
        List of paths to the captured PNG files, ordered by delay.
    """
    require_active_compositing(dbus_address)
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_delays = sorted(delays_ms)

    # Try fast D-Bus capture first
    try:
        return _capture_frame_burst_dbus(
            dbus_address, output_dir, sorted_delays, include_cursor=include_cursor
        )
    except dbus.DBusException:
        # D-Bus authorization failed (e.g. live session without permission bypass).
        # Fall back to spectacle CLI (slower; subject to session permissions).
        return _capture_frame_burst_spectacle(
            dbus_address,
            wayland_socket,
            output_dir,
            sorted_delays,
            include_cursor=include_cursor,
        )


def _capture_frame_burst_dbus(
    dbus_address: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[Path]:
    """Capture frames using fast KWin ScreenShot2 D-Bus interface."""
    # Reuse a single D-Bus connection for all captures
    bus = dbus.bus.BusConnection(dbus_address)
    screenshot_obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
    iface = dbus.Interface(screenshot_obj, "org.kde.KWin.ScreenShot2")
    options = {"include-cursor": dbus.Boolean(include_cursor)}

    # Phase 1: Capture all raw frames with accurate timing
    raw_frames: list[tuple[bytes, dict]] = []
    start = time.monotonic()
    for delay_ms in sorted_delays:
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        raw_frames.append(_capture_raw(iface, options))

    # Phase 2: Convert raw frames to PNG (timing-insensitive)
    frame_paths: list[Path] = []
    for i, (delay_ms, (data, results)) in enumerate(zip(sorted_delays, raw_frames, strict=True)):
        if not data:
            msg = f"KWin ScreenShot2 returned no data for frame at {delay_ms}ms"
            raise RuntimeError(msg)
        frame_path = output_dir / f"frame_{uuid.uuid4().hex[:8]}_{i:03d}_{delay_ms}ms.png"
        img = _decode_capture(data, results)
        img.save(frame_path, "PNG")
        frame_paths.append(frame_path)

    return frame_paths


def _capture_frame_burst_spectacle(
    dbus_address: str,
    wayland_socket: str,
    output_dir: Path,
    sorted_delays: list[int],
    *,
    include_cursor: bool = False,
) -> list[Path]:
    """Capture frames using spectacle CLI (slower; subject to session permissions)."""
    frame_paths: list[Path] = []
    start = time.monotonic()
    for i, delay_ms in enumerate(sorted_delays):
        target_time = start + delay_ms / 1000.0
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)

        frame_path = output_dir / f"frame_{uuid.uuid4().hex[:8]}_{i:03d}_{delay_ms}ms.png"
        _capture_via_spectacle(
            dbus_address,
            wayland_socket,
            output_path=frame_path,
            include_cursor=include_cursor,
        )
        frame_paths.append(frame_path)
    return frame_paths


def _capture_via_spectacle(
    dbus_address: str,
    wayland_socket: str,
    *,
    output_path: Path,
    include_cursor: bool = False,
) -> None:
    """Capture screenshot using spectacle CLI in background mode."""
    require_active_compositing(dbus_address)
    cmd = ["spectacle", "-b", "-f", "-n", "-o", str(output_path)]
    if include_cursor:
        cmd.append("-p")

    env = {**os.environ}
    if dbus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address
    if wayland_socket:
        env["WAYLAND_DISPLAY"] = wayland_socket
        env["QT_QPA_PLATFORM"] = "wayland"
    # Remove host display refs
    env.pop("DISPLAY", None)

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        msg = (
            "spectacle not found. Install spectacle "
            "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
        )
        raise RuntimeError(msg) from None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        msg = f"spectacle failed (exit {result.returncode}): {stderr}"
        raise RuntimeError(msg)

    if not output_path.exists() or output_path.stat().st_size == 0:
        msg = "spectacle produced no output"
        raise RuntimeError(msg)


def capture_output_dbus(
    dbus_address: str, output_name: str, output_path: Path, *, include_cursor: bool = False
) -> Path:
    """Capture exactly one named output; never fall back to a different scope."""
    if not output_name:
        raise ValueError("An exact output name is required")
    require_active_compositing(dbus_address)
    bus = dbus.bus.BusConnection(dbus_address)
    try:
        obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
        iface = dbus.Interface(obj, "org.kde.KWin.ScreenShot2")
        data, result = _capture_raw(
            iface,
            {
                "include-cursor": dbus.Boolean(include_cursor),
                "native-resolution": dbus.Boolean(True),
            },
            output_name=output_name,
        )
        if result.get("screen") != output_name:
            raise RuntimeError("KWin did not confirm the requested screenshot output")
        image = _decode_capture(data, result)
        image.save(output_path, "PNG")
        return output_path
    finally:
        bus.close()


def capture_window_dbus(dbus_address: str, window_id: str, *, owner: str) -> tuple[dict, bytes]:
    """Capture only this normal-window UUID on the previously verified bus owner."""
    target = uuid.UUID(window_id)
    if not owner.startswith(":"):
        raise ValueError("An exact compositor bus owner is required")
    require_active_compositing(dbus_address, owner=owner)
    bus = dbus.bus.BusConnection(dbus_address)
    try:
        current_owner = bus.call_blocking(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            "s",
            ("org.kde.KWin",),
            timeout=2.0,
        )
        if current_owner != owner:
            raise RuntimeError("KWin owner changed")
        obj = bus.get_object(owner, "/org/kde/KWin/ScreenShot2", introspect=False)
        iface = dbus.Interface(obj, "org.kde.KWin.ScreenShot2")
        data, result = _capture_raw(
            iface,
            dbus.Dictionary(
                {
                    "include-cursor": dbus.Boolean(False),
                    "include-decoration": dbus.Boolean(True),
                    "include-shadow": dbus.Boolean(False),
                    "native-resolution": dbus.Boolean(True),
                },
                signature="sv",
            ),
            window_id="{" + str(target) + "}",
            byte_limit=_MAX_WINDOW_RAW_BYTES,
            timeout=4.0,
        )
        if uuid.UUID(str(result.get("windowId", ""))) != target:
            raise RuntimeError("KWin did not confirm the requested screenshot window")
        image = _decode_capture(data, result)
        if image.mode == "RGBA" and image.getchannel("A").getextrema()[1] == 0:
            raise RuntimeError("KWin returned a fully transparent window frame")
        stream = io.BytesIO()
        image.save(stream, "PNG")
        if stream.tell() > _MAX_WINDOW_PNG_BYTES:
            raise RuntimeError("Window PNG exceeds transfer limit")
        return {"pixel_width": image.width, "pixel_height": image.height}, stream.getvalue()
    finally:
        bus.close()
