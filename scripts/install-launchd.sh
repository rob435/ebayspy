#!/usr/bin/env bash
#
# Install ebayspy as an always-on macOS LaunchAgent.
# - Builds an isolated .venv and installs the package
# - Validates .env (refuses to start without a Telegram token, to avoid a crash loop)
# - Writes ~/Library/LaunchAgents/com.ebayspy.tracker.plist
# - Starts it now and on every login; restarts it automatically if it crashes
#
# Re-run this any time to pick up code or config changes.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BOOTSTRAP="${PYTHON:-/opt/anaconda3/bin/python3}"
LABEL="com.ebayspy.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"

echo "==> Project: $PROJECT_DIR"

# 1. venv + install (idempotent)
if [ ! -x "$VENV/bin/ebayspy" ]; then
  echo "==> Creating venv and installing ebayspy..."
  "$PYTHON_BOOTSTRAP" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR"
else
  echo "==> venv present, refreshing install..."
  "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR"
fi

# 2. .env validation
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env  then fill it in."
  exit 1
fi

read_env() { grep -E "^$1=" "$PROJECT_DIR/.env" | head -n1 | cut -d= -f2- | tr -d '[:space:]'; }

token="$(read_env TELEGRAM_BOT_TOKEN || true)"
if [ -z "$token" ]; then
  cat <<'EOF'

ERROR: TELEGRAM_BOT_TOKEN is empty in .env.

  1. In Telegram, message @BotFather and send: /newbot
  2. Follow the prompts; copy the token it returns (like 123456789:AA...).
  3. Edit .env and set:  TELEGRAM_BOT_TOKEN=123456789:AA...
  4. Re-run this script.

Not starting the service yet (it would crash-loop without a token).
EOF
  exit 1
fi

app_id="$(read_env EBAY_APP_ID || true)"
secret="$(read_env EBAY_CLIENT_SECRET || true)"
if [ -z "$app_id" ] || [ -z "$secret" ]; then
  echo "ERROR: EBAY_APP_ID and EBAY_CLIENT_SECRET must be set in .env."
  exit 1
fi

# 3. logs
mkdir -p "$LOG_DIR"

# 4. write the LaunchAgent plist with absolute paths for this machine
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/ebayspy</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/ebayspy.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/ebayspy.err.log</string>
</dict>
</plist>
EOF
echo "==> Wrote $PLIST"

# 5. (re)load into the user GUI domain
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true

echo "==> Service loaded. Status:"
launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E '(state|pid) =' || launchctl list | grep ebayspy || true

cat <<EOF

Done. ebayspy is running and will start automatically every time you log in.

  Live log:   tail -f "$LOG_DIR/ebayspy.out.log"
  Errors:     tail -f "$LOG_DIR/ebayspy.err.log"
  Stop/remove: ./scripts/uninstall-launchd.sh

Next: open Telegram, send /start to your bot, then /add <sellername>.
EOF
