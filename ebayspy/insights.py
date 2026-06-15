"""Summaries derived from real Marketplace Insights sold-sales data.

When the (Limited Release) Insights API is available, demand stops being an
inferred signal and becomes a measured one: actual completed sales over eBay's
90-day window. This module turns a list of SoldItem into a headline read.
"""

from __future__ import annotations

from .models import SoldItem

# Marketplace Insights item_sales covers the trailing 90 days.
WINDOW_DAYS = 90


def summarize_sold(items: list[SoldItem]) -> tuple[str, str]:
    """Return (tag, detail) describing real sales velocity over the last 90 days."""
    if not items:
        return "💰 Demand: no recorded sales (90d)", "No completed sales found in the last 90 days."
    quantity = sum(max(1, item.quantity) for item in items)
    per_week = quantity / WINDOW_DAYS * 7
    if per_week >= 7:
        emoji, label = "🔥", "Hot"
    elif per_week >= 2:
        emoji, label = "🌤", "Warm"
    elif per_week >= 0.5:
        emoji, label = "📊", "Steady"
    else:
        emoji, label = "🐌", "Slow"
    tag = f"{emoji} Demand: {label} — ~{per_week:.0f} sold/wk (real, {quantity} in 90d)"
    detail = (
        f"{tag}. Based on {len(items)} comparable completed sales from the "
        "Marketplace Insights API (actual sold prices, not asking prices)."
    )
    return tag, detail
