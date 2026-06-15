#!/bin/zsh
APP_DIR="${0:A:h}"
cd "$APP_DIR" || exit 1
exec "$APP_DIR/.venv/bin/python" -u \
  "$APP_DIR/midi_filter.py" \
  filter \
  --input "Launch Control XL" \
  --output "Launch Control XL Filtered" \
  --block-file "$APP_DIR/blocked_controls.txt" \
  --reload-interval 1 \
  --report 5
