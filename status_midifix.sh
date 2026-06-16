#!/bin/zsh
set -euo pipefail

INSTALLED_DIR="$HOME/Library/Application Support/midifix"
SOURCE_DIR="/Users/morganlavery/Documents/Midi Fix"
if [[ -d "$INSTALLED_DIR" ]]; then
  APP_DIR="$INSTALLED_DIR"
else
  APP_DIR="$SOURCE_DIR"
fi
LABEL="com.local.midifix"
GUI_DOMAIN="gui/$(id -u)"

launchctl print "$GUI_DOMAIN/$LABEL" 2>/dev/null | sed -n '1,40p' || {
  echo "INFINIGHT MidiSurgeon launch agent is not loaded."
}

echo
"$APP_DIR/.venv/bin/python" "$APP_DIR/midi_filter.py" blocks list
