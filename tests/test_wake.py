from datetime import datetime

from ebayspy import wake


def test_next_wake_time_formats_for_pmset() -> None:
    # pmset wants MM/DD/YYYY HH:MM:SS in local time.
    now = datetime(2026, 6, 15, 16, 30, 0)
    assert wake.next_wake_time(now, 6) == "06/15/2026 22:30:00"
    # Crosses midnight into the next day.
    assert wake.next_wake_time(datetime(2026, 6, 15, 20, 0, 0), 6) == "06/16/2026 02:00:00"


def test_arm_wake_argv_matches_sudoers_rule() -> None:
    # Must be exactly `/usr/bin/pmset schedule wake <when>` under sudo -n so it
    # matches the NOPASSWD rule installed by enable-wake-sudo.sh.
    argv = wake.arm_wake_argv("06/15/2026 22:30:00")
    assert argv == [
        "/usr/bin/sudo", "-n", "/usr/bin/pmset", "schedule", "wake", "06/15/2026 22:30:00",
    ]


def test_caffeinate_argv_waits_on_pid() -> None:
    assert wake.caffeinate_argv(4321) == ["/usr/bin/caffeinate", "-i", "-w", "4321"]
