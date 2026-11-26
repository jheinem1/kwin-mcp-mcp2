# Exact staged semantic actions

This is a **private Qt staging feature** for KWin/Wayland. It does not enable
actions on the live desktop or for read-only heuristic matches. It requires
the opt-in registered Qt bridge, the staged compositor socket, a matching
compositor UUID and registration generation, and an accessible object owned by
the registered AT-SPI provider. It never uses focus, coordinates, key events,
clipboard, screenshot, title matching or a generic accessibility action as a
fallback.

1. Request `window_accessibility(window_id, include_action_handles=true)`.
   Eligible nodes include an opaque `action_handle` and `semantic_actions`.
   A verification-only node may have a handle with an empty actions list.
   Use a lower `max_nodes` if the actionable output exceeds the reply budget.
2. Call `window_semantic_action(window_id, action_handle, action, ...)`:
   - `press`: supply a `verify_handle` from the same snapshot,
     `verify_state` (`checked`, `expanded`, `selected`, or `enabled`), and
     `verify_present` (boolean). The condition must be different before
     dispatch and match afterward. This verifies the observed condition,
     not causation or an application's other side effects.
   - `set_text`: supply `text` for an initially empty editable, single-line
     and non-password field. The worker checks the resulting exact text
     without returning either value. Only disposable, non-secret text is
     appropriate; the caller still supplies text to the tool. When Qt
     does not publish the AT-SPI single-line state, the application must
     explicitly opt that widget in with the accessible identifier
     `jamesos.ax.single-line.v1`. Unmarked Qt fields are refused.
3. After an accepted invocation, even if its result is uncertain, take a
   fresh snapshot. Do not retry an `unverified` result blindly. A worker
   failure can leave `dispatched: null`; `refused` means no dispatch.
   A consumed snapshot cannot supply another action. Unused handles
   expire after 30 seconds, session changes, or eviction from the four
   most recent actionable snapshots.

Nodes also carry `action_key` in actionable snapshots. This is a non-actionable
correlation key, stable within one engine/session while the registration,
provider, object path, role and parent-index lineage match. It must not be
used if absent or after an identity change. Provider paths and app-reported
metadata are not a security boundary against malicious same-user code.
Compositor identity, lock, and accessibility cannot be read or changed
atomically across independent buses; the implementation rechecks immediately
around dispatch and does not claim a verified effect after an observed change.

The opt-in integration modes `semantic` and `semantic-lock` in the
[Qt staging instructions](../integrations/qt-axlink/README.md) use disposable
same-process, same-title and same-size Qt windows. They do not access personal
apps or modify the physical desktop.
