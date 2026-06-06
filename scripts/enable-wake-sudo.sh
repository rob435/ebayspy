#!/usr/bin/env bash
#
# One-time: grant passwordless `pmset schedule` so the wake-poll agent can arm
# wakes without a password prompt. Scoped to just `pmset schedule`.
#
# Run once:   sudo ./scripts/enable-wake-sudo.sh

set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo ./scripts/enable-wake-sudo.sh" >&2
  exit 1
fi

RULE_USER="${SUDO_USER:-root}"
F="/etc/sudoers.d/ebayspy-pmset"
printf '%s ALL=(root) NOPASSWD: /usr/bin/pmset schedule *\n' "$RULE_USER" > "$F"
chmod 440 "$F"
if visudo -cf "$F" >/dev/null; then
  echo "Installed $F (user: $RULE_USER)"
  echo "The wake-poll agent can now wake the Mac from sleep to poll."
else
  rm -f "$F"
  echo "sudoers validation failed; nothing installed." >&2
  exit 1
fi
