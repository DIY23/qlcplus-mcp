#!/bin/bash
# Assemble a runnable dev bundle from the build tree, without `make install`.
#
# QLC+ resolves everything relative to the executable on macOS:
#   resources -> <exe dir>/../Resources/<Name>
#   I/O plugins -> files in <exe dir> itself
# so a build-tree binary sees no fixtures and no plugins. This script lays out
#   <bundle>/Contents/MacOS/qlcplus-qml   (+ flat plugin dylibs)
#   <bundle>/Contents/Resources/<...>     (symlinks into src/resources)
set -euo pipefail
REPO=/Users/tom/Hermes/qlcplus
BUILD=$REPO/build
SRC=$REPO/src
BUNDLE=${1:-/tmp/QLC+Agent.app}

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

cp "$BUILD/qmlui/qlcplus-qml" "$BUNDLE/Contents/MacOS/"

# I/O plugins live in Contents/PlugIns (the loader asks for <exe>/../PlugIns)
mkdir -p "$BUNDLE/Contents/PlugIns/Audio"
find "$BUILD/plugins" -name "*.dylib" -exec cp {} "$BUNDLE/Contents/PlugIns/" \;
find "$BUILD/engine/audio/plugins" -name "*.dylib" -exec cp {} "$BUNDLE/Contents/PlugIns/Audio/" \; 2>/dev/null || true

# resources, named the way the macOS build expects (DATADIR = "Resources")
ln -s "$SRC/resources/fixtures"          "$BUNDLE/Contents/Resources/Fixtures"
ln -s "$SRC/resources/rgbscripts"        "$BUNDLE/Contents/Resources/RGBScripts"
ln -s "$SRC/resources/inputprofiles"     "$BUNDLE/Contents/Resources/InputProfiles"
ln -s "$SRC/resources/miditemplates"     "$BUNDLE/Contents/Resources/MidiTemplates"
ln -s "$SRC/resources/modifierstemplates" "$BUNDLE/Contents/Resources/ModifiersTemplates"
ln -s "$SRC/resources/colorfilters"      "$BUNDLE/Contents/Resources/ColorFilters"
ln -s "$SRC/resources/gobos"             "$BUNDLE/Contents/Resources/Gobos"
ln -s "$SRC/resources/meshes"            "$BUNDLE/Contents/Resources/Meshes"
ln -s "$SRC/resources/docs"              "$BUNDLE/Contents/Resources/Documents"
ln -s "$SRC/resources/Sample.qxw"        "$BUNDLE/Contents/Resources/Sample.qxw" 2>/dev/null || true
ln -s "$SRC/webaccess/res"               "$BUNDLE/Contents/Resources/Web"
mkdir -p "$BUNDLE/Contents/Resources/Translations"
find "$BUILD" -maxdepth 1 -name "*.qm" -exec cp {} "$BUNDLE/Contents/Resources/Translations/" \; 2>/dev/null || true

cp "$SRC/platforms/macos/Info.plist.qmlui" "$BUNDLE/Contents/Info.plist" 2>/dev/null || true

# The macOS install step normally copies the icon and substitutes the version; without both, the
# Dock shows a generic icon (and a placeholder version). Do it here so the dev bundle behaves.
cp "$SRC/resources/icons/qlcplus.icns" "$BUNDLE/Contents/Resources/qlcplus.icns"
VERSION=$(grep -m1 'set(APPVERSION' "$SRC/variables.cmake" | grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' | tr -d '"')
sed -i '' "s/__QLC_VERSION__/${VERSION:-dev}/g" "$BUNDLE/Contents/Info.plist"

# Launched via `open -na <bundle> --args ...`, macOS treats it as a real app (icon, Dock entry,
# activation); running Contents/MacOS/qlcplus-qml directly from a shell never will.
echo "identity: $(/usr/libexec/PlistBuddy -c 'Print CFBundleName' "$BUNDLE/Contents/Info.plist" 2>/dev/null) $VERSION"

echo "bundle ready: $BUNDLE"
ls "$BUNDLE/Contents/MacOS" | head -20
echo "resources: $(ls "$BUNDLE/Contents/Resources" | tr '\n' ' ')"
