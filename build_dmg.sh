#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h}"
BUILD_DIR="$ROOT_DIR/build/dmg"
DIST_DIR="$ROOT_DIR/dist"
APP_NAME="INFINIGHT MidiSurgeon.app"
APP_OUT="$BUILD_DIR/$APP_NAME"
APP_RESOURCES="$APP_OUT/Contents/Resources"
DMG_STAGING="$ROOT_DIR/build/dmg-staging"
DMG_PATH="$DIST_DIR/INFINIGHT-MidiSurgeon.dmg"

rm -rf "$BUILD_DIR" "$DMG_STAGING"
mkdir -p "$APP_OUT/Contents/MacOS" "$APP_RESOURCES/app" "$DIST_DIR" "$DMG_STAGING"

cp "$ROOT_DIR/midifix.app/Contents/Info.plist" "$APP_OUT/Contents/Info.plist"
cp "$ROOT_DIR/midifix.app/Contents/MacOS/midifix" "$APP_OUT/Contents/MacOS/midifix"
chmod +x "$APP_OUT/Contents/MacOS/midifix"

rsync -a "$ROOT_DIR/midifix.app/Contents/Resources/" "$APP_RESOURCES/"

rsync -a \
  "$ROOT_DIR/midifix.py" \
  "$ROOT_DIR/midifix_desktop.py" \
  "$ROOT_DIR/midi_filter.py" \
  "$ROOT_DIR/blocked_controls.txt" \
  "$ROOT_DIR/requirements.txt" \
  "$APP_RESOURCES/app/"

rsync -a "$ROOT_DIR/controller_templates" "$APP_RESOURCES/app/"

rsync -a \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  "$ROOT_DIR/.venv/" "$APP_RESOURCES/venv/"

cp -R "$APP_OUT" "$DMG_STAGING/$APP_NAME"
ln -s /Applications "$DMG_STAGING/Applications"

rm -f "$DMG_PATH"
hdiutil create \
  -volname "INFINIGHT MidiSurgeon" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

echo "$DMG_PATH"
