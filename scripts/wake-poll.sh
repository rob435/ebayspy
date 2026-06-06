#!/usr/bin/env bash
#
# Run by the com.ebayspy.wakepoll LaunchAgent at scheduled slots.
# Does ONE poll (holding the Mac awake just for the duration), then arms the
# next wake so a sleeping/battery Mac comes back up for the following poll.
#
# Arming the wake needs the one-time sudoers rule: scripts/enable-wake-sudo.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

WAKE_HOURS="${EBAYSPY_WAKE_HOURS:-6}"          # how far ahead to arm the next wake
NETWAIT="${EBAYSPY_WAKE_NETWAIT:-20}"          # seconds to let Wi-Fi reconnect after wake

{
  echo "=== wake-poll $(date '+%F %T') ==="
  sleep "$NETWAIT"
  # caffeinate -i keeps the Mac from idle-sleeping mid-poll; it releases when the poll ends.
  caffeinate -i "$PROJECT_DIR/.venv/bin/ebayspy" check
  NEXT="$(date -v+"${WAKE_HOURS}"H '+%m/%d/%Y %H:%M:%S')"
  if sudo -n /usr/bin/pmset schedule wake "$NEXT" 2>/dev/null; then
    echo "armed next wake: $NEXT"
  else
    echo "WARN: could not arm wake — run once:  sudo ./scripts/enable-wake-sudo.sh"
  fi
  echo "=== done $(date '+%F %T') ==="
} >> logs/wake-poll.log 2>&1
