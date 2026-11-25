"""Fail-closed matching for read-only KWin-to-AT-SPI window snapshots."""

from __future__ import annotations

import math
from typing import Any

MAX_PEERS = 32
MAX_APPLICATIONS = 256
MAX_TOP_LEVELS = 256


def _logical_dimension(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 < value <= 32768 or not math.isfinite(value):
        return None
    integer = round(value)
    # KWin's QRectF can carry tiny representation error after output scaling,
    # e.g. 429.99999999999994. Accept up to two IEEE representation steps;
    # this is not a physical pixel tolerance or a fuzzy geometry match.
    if abs(value - integer) > 2 * math.ulp(value):
        return None
    return integer


def _size(geometry: object) -> tuple[int, int] | None:
    """Accept only exact, positive integer logical dimensions."""
    if not isinstance(geometry, dict):
        return None
    width = _logical_dimension(geometry.get("width"))
    height = _logical_dimension(geometry.get("height"))
    return (width, height) if width is not None and height is not None else None


def match_window_peer(
    target: dict[str, Any],
    accessible_sizes: list[tuple[int, int] | None],
    toolkit: str,
) -> tuple[int, str] | None:
    """Choose only a unique, complete PID/title cohort match.

    The caller establishes exact PID/title identity for both cohorts. Matches
    within a duplicate GTK cohort require a unique one-to-one exact size match;
    no screen position, window ordering, activity or partial cohort is trusted.
    """
    peers = target.get("peers")
    wanted = target.get("window_id")
    if not isinstance(peers, list) or not 1 <= len(peers) <= MAX_PEERS:
        return None
    if not all(isinstance(p, dict) and isinstance(p.get("window_id"), str) for p in peers):
        return None
    ids = [p["window_id"] for p in peers]
    if len(ids) != len(set(ids)) or wanted not in ids or len(ids) != len(accessible_sizes):
        return None
    if len(ids) == 1:
        return 0, "pid_and_exact_title"
    # Gtk 3 is the first bridge qualified by the private KWin fixture. Qt and
    # other toolkit geometries remain deliberately unsupported for title ties.
    if toolkit != "gtk3" or any(size is None for size in accessible_sizes):
        return None
    mappings: list[int] = []
    for peer in peers:
        # All qualified sizes use the client rect. Decorations and full buffer
        # dimensions are different concepts and must not create fuzzy matches.
        matches = [
            i
            for i, size in enumerate(accessible_sizes)
            if size == _size(peer.get("client_geometry"))
        ]
        if len(matches) != 1:
            return None
        mappings.append(matches[0])
    if len(set(mappings)) != len(ids):
        return None
    return mappings[ids.index(wanted)], "pid_title_and_unique_gtk3_client_size"
