# Exact Qt accessibility window registration (staging prototype)

The bridge pairs a Qt accessibility root with its actual Wayland surface,
not with a caption, size, focused window, or enumeration order. Tests use
version-matched dynamically loaded Qt and KWin companion plugins and unmodified
native applications. Nothing here enables the feature in a regular session.

## Authority and lifecycle

- Qt publishes a numeric root identifier on a Wayland connection that owns
  the surface. KWin checks the surface, compositor window, mapping state,
  and process credentials before accepting it.
- The lookup belongs to the compositor's existing script route. Calling the
  broker directly from another D-Bus client provides no mapping. KWin's
  actual lock state blocks lookup. Unknown, absent, duplicate, cross-process,
  and closed registrations produce no tree.
- The reader checks the selected session's AT-SPI bus and the actual D-Bus
  provider PID. The worker verifies the same unique top-level root twice.
  The engine rechecks the compositor link and its generation before releasing
  the result. A hide/unmap clears the old generation and a subsequent mapping
  must publish again, even if Qt keeps the same object path.
- Qt publishes only when its accessibility engine is active. The staged
  harness turns it on for its own processes only. A bounded poll runs only
  after show/surface/activation events and stops after confirmation. No
  text, input, action or screen-lock permissions are added by registration.

## Reproduction

Used KWin 6.8.3, Qt 6.11.2 and native Fedora 45 KDE binaries. Use the matching
KWin source tag for the internal Wayland server header, and verify the Fedora
development RPMs before extracting them into a private SDK root. There is no
host installation step. The SDK includes Qt base (normal and private),
Qt Declarative, Wayland, KWin, KWindowSystem, KConfig, and their version-matched
compiler headers (including Vulkan/DRM). Qt's moc lives under the extracted
SDK. The build also uses the system runtime libraries on this distribution.

Expected private layout below `AX_BUILD_ROOT`: `sdk/usr` contains the extracted
development files, `src/kwin` holds matching KWin source, and `mcp/src`
contains the test checkout's Python packages (or set `AX_MCP_SRC`). Run
`compile.sh all` with that build root and a Wayland scanner available. Run
`private.sh fixture`, `private.sh kate`, `private.sh dolphin`,
`private.sh quick`, `private.sh lock`, or `private.sh no-publisher`. The harness creates private
Wayland, D-Bus, configuration, and app homes. It closes only its own fixture
windows. The lock case denies the compositor access to the physical system bus.

## Release limits

Not a JamesOS package yet. The local plugin ABI pins are deliberate; do not
strip the private-runtime guard or let this auto-enable. A packaged opt-in
will require a reviewed deployment/rollback path and regression checks across
runtime updates. Applications that do not publish (including non-Qt or
unverified sandboxed providers) remain unsupported by this exact path.

## Version-guarded private package and rollback

Build the companion libraries with the matched SDK as above, then use
`package.py build BUILD/plugins PACKAGE_DIR RELEASE_ID` and
`package.py install STORE_DIR PACKAGE_DIR`. Use freshly created private
directories for the package and store. Nothing is loaded unless a release is
selected and a caller explicitly supplies `AX_PACKAGE_ROOT=STORE_DIR` to the
private harness. The launcher verifies the exact installed RPM versions for
KWin, Qt base and Qt Wayland and validates the two installed plugin checksums
before starting the compositor. A version or checksum mismatch does not
change the selection. Backend and app permissions remain unchanged.

Use `package.py status STORE_DIR` to inspect the active and prior selections,
`package.py rollback STORE_DIR` to switch back atomically, or
`package.py disable STORE_DIR` to clear opt-in. Retained release directories
are inert with opt-in cleared and contain only the test bundle. No system
package registration or desktop startup files are changed.

`verify-package.sh` reproduces first install, same-ABI release replacement,
wrong-version refusal for both components, checksum refusal before launch,
rollback and full opt-out. It also reruns positive and lock/lifecycle
conformance from installed plugins, rather than the build directory. All
runs use fresh private app homes, buses, store and sockets.

The `semantic` and `semantic-lock` modes of `private.sh` cover the optional
private exact-window [semantic actions](../../docs/EXACT_SEMANTIC_ACTIONS.md).
Build the fixture with `compile.sh fixture` before running them. These modes
use a private compositor and disposable Qt widgets only.
