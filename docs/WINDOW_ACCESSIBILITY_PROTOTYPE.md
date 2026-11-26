# Exact window accessibility prototype

The read-only `window_accessibility(window_id, max_depth=6, max_nodes=100,
include_labels=false)` command first resolves the compositor UUID to a
process/title cohort. Only direct AT-SPI top-level windows with the same
process and exact title participate. A duplicate GTK 3 title must have a
complete, unique one-to-one geometry match using width and height, not screen
position, active state, traversal order, or approximate sizes. Qt and other
bridges are not qualified for duplicate-title geometry. Equal or missing
geometry, an incomplete cohort, and a changed identity yield `unsupported`
with no tree.

The worker enumerates bounded PID/top-level metadata to prove the match; it
visits only the selected subtree. Data is a one-time observation, not an
identity token or security boundary against other same-user applications.
Labels are opt-in, restricted to a control/label role allowlist, and can
contain application-controlled text. Values, descriptions, editable/password
and document subtrees are not read out. Live reads fail closed if KDE's
ScreenSaver `GetActive` cannot establish an unlocked state before and after
the read. No input action, focus operation, screenshot, or fallback is used.

Verified with KDE/KWin 6.8.3 and GTK 3.24.52 through the CLI: both distinct
sizes in a same-title, same-process fixture yielded only their own controls;
an equal-size pair was refused. The private session kept active UUID stable
and verified password redaction, labels off by default, and node truncation.
An authorized live fixture check on the same versions kept active UUID and
pointer coordinates unchanged during both read operations. Launching the
fixture briefly activated a window; the live test then cleaned up the owned
process. These do not verify lock-screen access, action execution, Qt duplicate
titles, or every toolkit's geometry.
