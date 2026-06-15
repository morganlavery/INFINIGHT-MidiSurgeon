#!/bin/zsh
set -euo pipefail

SOURCE_DIR="/Users/morganlavery/Documents/Midi Fix"
APP_DIR="$HOME/Library/Application Support/midifix"
OLD_APP_DIR="$HOME/Library/Application Support/Midi CC Blocker"
LOG_DIR="$HOME/Library/Logs/midifix"
LABEL="com.local.midifix"
OLD_LABEL="com.local.midi-cc-blocker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$APP_DIR" "$LOG_DIR"

if [[ -f "$APP_DIR/blocked_controls.txt" ]]; then
  cp "$APP_DIR/blocked_controls.txt" "$SOURCE_DIR/blocked_controls.txt"
elif [[ -f "$OLD_APP_DIR/blocked_controls.txt" ]]; then
  cp "$OLD_APP_DIR/blocked_controls.txt" "$SOURCE_DIR/blocked_controls.txt"
fi

rsync -a --delete \
  --exclude ".git/" \
  --exclude "*.log" \
  --exclude "midi-filter.pid" \
  "$SOURCE_DIR/" "$APP_DIR/"

chmod +x "$APP_DIR/midifix.sh" "$APP_DIR/run_midifix_filter.sh" \
  "$APP_DIR/install_midifix.sh" "$APP_DIR/uninstall_midifix.sh" \
  "$APP_DIR/status_midifix.sh" "$APP_DIR/midi_blocker.sh" \
  "$APP_DIR/run_midi_filter.sh" "$APP_DIR/install_midi_blocker.sh" \
  "$APP_DIR/uninstall_midi_blocker.sh" "$APP_DIR/status_midi_blocker.sh"

if screen -ls 2>/dev/null | grep -q '[.]midi-filter'; then
  screen -S midi-filter -X quit || true
fi

launchctl bootout "$GUI_DOMAIN" "$OLD_PLIST" 2>/dev/null || true
rm -f "$OLD_PLIST"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/run_midifix_filter.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$APP_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/midi-filter.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/midi-filter.err.log</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST" >/dev/null
launchctl bootout "$GUI_DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$GUI_DOMAIN" "$PLIST"
launchctl enable "$GUI_DOMAIN/$LABEL"
launchctl kickstart -k "$GUI_DOMAIN/$LABEL"

echo "Installed midifix launch agent."
echo "Virtual port: Launch Control XL Filtered"
echo "Blocklist: $APP_DIR/blocked_controls.txt"
echo "Logs: $LOG_DIR"
echo "UI: $APP_DIR/midifix.sh"
