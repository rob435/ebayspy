from ebayspy.demand import summarize


def _stats(**kw) -> dict:
    base = {
        "active_count": 0,
        "ended_in_window": 0,
        "new_in_window": 0,
        "avg_drops": 0.0,
        "history_days": 0.0,
        "lifespans": [],
        "active_ages": [],
    }
    base.update(kw)
    return base


def test_warming_up_until_enough_events() -> None:
    tag, detail = summarize(_stats(active_count=10, ended_in_window=1, history_days=10),
                            window_days=14, min_events=3)
    assert "warming up" in tag
    assert "Needs more data" in detail


def test_hot_market_when_clears_fast() -> None:
    # 14 disappeared in 14 days, only 10 active -> ~1/day, clears in ~10... tweak to Hot.
    tag, _ = summarize(
        _stats(active_count=5, ended_in_window=14, history_days=20,
               lifespans=[3 * 86400] * 5, active_ages=[2 * 86400] * 5, avg_drops=0.4),
        window_days=14, min_events=3,
    )
    assert "🔥" in tag and "Hot" in tag


def test_slow_market_when_barely_sells() -> None:
    tag, detail = summarize(
        _stats(active_count=200, ended_in_window=4, history_days=30),
        window_days=14, min_events=3,
    )
    assert "🐌" in tag and "Slow" in tag
    assert "to clear" in detail
