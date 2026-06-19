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

_PMSET_FORMAT = "%m/%d/%Y %H:%M:%S"

# The wakepoll LaunchAgent fires via StartCalendarInterval at HH:05 on the
# 6-hour grid (00/06/12/18). We arm the pmset wake one minute earlier so the Mac
# is already awake when launchd runs the job. The wake time MUST land on the same
# grid as the LaunchAgent — arming "now + N hours" lets the two drift apart, so
# the Mac wakes at a moment nothing is scheduled and then sleeps through the real
# slot. Keep these in sync with scripts/install-wakepoll.sh.
LAUNCHD_MINUTE = 5
WAKE_LEAD_MINUTES = 1


def _aligned_after(now: datetime, hours: float) -> datetime:
    """The next wake instant on the 6-hour grid strictly after ``now``.

    Grid points are every ``hours`` hours anchored at local midnight (so the
    default 6 gives 00/06/12/18), at ``LAUNCHD_MINUTE - WAKE_LEAD_MINUTES`` past
    the hour. Aligning to the grid — rather than ``now + hours`` — is what keeps
    the armed wake matched to the LaunchAgent's calendar schedule.
    """
    period = max(1, int(round(hours)))
    minute = (LAUNCHD_MINUTE - WAKE_LEAD_MINUTES) % 60
    anchor = now.replace(minute=minute, second=0, microsecond=0)
    for day_offset in (0, 1):
        day = (anchor + timedelta(days=day_offset)).replace(hour=0)
        for hour in range(0, 24, period):
            candidate = day.replace(hour=hour)
            if candidate > now:
                return candidate
    return now + timedelta(hours=hours)  # pragma: no cover - unreachable for hours<=24


def next_wake_time(now: datetime, hours: float) -> str:
    """``pmset``-formatted timestamp of the next grid wake strictly after ``now``."""
    return _aligned_after(now, hours).strftime(_PMSET_FORMAT)


def next_wake_times(now: datetime, hours: float, count: int = 2) -> list[str]:
    """The next ``count`` grid wakes, as ``pmset`` timestamps.

    Arming more than one slot makes the chain self-healing: if a single wakepoll
    run is missed (e.g. the Mac stayed in dark-wake too briefly to launch the
    job), the following slot's wake is still armed and the schedule recovers.
    """
    times: list[str] = []
    cursor = now
    for _ in range(max(1, count)):
        nxt = _aligned_after(cursor, hours)
        times.append(nxt.strftime(_PMSET_FORMAT))
        cursor = nxt
    return times


def arm_wake_argv(when: str) -> list[str]:
    """argv that arms a single wake at ``when`` via the passwordless sudoers rule."""
    return [SUDO, "-n", PMSET, "schedule", "wake", when]


def caffeinate_argv(pid: int) -> list[str]:
    """argv for a ``caffeinate`` that prevents idle sleep until ``pid`` exits."""
    return [CAFFEINATE, "-i", "-w", str(pid)]
