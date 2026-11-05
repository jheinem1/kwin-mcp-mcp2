# Behavior, safety boundaries, and feature gaps

This document describes the MCP 2 fork, not a promise of support for every KDE
application. A successful transport call is not proof that an application changed.

## Choosing a control path

- **Virtual session:** starts a private D-Bus session and KWin Wayland display.
  Optional `isolate_home` separates HOME/XDG configuration. It does not sandbox
  filesystem access, networking, credentials explicitly supplied by the caller,
  or processes. Startup checks dependencies, waits within a fixed deadline, and
  cleans up owned resources on failure. It never deletes a caller-named existing
  Wayland socket to force startup. Fix the reported prerequisite and retry.
- **Live session:** attaches to an existing session. Pointer and keyboard input
  affect that session's current focus. Attaching does not grant permission to
  unlock it, bypass the lock screen, or change compositor security settings.
- **Semantic action:** `invoke_ui_action` uses an application's AT-SPI action.
  This can work without moving the global pointer, but only if the app exposes
  and accepts the action. It is not a universal background-control mechanism.
- **Coordinates:** EIS absolute input uses global logical coordinates. AT-SPI
  coordinates depend on toolkit/Wayland support and can be surface-relative.
  Do not assume accessibility coordinates or screenshot pixels can be clicked
  directly on a scaled, moved, or multi-monitor window.

## Result and retry contract

Input calls with `screenshot_after_ms` report action dispatch separately from
observation. A capture error returns the original action result, an observation
failure, and a warning not to repeat the input. Dispatch means the backend
accepted the command, not that the intended application received it or completed
its task. Input errors before capture remain errors and can still have an unknown
partial outcome. Inspect current state before retrying any mutation.

`invoke_ui_action` accepts optional `required_states` and `verify_states` lists.
Required states are checked before dispatch. Verification is a single observation
of the same element after dispatch, not a wait for a business outcome. For example,
`verify_states=["checked"]` can check a toggle, but cannot prove that a file saved.
A disappearing element or failed postcondition does not authorize repeating the
action. Existing `match_index` selection remains available; use narrow queries and
re-read the target before consequential actions.

`focus_window(app_name, window_title="exact title")` can restrict AT-SPI focus to
an exact title. Ambiguous windows are rejected; the focus request and observed
focused/active state are checked separately. This is **not a KWin window-ID API**.
AT-SPI-hidden windows and duplicate titles still require another control path.

AT-SPI requests use a single bounded subprocess budget (normally six seconds,
wait requests up to sixty seconds including overhead), native call timeouts, and
a worker deadline. Read-only operations may retry within the remaining budget.
Focus and semantic mutations are never automatically retried. Timeout means
unknown outcome when dispatch might already have happened.

## Screenshots and scaling

Single screenshots currently use Spectacle. Frame bursts first use KWin
ScreenShot2 and may fall back to Spectacle on D-Bus failure. These paths may have
different output scope: an active-screen capture is not necessarily a full-desktop
capture. Permission failure is not permission to disable live capture checks.

Screenshot responses include actual `pixel_width` and `pixel_height` plus
`coordinate_space`, `input_coordinate_space`, `transform_available`, `scale`, and
`origin`. Currently `transform_available` is false, and scale/origin are null.
This explicitly signals missing geometry rather than silently inventing 1:1
scaling. Clients need independently verified output geometry before converting
pixels to pointer coordinates. Mixed-scale output mapping is not implemented.

D-Bus pixel pipes drain concurrently with the method call to avoid large-frame
pipe deadlocks. Each capture has a ten-second deadline, a 256 MiB size limit, and
validates the expected pixel length. Frame scheduling is best effort, not a
real-time guarantee. A burst can take longer than one capture deadline.

## Exact window IDs and named-output coordinates

`kwin_windows` uses a short-lived KWin 6 script to return compositor internal
UUIDs, captions, logical window rectangles, output names, and logical output
geometry/scale. `activate_window(window_id)` targets only that UUID and observes
whether KWin's active-window property changed. This is not proof that a locked
desktop is visible, that a minimized application rendered, or that an app accepted
subsequent input. Unknown IDs fail without title matching. The script is unloaded
after the request; cleanup errors and uncertain activation outcomes are reported.

`screenshot_output(output_name)` calls ScreenShot2 CaptureScreen for exactly the
named output, with no whole-desktop fallback. It reads layout before and after
capture. Stable layout produces a capture ID usable for 60 seconds in the same
session. `map_screenshot_point(capture_id, x, y)` rechecks layout and maps the
image pixel center to global logical coordinates without dispatching input.
Receipts are bounded to 32. Negative origins, fractional scales, and other outputs
with different scales are covered by mathematical regressions. Changed output
layout, disconnected outputs, expired receipts, and out-of-image points fail.

This relies on CaptureScreen returning the named output in displayed orientation.
Rotated-output, fractional-DPI, and hotplug behavior still need real compositor
acceptance. A layout can also change after mapping and before separate input.
Prefer semantic actions for consequential operations. Existing whole-desktop
screenshots and bursts do not acquire a global transform.

## Native file-manager adapter and browser boundaries

`file_manager_show(paths, action="show_items")` calls the standard
org.freedesktop.FileManager1 ShowItems method. Use `show_folders` to open folders.
Only explicit local absolute paths are accepted, at most 32 per request. URI
escaping is handled internally; remote URIs and traversal components are rejected.
Dolphin implements this interface, but the session's registered file manager may
be another application. A request may launch a window. D-Bus acceptance does not
prove selection, focus, rendering, path existence, or a lock-screen transition.
There is no automatic retry after an uncertain result.

Firefox and Electron applications (Discord, T3 Code, Steam, and Nook) continue
through generic AT-SPI and input where exposed. No browser debugging port,
extension grant, authentication bypass, or application-specific private protocol
is enabled. Future browser adapters should attach only to a deliberately
authorized browser automation session, scope access to requested pages, verify
page identity immediately before actions, and observe app-specific postconditions.
A separate Discord bridge's locked observation is not evidence that this server
supports locked actions.

## Current feature gaps

- Whole-desktop mixed-DPI transforms, and live validation of named-output mapping.
- Rich app adapters, stable semantic locators across UI changes, and app-specific
  postconditions. Current support is generic AT-SPI, input, clipboard and D-Bus.
- Universal background/minimized control. EIS is focus-based; AT-SPI coverage and
  behavior vary by app. Locked-session behavior is deliberately not bypassed.
- Durable structured action/observation receipts. MCP tools currently return text;
  screenshot metadata is JSON embedded in that text, not an image attachment.
- Real KDE integration CI covering startup, toolkit differences, output scaling,
  background/minimized windows, and lock boundaries.

## Verification record for this change

New automated unit regressions cover startup failures and socket ownership,
accessibility ambiguity/deadlines/state checks, screenshot pipe blocking and
cleanup, and action-versus-observation reporting. These tests use native API
mocks and are not evidence of end-to-end KDE compatibility.

Local verification on 2026-11-05: 51 unit tests pass; repository-wide Ruff lint
and formatting checks and the documentation consistency check pass. Type checking
passes only with unresolved native imports excluded locally
(`ty check --ignore unresolved-import`); the full check is blocked by the missing
D-Bus bindings. CI installs the native development libraries before its full
source type check. The unit-test job installs Pillow for image metadata tests.

A subsequent non-root setup extracted official Debian KWin 6.3.6, Qt 6.8.2,
libei 1.3.901 and native D-Bus/AT-SPI bindings. Version/import checks passed.
Actual startup is blocked earlier: this environment denies AF_UNIX socket
creation with EPERM, so dbus-run-session cannot start, including under the
narrowly scoped execution approval. No socket or security restriction was
bypassed. Real compositor,
GTK/Qt app, background, minimized, and locked-session acceptance checks have not
been run in this environment. Do not treat historical milestone checkmarks in
ROADMAP.md as verification of this fork on current Plasma releases.

For each future app adapter, record the application/toolkit/KWin versions,
foreground/background/minimized/private-session outcome, observed postcondition,
and reproduction steps. Test locks only in an owned disposable session; retain
the expected denial rather than weakening authentication. Promote successful
agent workflows and failures into repeatable regression tests before advertising
new coverage.


## Acceptance matrix still to run on an owned KDE test machine

| Control path | Foreground | Background | Minimized | Locked |
| --- | --- | --- | --- | --- |
| Exact-ID KWin activation | Not run | Not run | Not run | Not run; do not infer visible focus |
| Named-output capture/mapping | Not run | Not run | Not run | Not run; retain permission denial |
| AT-SPI action/state verification | Not run | Not run | Not run | Not run; no unlock |
| Dolphin FileManager1 navigation | Not run | Not run | Not run | Not run; dispatch is not observation |

Use disposable documents/directories and duplicate window titles. Record KWin,
toolkit and application versions. Compare named-output screenshots with measured
logical geometry at 100%, 150%, and 200% scale, negative origins, rotated outputs
and hotplug. Test locks only through the normal lock/unlock UI with the owner.
Never enable live permission bypasses to make a test pass.

The follow-up implementation passes 78 local unit regressions, repository-wide
Ruff lint/format, type checking with unresolved native imports excluded, docs
consistency, and wheel/sdist builds. Generated KWin JavaScript is executed against
a mocked workspace in Node when available. These are not compositor integration
tests. Full source type checking is also exercised in dependency-equipped CI.
