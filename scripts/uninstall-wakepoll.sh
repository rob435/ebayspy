#!/usr/bin/env bash
#
# Remove the wake-to-poll LaunchAgent and clear its scheduled wakes.
# Leaves the always-on run service (com.ebayspy.tracker) untouched.

set -eu

LABEL="com.ebayspy.wakepoll"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"

if sudo -n /usr/bin/pmset schedule cancelall 2>/dev/null; then
  echo "Cleared scheduled wakes."
else
  echo "Note: clear pending wakes manually with:  sudo pmset schedule cancelall"
fi
echo "Removed $LABEL."
echo "To also revoke the sudo rule:  sudo rm /etc/sudoers.d/ebayspy-pmset"
