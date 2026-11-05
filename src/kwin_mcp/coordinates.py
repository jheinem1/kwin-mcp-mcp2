"""Conservative mapping for a named-output capture, never whole-desktop guesses."""

from __future__ import annotations

import math
from typing import Any


def output_geometry(outputs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Require exactly one named output and finite, positive logical dimensions."""
    matches = [output for output in outputs if output.get("name") == name]
    if len(matches) != 1:
        raise ValueError("Output name must identify exactly one connected output")
    output = matches[0]
    geometry = output.get("geometry", {})
    values = {key: geometry.get(key) for key in ("x", "y", "width", "height")}
    if any(
        isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
        for value in values.values()
    ):
        raise ValueError("Output geometry is missing or non-finite")
    if values["width"] <= 0 or values["height"] <= 0:
        raise ValueError("Output dimensions must be positive")
    scale = output.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale):
        raise ValueError("Output scale is unavailable")
    if scale <= 0:
        raise ValueError("Output scale must be positive")
    return {"name": name, "geometry": values, "scale": scale}


def map_point(
    pixel_x: float,
    pixel_y: float,
    pixel_width: int,
    pixel_height: int,
    captured_output: dict[str, Any],
    current_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map an image pixel center only while the named output's layout is unchanged."""
    previous = output_geometry([captured_output], captured_output["name"])
    current = output_geometry(current_outputs, previous["name"])
    if previous != current:
        raise ValueError("Output layout changed since capture; capture again before mapping")
    if (
        isinstance(pixel_width, bool)
        or isinstance(pixel_height, bool)
        or not isinstance(pixel_width, int)
        or not isinstance(pixel_height, int)
        or pixel_width <= 0
        or pixel_height <= 0
    ):
        raise ValueError("Image dimensions must be positive integers")
    for value, limit in ((pixel_x, pixel_width), (pixel_y, pixel_height)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= limit - 1
        ):
            raise ValueError("Point must be finite and inside the captured image")
    geometry = previous["geometry"]
    # ScreenShot2 CaptureScreen returns the output's displayed orientation.
    # Use measured image dimensions, including fractional-scale rounding.
    x = geometry["x"] + (pixel_x + 0.5) * geometry["width"] / pixel_width
    y = geometry["y"] + (pixel_y + 0.5) * geometry["height"] / pixel_height
    return {
        "x": x,
        "y": y,
        "coordinate_space": "global_logical",
        "output_name": previous["name"],
        "mapping": "named_output_pixel_center",
        "input_dispatched": False,
    }
