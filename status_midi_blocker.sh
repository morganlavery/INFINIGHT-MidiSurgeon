#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
exec "$SCRIPT_DIR/status_midifix.sh"
