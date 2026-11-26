#!/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
b="${AX_BUILD_ROOT:-$(realpath ..)}"
mkdir -p "$b/build/plugins/"{generic,kwin/plugins}
q="$b/sdk/usr/include/qt6"
i="$b/sdk/usr/include"
moc="$b/sdk/usr/lib64/qt6/libexec/moc"
common=(-I"$i" -I"$q" -I"$q/QtCore" -I"$q/QtGui" -I"$q/QtDBus" -I"$q/QtQml" -I"$q/QtWidgets" -I"$q/QtGui/6.11.2/QtGui")
kw=(-I"$i/KF6" -I"$i/kwin" -I"$b/src/kwin/src" -I"$i/KF6/KWindowSystem" -I"$i/KF6/KConfigCore" -I"$i/KF6/KConfig" -I"$i/KF6/KCoreAddons" -I"$i/KF6/KGlobalAccel" -I"$i/KF6/KI18n" -I"$i/libdrm" -I"$i/drm" -I/usr/include/drm)
sc="${WAYLAND_SCANNER:-wayland-scanner}"
"$sc" server-header ax-link.xml ax-link-server.h
"$sc" client-header ax-link.xml ax-link-client.h
"$sc" private-code ax-link.xml ax-link-generated.c
case "${1:-all}" in
 qt|all)
   "$moc" "${common[@]}" qt_bridge.cpp -o qt_bridge.moc
   /usr/bin/gcc -I"$i" -fPIC -c ax-link-generated.c -o "$b/build/ax-link-generated.o"
   /usr/bin/g++ -std=c++23 -shared -fPIC -O1 -Wall -Wextra "${common[@]}" qt_bridge.cpp "$b/build/ax-link-generated.o"       -o "$b/build/plugins/generic/libjamesosaxlink.so" -l:libQt6Core.so.6 -l:libQt6Gui.so.6 -l:libwayland-client.so.0
 ;;
esac
case "${1:-all}" in
 fixture|all)
   /usr/bin/g++ -std=c++17 -fPIC -O1 "${common[@]}" lifecycle_fixture.cpp -o "$b/build/lifecycle_fixture" \
      -l:libQt6Widgets.so.6 -l:libQt6Gui.so.6 -l:libQt6Core.so.6
 ;;
esac
case "${1:-all}" in
 kwin|all)
   "$moc" "${common[@]}" "${kw[@]}" kwin_bridge.cpp -o kwin_bridge.moc
   /usr/bin/gcc -I"$i" -fPIC -c ax-link-generated.c -o "$b/build/ax-link-generated.o"
   /usr/bin/g++ -std=c++23 -shared -fPIC -O1 -Wall -Wextra "${common[@]}" "${kw[@]}" kwin_bridge.cpp "$b/build/ax-link-generated.o"       -o "$b/build/plugins/kwin/plugins/kwin-axlink.so" -l:libkwin.so.6 -l:libQt6Core.so.6 -l:libQt6Gui.so.6 -l:libQt6DBus.so.6 -l:libwayland-server.so.0
 ;;
esac
