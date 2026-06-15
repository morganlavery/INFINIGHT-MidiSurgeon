#!/bin/zsh
set -euo pipefail

INSTALLED_DIR="$HOME/Library/Application Support/midifix"
SOURCE_DIR="${0:A:h}"

if [[ -d "$INSTALLED_DIR" ]]; then
  APP_DIR="$INSTALLED_DIR"
else
  APP_DIR="$SOURCE_DIR"
fi

exec "$APP_DIR/.venv/bin/python" "$APP_DIR/midi_filter.py" "$@"
