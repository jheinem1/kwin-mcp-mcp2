# Named KWin output capture

Use the named-output tools when several Wayland outputs are connected and you
know which one to inspect. The compositor output inventory supplies exact names
and global logical rectangles. The capture remains restricted to the named
output; a denied capture never falls back to a different output or the desktop.\nThe compositor must confirm the exact output name in its capture reply.

1. Call **kwin_windows** to get an exact name, e.g. **DP-1**.
2. Call **screenshot_output** with that name. The saved PNG uses native-resolution
   pixels. The result also reports its dimensions and whether a coordinate
   transform was available.
3. If available, use the recent **capture_id** with **map_screenshot_point**. This
   returns a global logical coordinate without sending any input.

Mapping exists for at most 60 seconds, within the same MCP session. It is
withheld if the output disappears, its logical rectangle or scale changes, or
the image dimensions are inconsistent with that output's native geometry. A
mapping is not evidence that a UI target is still in the same place. It is not
an assertion about desktop-wide mixed-DPI transforms, all monitor rotations,
or lock-screen capture. Existing KWin capture permissions are unchanged.
