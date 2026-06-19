from datetime import datetime

from ebayspy import wake


def test_next_wake_time_aligns_to_the_six_hour_grid() -> None:
    # Wakes land on the 00/06/12/18 grid (one minute before the LaunchAgent's
    # :05 slot), NOT at now+6h — otherwise the armed wake drifts away from the
    # calendar schedule and the Mac sleeps through the real slot.
    assert wake.next_wake_time(datetime(2026, 6, 15, 16, 30, 0), 6) == "06/15/2026 18:04:00"
    # After the last slot of the day, roll over to tomorrow's first slot.
    assert wake.next_wake_time(datetime(2026, 6, 15, 20, 0, 0), 6) == "06/16/2026 00:04:00"
    # A few minutes before a slot picks that slot.
    assert wake.next_wake_time(datetime(2026, 6, 15, 5, 0, 0), 6) == "06/15/2026 06:04:00"


def test_next_wake_time_skips_the_current_slot() -> None:
    # Exactly on a grid instant: arm the NEXT one, never re-arm the slot we're in.
    assert wake.next_wake_time(datetime(2026, 6, 15, 6, 4, 0), 6) == "06/15/2026 12:04:00"


def test_next_wake_times_returns_consecutive_grid_slots() -> None:
    # Arming several slots makes the wake chain self-healing if one run is missed.
    assert wake.next_wake_times(datetime(2026, 6, 15, 16, 30, 0), 6, 2) == [
        "06/15/2026 18:04:00",
        "06/16/2026 00:04:00",
    ]
    # count is floored at 1.
    assert wake.next_wake_times(datetime(2026, 6, 15, 16, 30, 0), 6, 0) == [
        "06/15/2026 18:04:00",
    ]


def test_arm_wake_argv_matches_sudoers_rule() -> None:
    # Must be exactly `/usr/bin/pmset schedule wake <when>` under sudo -n so it
    # matches the NOPASSWD rule installed by enable-wake-sudo.sh.
    argv = wake.arm_wake_argv("06/15/2026 22:30:00")
    assert argv == [
        "/usr/bin/sudo", "-n", "/usr/bin/pmset", "schedule", "wake", "06/15/2026 22:30:00",
    ]


def test_caffeinate_argv_waits_on_pid() -> None:
    assert wake.caffeinate_argv(4321) == ["/usr/bin/caffeinate", "-i", "-w", "4321"]
