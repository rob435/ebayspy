"""Heuristic scam/risk scoring for a candidate deal.

A deal that looks too good often is. This combines the cheap signals we already
have — seller feedback, how far below market the price sits, and where the item
ships from — into a 0–100 risk score plus human-readable reasons, so the buyer
is warned before acting (and the worst offenders can be suppressed entirely).
"""

from __future__ import annotations

from .models import MarketItem


def assess(item: MarketItem, market_price: float, home_country: str = "") -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    feedback = item.seller_feedback_score
    if feedback is not None and feedback < 10:
        score += 35
        reasons.append(f"seller has very little feedback ({feedback})")
    elif feedback is not None and feedback < 50:
        score += 15
        reasons.append(f"seller fairly new ({feedback} feedback)")

    percent = item.seller_feedback_percent
    if percent is not None and 0 < percent < 95:
        score += 20
        reasons.append(f"only {percent:g}% positive feedback")

    if market_price and item.total_price < market_price * 0.55:
        score += 30
        reasons.append("price is suspiciously far below market")

    if (
        home_country
        and item.item_location
        and item.item_location.upper() != home_country.upper()
    ):
        score += 25
        reasons.append(f"ships from {item.item_location}")

    return min(score, 100), reasons
