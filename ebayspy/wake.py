"""Helpers for the scheduled wake-poll agent (``com.ebayspy.wakepoll``).

The agent runs ``ebayspy wakepoll``: one poll, then it arms the next wake so a
sleeping/battery Mac comes back up for the following slot.

It is run *directly* by launchd (ProgramArguments points at the venv's ``ebayspy``
binary) rather than via a shell script. That matters on macOS: a LaunchAgent's
shell cannot read or execute files under ~/Desktop (TCC "Operation not
permitted"), which is why the old ``wake-poll.sh`` agent silently failed with
exit 126. Executing the already-granted Python interpreter — exactly how the
``com.ebayspy.tracker`` agent runs — sidesteps that entirely.

Arming the wake needs the one-time passwordless sudoers rule installed by
``scripts/enable-wake-sudo.sh`` (grants ``/usr/bin/pmset schedule *``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Absolute paths: a LaunchAgent has a minimal PATH and sudo only matches the
# exact command in the sudoers rule (/usr/bin/pmset schedule *).
SUDO = "/usr/bin/sudo"
PMSET = "/usr/bin/pmset"
CAFFEINATE = "/usr/bin/caffeinate"


def next_wake_time(now: datetime, hours: float) -> str:
    """``pmset``-formatted timestamp ``hours`` ahead of ``now`` (MM/DD/YYYY HH:MM:SS)."""
    return (now + timedelta(hours=hours)).strftime("%m/%d/%Y %H:%M:%S")


def arm_wake_argv(when: str) -> list[str]:
    """argv that arms a single wake at ``when`` via the passwordless sudoers rule."""
    return [SUDO, "-n", PMSET, "schedule", "wake", when]


def caffeinate_argv(pid: int) -> list[str]:
    """argv for a ``caffeinate`` that prevents idle sleep until ``pid`` exits."""
    return [CAFFEINATE, "-i", "-w", str(pid)]
