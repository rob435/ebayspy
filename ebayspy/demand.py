"""Turn collected listing-lifecycle data into a demand / liquidity read.

The Browse API never reports sales, so demand is inferred from data we collect
ourselves over time: how quickly comparable listings *disappear* from the market
(≈ sold or pulled), how long the survivors have been sitting, and how often
sellers cut their prices. None of these is conclusive alone, so the read is
deliberately hedged with a confidence gate — it stays "warming up" until enough
disappearances and enough history have accrued.

Signals combined:
  * clearance rate — disappearances per week, and how many days the current
    supply would last at that rate (days-to-clear) → the headline liquidity band;
  * time-to-sell — median lifespan of disappeared listings;
  * sitting age — median age of still-active listings (old stock = soft demand);
  * discount pressure — average number of price cuts per active listing.
"""

from __future__ import annotations

import statistics


def _median_days(seconds: list[float]) -> float | None:
    return statistics.median(seconds) / 86400 if seconds else None


def summarize(stats: dict, *, window_days: int, min_events: int) -> tuple[str, str]:
    """Return (tag, detail). ``tag`` is a one-line liquidity badge for an alert;
    ``detail`` is the fuller breakdown for an on-demand report."""
    active = stats["active_count"]
    ended = stats["ended_in_window"]
    history_days = stats["history_days"]
    confident = ended >= min_events and history_days >= min(window_days, 3)

    if not confident:
        tag = "📊 Demand: warming up"
        detail = (
            f"{tag} — tracking {active} active listings, {ended} disappeared in "
            f"{window_days}d over {history_days:.0f}d of history. Needs more data to call it."
        )
        return tag, detail

    daily = ended / window_days if window_days else 0
    per_week = daily * 7
    days_to_clear = (active / daily) if daily > 0 else None
    if days_to_clear is None:
        emoji, label = "🧊", "Cold"
    elif days_to_clear < 7:
        emoji, label = "🔥", "Hot"
    elif days_to_clear < 21:
        emoji, label = "🌤", "Warm"
    elif days_to_clear < 60:
        emoji, label = "📊", "Steady"
    else:
        emoji, label = "🐌", "Slow"

    # ``days_to_clear`` is None only in the Cold case (no disappearances); 0.0
    # means supply has already fully cleared (Hot). Guard on None so a cleared-out
    # market doesn't read as the contradictory "Hot … barely selling".
    clear_txt = (
        f"~{days_to_clear:.0f}d to clear supply"
        if days_to_clear is not None
        else "barely selling"
    )
    tag = f"{emoji} Demand: {label} ({per_week:.0f}/wk, {clear_txt})"

    bits = [tag]
    sell_days = _median_days(stats["lifespans"])
    if sell_days is not None:
        bits.append(f"median time-to-sell ≈ {sell_days:.0f}d")
    age_days = _median_days(stats["active_ages"])
    if age_days is not None:
        bits.append(f"median age of active ≈ {age_days:.0f}d")
    drops = stats.get("avg_drops") or 0
    if drops:
        bits.append(f"discount pressure ≈ {drops:.1f} price-cuts/listing")
    return tag, " · ".join(bits)
