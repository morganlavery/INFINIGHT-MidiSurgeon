#!/bin/zsh
set -euo pipefail

LABEL="com.local.midifix"
OLD_LABEL="com.local.midi-cc-blocker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"

launchctl bootout "$GUI_DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootout "$GUI_DOMAIN" "$OLD_PLIST" 2>/dev/null || true
rm -f "$PLIST" "$OLD_PLIST"

echo "Uninstalled midifix launch agent."
