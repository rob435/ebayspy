#!/usr/bin/env bash
#
# Stop and remove the ebayspy LaunchAgent.
# Leaves the .venv, database (ebayspy.sqlite3), .env, and logs in place.

set -eu

LABEL="com.ebayspy.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Stopped and removed $LABEL."
echo "Kept: .venv, ebayspy.sqlite3, .env, logs/. Re-add with scripts/install-launchd.sh."
