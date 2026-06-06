#!/usr/bin/env bash
#
# Run ebayspy in the foreground for testing (Ctrl-C to stop).
# Useful before installing the always-on service.
#
#   scripts/run-local.sh          # runs the tracker (== ebayspy run)
#   scripts/run-local.sh check    # one-off poll, then exits
#   scripts/run-local.sh status   # show last-check status per seller

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BOOTSTRAP="${PYTHON:-/opt/anaconda3/bin/python3}"
VENV="$PROJECT_DIR/.venv"

if [ ! -x "$VENV/bin/ebayspy" ]; then
  "$PYTHON_BOOTSTRAP" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR"
fi

if [ "$#" -eq 0 ]; then set -- run; fi
exec "$VENV/bin/ebayspy" "$@"
