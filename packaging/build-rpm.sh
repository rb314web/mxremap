#!/usr/bin/env bash
# Buduje pakiet RPM (na Fedorze: sudo dnf install rpm-build). Uruchom z dowolnego miejsca.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER=$(awk '/^Version:/{print $2}' "$ROOT/packaging/mxremap.spec")
TOP="${RPMTOP:-$HOME/rpmbuild}"
mkdir -p "$TOP"/{SOURCES,SPECS,RPMS,BUILD,SRPMS}
STAGE=$(mktemp -d); mkdir -p "$STAGE/mxremap-$VER/packaging"
cp "$ROOT"/mxremap.py "$ROOT"/mxremap_gui.py "$ROOT"/mxremap_tray.py "$STAGE/mxremap-$VER/"
cp "$ROOT"/packaging/{mxremap.sh,mxremap-engine.sh,mxremap.desktop,70-mxremap.rules,mxremap-uinput.conf} "$STAGE/mxremap-$VER/packaging/"
tar -C "$STAGE" -czf "$TOP/SOURCES/mxremap-$VER.tar.gz" "mxremap-$VER"
cp "$ROOT/packaging/mxremap.spec" "$TOP/SPECS/"
rpmbuild --define "_topdir $TOP" -bb "$TOP/SPECS/mxremap.spec"
echo; echo "Gotowy pakiet:"; find "$TOP/RPMS" -name 'mxremap-*.rpm'
