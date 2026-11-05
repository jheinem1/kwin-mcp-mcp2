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

## Current feature gaps

- Exact KWin window IDs, geometry/scale enumeration, and verified activation by ID.
- Reliable screenshot-to-global coordinate transforms, especially mixed DPI.
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

The development container has no `kwin_wayland`. Native dependency installation
also fails because Cairo development libraries are absent. Real compositor,
GTK/Qt app, background, minimized, and locked-session acceptance checks have not
been run in this environment. Do not treat historical milestone checkmarks in
ROADMAP.md as verification of this fork on current Plasma releases.

For each future app adapter, record the application/toolkit/KWin versions,
foreground/background/minimized/private-session outcome, observed postcondition,
and reproduction steps. Test locks only in an owned disposable session; retain
the expected denial rather than weakening authentication. Promote successful
agent workflows and failures into repeatable regression tests before advertising
new coverage.
