#!/usr/bin/env bash
#
# Install the "sleep, wake to poll" LaunchAgent (com.ebayspy.wakepoll).
# Lets the Mac sleep normally and wakes it ~every 6h to run one poll.
# Pair with the one-time:  sudo ./scripts/enable-wake-sudo.sh
#
# The agent runs the venv's `ebayspy wakepoll` DIRECTLY (not via a shell script).
# On macOS a LaunchAgent's shell cannot read/exec files under ~/Desktop (TCC
# "Operation not permitted" -> exit 126, which is why the old wake-poll.sh agent
# silently failed); executing the already-granted Python interpreter, exactly as
# com.ebayspy.tracker does, sidesteps that.
#
# Re-run anytime; idempotent.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.ebayspy.wakepoll"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

if [ ! -x "$VENV/bin/ebayspy" ]; then
  echo "ERROR: $VENV/bin/ebayspy not found. Run scripts/install-launchd.sh first." >&2
  exit 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/ebayspy</string>
        <string>wakepoll</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key><string>$HOME</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>5</integer></dict>
        <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>5</integer></dict>
        <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>5</integer></dict>
        <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>5</integer></dict>
    </array>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>$LOG_DIR/wakepoll.agent.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/wakepoll.agent.log</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL — polls at 00:05, 06:05, 12:05, 18:05 (waking the Mac if asleep)."

# Arm the first wake now if the sudoers rule is already in place.
NEXT="$(date -v+6H '+%m/%d/%Y %H:%M:%S')"
if sudo -n /usr/bin/pmset schedule wake "$NEXT" 2>/dev/null; then
  echo "Armed first wake: $NEXT"
else
  echo
  echo ">>> ONE manual step to enable waking from sleep:"
  echo ">>>   sudo ./scripts/enable-wake-sudo.sh"
  echo ">>> then re-run:  ./scripts/install-wakepoll.sh"
  echo ">>> (Until then it only polls when the Mac is already awake at a slot — and"
  echo ">>>  remember: while plugged in your Mac never sleeps, so it runs continuously anyway.)"
fi
