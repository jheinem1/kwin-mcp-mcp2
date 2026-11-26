# Scoped semantic steps (isolated exact Qt runtime)

`window_run_steps(window_id, steps)` groups up to five bounded operations
against a single exact compositor window. It never evaluates code or invokes
a shell, D-Bus method name, arbitrary selector, coordinate input, or focus
fallback. This prototype depends on the exact-window semantic-action tool
and its explicitly enabled private Qt runtime.

Take a current `window_accessibility` snapshot with action handles enabled
and use its opaque element keys (not indexes, labels, raw paths or
single-use write handles) to build a plan. Example step schemas:

- `{"op":"set_text","key":"...","text":"fixture value"}`
- `{"op":"press","key":"...","verify_key":"...","state":"checked","present":true}`
- `{"op":"assert_state","key":"...","state":"checked","present":true}`

Every step refreshes the window's exact accessibility root, resolves the
same key once, and passes a fresh one-shot handle to the bounded primitive.
State assertions are read-only; button presses must have an observable
state change and text entry verifies the exact value internally. The
batch does not return entered text.

Invalid syntax rejects the entire plan before dispatch. Missing, stale,
duplicated or protected identities, the wrong window, a lock, refused or
unverified writes, and verification failure stop the batch. Receipts
identify completed and uncertain steps; already verified effects are not
undone. There are no automatic write retries. A deadline stops new effects
but does not undo an in-flight effect.

For the exact-window batch conformance case, run `private.sh scoped` with the
same disposable Qt fixture used by `private.sh semantic`.
