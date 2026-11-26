#!/usr/bin/bash
set -euo pipefail
ulimit -c 0
umask 077
src="$(realpath "$(dirname "$0")")"
b="${AX_BUILD_ROOT:-$(realpath "$src/..")}"; b="$(realpath "$b")"
s="$(mktemp -d /tmp/kax-XXXXXX)"
echo "PRIVATE_ROOT=$s"
cd "$s"
mkdir -m700 r home config state cache data
export AX_WORK="$b" AX_STAGE="$s"
export XDG_RUNTIME_DIR="$s/r" HOME="$s/home" XDG_CONFIG_HOME="$s/config" XDG_CACHE_HOME="$s/cache" XDG_DATA_HOME="$s/data" XDG_STATE_HOME="$s/state"
export AXLINK_PRIVATE_RUNTIME="$XDG_RUNTIME_DIR"
if [[ -n "${AX_PACKAGE_ROOT:-}" ]]; then
  plugins="$(/usr/bin/python3 "$src/package.py" plugin-dir "$AX_PACKAGE_ROOT" "$XDG_RUNTIME_DIR")"
else
  plugins="$b/build/plugins"
fi
export WAYLAND_DISPLAY=axlink-0 QT_PLUGIN_PATH="$plugins" ATSPI_DBUS_IMPLEMENTATION=dbus-daemon
export PYTHONPATH="${AX_MCP_SRC:-$b/mcp/src}" KWIN_MCP_EXPLICIT_AX_TEST=1
unset DISPLAY DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS SESSION_MANAGER DBUS_STARTER_ADDRESS DBUS_STARTER_BUS_TYPE QT_QPA_GENERIC_PLUGINS XDG_SESSION_ID
printf '[Plugins]\nkwin-axlinkEnabled=true\n' > "$XDG_CONFIG_HOME/kwinrc"
timeout -k 6 120 /usr/bin/dbus-run-session -- /usr/bin/python3 -u "$src/integration.py" "${1:-fixture}"
if find "$XDG_RUNTIME_DIR" -name axlink-0 -type s -print -quit | grep -q .; then echo 'Private Wayland socket not cleaned'; exit 5; fi
echo "PRIVATE CLEANUP PASS"
