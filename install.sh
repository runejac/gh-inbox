#!/usr/bin/env bash
# Install or remove the gh-inbox LaunchAgent.
# Usage: ./install.sh [interval-seconds]   (default 120)
#        ./install.sh --uninstall
set -euo pipefail

LABEL="local.gh-inbox"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$HOME/.local/state/gh-inbox"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL. State and logs stay in $STATE_DIR."
  exit 0
fi

INTERVAL="${1:-120}"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL >= 60 )) || {
  echo "Usage: ./install.sh [interval-seconds, 60 or more] | --uninstall" >&2; exit 1; }
for bin in python3 gh terminal-notifier; do
  command -v "$bin" >/dev/null || { echo "Install $bin first (brew install $bin)." >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "Log in first: gh auth login" >&2; exit 1; }

BIN_PATH="$(dirname "$(command -v gh)"):$(dirname "$(command -v terminal-notifier)"):/usr/bin:/bin"
mkdir -p "$STATE_DIR" "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(command -v python3)</string>
    <string>$DIR/gh_inbox.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$BIN_PATH</string></dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$STATE_DIR/gh-inbox.log</string>
  <key>StandardErrorPath</key><string>$STATE_DIR/gh-inbox.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL. It runs every ${INTERVAL}s."
echo "Alert log: $STATE_DIR/gh-inbox.log"
