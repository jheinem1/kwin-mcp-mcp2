"""Bounded native adapters; dispatch acknowledgments are not UI observations.

FileManager1 is implemented by Dolphin on KDE, but the session's registered
provider may be another file manager. No particular process or window is targeted.
"""

from __future__ import annotations

import contextlib
from urllib.parse import quote

import dbus
import dbus.bus

_FILE_MANAGER = "org.freedesktop.FileManager1"
_ACTIONS = {"show_items": "ShowItems", "show_folders": "ShowFolders"}


def local_path_uri(path: str) -> str:
    """Encode a local absolute path without expansion, resolution, or filesystem I/O."""
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise ValueError("Each path must be a nonempty string of at most 4096 characters")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Only local absolute paths are supported, not URIs or network paths")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Control characters are not allowed in paths")
    if any(component in (".", "..") for component in path.split("/")):
        raise ValueError("Dot and parent traversal components are not allowed")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Paths must be valid UTF-8") from exc
    return "file://" + quote(path, safe="/")


def show_local_paths(dbus_address: str, paths: list[str], *, action: str = "show_items") -> dict:
    """Ask the selected session's file manager to reveal items or open folders.

    Paths belong to the desktop machine. Existence and UI state are deliberately
    not inferred from this process's filesystem. The provider can launch or open
    windows. A returned D-Bus reply means only that the request was accepted;
    selection, focus, and navigation must be observed separately. Never retry
    automatically after a dispatch error, which may have followed execution.
    """
    if action not in _ACTIONS:
        raise ValueError("action must be show_items or show_folders")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise ValueError("paths must be a list containing between 1 and 32 local paths")
    uris = [local_path_uri(path) for path in paths]
    if not isinstance(dbus_address, str) or not dbus_address.strip():
        raise ValueError("An explicit session D-Bus address is required")
    result = {
        "adapter": "freedesktop_file_manager",
        "provider": _FILE_MANAGER,
        "action": action,
        "uris": uris,
        "dispatch": "not_attempted",
        "observation": "not_verified",
        "ui_verified": False,
    }
    bus = None
    try:
        bus = dbus.bus.BusConnection(dbus_address)
        # Avoid implicit introspection RPCs. Only the allowlisted operation is called.
        proxy = bus.get_object(_FILE_MANAGER, "/org/freedesktop/FileManager1", introspect=False)
        iface = dbus.Interface(proxy, _FILE_MANAGER)
        method = getattr(iface, _ACTIONS[action])
        result["dispatch"] = "unknown"
        method(dbus.Array(uris, signature="s"), "", timeout=5.0)
        result["dispatch"] = "accepted"
        result["message"] = "D-Bus accepted the request; the resulting UI state is not verified."
    except Exception as exc:
        result["error"] = str(exc)[:1024]
        result["message"] = (
            "Dispatch outcome may be unknown; observe before retrying."
            if result["dispatch"] == "unknown"
            else "Request was not dispatched."
        )
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.close()
    return result
