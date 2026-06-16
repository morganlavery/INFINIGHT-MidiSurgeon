#!/bin/zsh
set -euo pipefail

APP_DIR="${0:A:h}"
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/midifix_desktop.py" "$@"
