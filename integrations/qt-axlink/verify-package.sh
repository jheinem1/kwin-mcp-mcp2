#!/usr/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
export AX_BUILD_ROOT="$(realpath "${AX_BUILD_ROOT:?set the private build root}")"
export AX_MCP_SRC="$(realpath "${AX_MCP_SRC:?set the MCP source path}")"
s="$(mktemp -d /tmp/kax-package-XXXXXX)"
mkdir -m700 "$s"/{store,alpha,bravo,bad-kwin,bad-qt}
export AX_PACKAGE_ROOT="$s/store"
p() { /usr/bin/python3 package.py "$@"; }
quiet_case() {
  if ! ./private.sh "$1" >"$s/$2.log" 2>&1; then
    tail -60 "$s/$2.log"; return 1
  fi
  grep -E "PASS|owning process" "$s/$2.log"
}
untouched() {
  cmp "$s/initial.state" "$AX_PACKAGE_ROOT/selection.json"
}
refuse() {
  local match="$1"; shift
  if "$@" >"$s/reject.log" 2>&1; then
    echo "Unexpected successful action: $*"; return 1
  fi
  grep -q "$match" "$s/reject.log"
}
expect() {
  p status "$AX_PACKAGE_ROOT" | /usr/bin/python3 -c \
    'import json,sys; r=json.load(sys.stdin); assert r["active"]==json.loads(sys.argv[1]),r' "$1"
}
p build "$AX_BUILD_ROOT/build/plugins" "$s/alpha" alpha
p build "$AX_BUILD_ROOT/build/plugins" "$s/bravo" bravo
expect null
refuse 'No opt-in release selected' ./private.sh kate
p install "$AX_PACKAGE_ROOT" "$s/alpha"
expect '"alpha"'
cp "$AX_PACKAGE_ROOT/selection.json" "$s/initial.state"
quiet_case kate "installed-alpha"
quiet_case no-publisher "optout-alpha"
for item in kwin qt; do
  d="$s/bad-$item"
  cp -r "$s/alpha/plugins" "$d/"
  cp "$s/alpha/manifest.json" "$d/"
  /usr/bin/python3 - "$d" "$item" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])/'manifest.json'
data=json.loads(p.read_text())
data['id']='bad-'+sys.argv[2]
key='kwin' if sys.argv[2]=='kwin' else 'qt6-qtbase'
data['rpm'][key]='0.0.0-0.incompatible.x86_64'
p.write_text(json.dumps(data))
PY
done
refuse 'Incompatible kwin:' p install "$AX_PACKAGE_ROOT" "$s/bad-kwin"
untouched
refuse 'Incompatible qt6-qtbase:' p install "$AX_PACKAGE_ROOT" "$s/bad-qt"
untouched
refuse 'private KWin test runtime' p plugin-dir "$AX_PACKAGE_ROOT" "/run/user/$(id -u)"
echo 'PACKAGE PASS: KWin and Qt mismatch and non-private runtime rejected; active install unchanged'
p install "$AX_PACKAGE_ROOT" "$s/bravo"
expect '"bravo"'
quiet_case dolphin "installed-bravo"
quiet_case lock "locked-bravo"
printf bad >> "$AX_PACKAGE_ROOT/bravo/plugins/generic/libjamesosaxlink.so"
refuse 'Plugin checksum mismatch' ./private.sh kate
p rollback "$AX_PACKAGE_ROOT"
expect '"alpha"'
quiet_case fixture "rolled-back-alpha"
p disable "$AX_PACKAGE_ROOT"
expect null
refuse 'No opt-in release selected' ./private.sh kate
echo 'PACKAGE PASS: same-ABI upgrade, corrupt release refused at launch, rollback and opt-out verified'
echo "PACKAGE_EVIDENCE=$s"
