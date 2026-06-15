"""Pure helpers for pricing a market watch and spotting below-market deals.

The Browse API only exposes *active asking prices*, not completed-sale prices,
so "market price" here is the median total cost (item + shipping) of the live
listings that match a watch. The median is computed twice: once over the raw
sample, then again after trimming items that sit far from that first median, so
stray parts/accessories/bundles do not drag the figure around.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable

from .matching import COLOURS, attributes, normalize_capacity
from .models import MarketItem

# Considered in reading order for the variant label. Each dimension is included
# only when it independently splits the market (see choose_cluster_dimensions),
# so a watch is priced per model-line *and* capacity when both matter, but not
# fragmented on an attribute (e.g. colour) that does not move the price.
CLUSTER_DIMENSIONS = ("qualifier", "capacity", "colour")

# eBay aspect names (lowercased) that authoritatively carry each variant value.
_CAPACITY_ASPECTS = (
    "storage capacity", "capacity", "hard drive capacity", "ssd capacity",
    "internal storage", "memory",
)
_COLOUR_ASPECTS = ("colour", "color")

# Items priced far from the rough median are usually a different product
# (a spare part, an accessory, or a multi-item bundle) rather than a comparable.
TRIM_LOW_RATIO = 0.4
TRIM_HIGH_RATIO = 2.5


def market_price(totals: Iterable[float]) -> float | None:
    """Return a trimmed median of total prices, or None when there is no data."""
    values = sorted(value for value in totals if value and value > 0)
    if not values:
        return None
    rough = statistics.median(values)
    trimmed = [
        value
        for value in values
        if rough * TRIM_LOW_RATIO <= value <= rough * TRIM_HIGH_RATIO
    ]
    return statistics.median(trimmed) if trimmed else rough


def find_deals(
    items: list[MarketItem],
    price: float | None,
    discount_percent: float,
    min_ratio: float = TRIM_LOW_RATIO,
) -> list[MarketItem]:
    """Items priced at least ``discount_percent`` below ``price``.

    A floor of ``price * min_ratio`` excludes "too good to be true" listings that
    are almost always the wrong item (parts/accessories) or an outright scam,
    rather than a genuine bargain. Results are ordered cheapest first.
    """
    if not price or price <= 0:
        return []
    threshold = price * (1 - discount_percent / 100)
    floor = price * min_ratio
    deals = [item for item in items if floor <= item.total_price <= threshold]
    deals.sort(key=lambda item: item.total_price)
    return deals


def discount_percent_for(total_price: float, price: float) -> float:
    """How far below market (as a percentage) a given total price sits."""
    if not price or price <= 0:
        return 0.0
    return max(0.0, (1 - total_price / price) * 100)


def find_arbitrage(
    quotes: list[tuple[str, float, str]],
    convert: Callable[[float, str, str], float | None],
    threshold_percent: float,
    home_currency: str,
) -> dict | None:
    """Spot a cross-marketplace price gap.

    ``quotes`` is (marketplace, median_price, currency). Each is converted to a
    common currency; if the cheapest marketplace sits at least
    ``threshold_percent`` below the dearest, returns the buy/sell pair and gap.
    """
    converted = []
    for marketplace, price, currency in quotes:
        home_value = convert(price, currency, home_currency)
        if home_value and home_value > 0:
            converted.append((marketplace, price, currency, home_value))
    if len(converted) < 2:
        return None
    cheapest = min(converted, key=lambda q: q[3])
    dearest = max(converted, key=lambda q: q[3])
    if cheapest[0] == dearest[0]:
        return None
    gap = (dearest[3] - cheapest[3]) / dearest[3] * 100
    if gap < threshold_percent:
        return None
    return {
        "buy_marketplace": cheapest[0],
        "buy_price": cheapest[1],
        "buy_currency": cheapest[2],
        "buy_home": cheapest[3],
        "sell_marketplace": dearest[0],
        "sell_price": dearest[1],
        "sell_currency": dearest[2],
        "sell_home": dearest[3],
        "gap_percent": gap,
        "home_currency": home_currency,
    }


def estimate_resale_profit(
    market_price: float, buy_total: float, fee_percent: float, fee_fixed: float
) -> tuple[float, float]:
    """Estimate flip profit and ROI if bought at ``buy_total`` and resold at market.

    Resale proceeds are the market price net of the seller fee (a percentage plus
    a fixed per-order charge). Returns (profit, roi_percent); ROI is profit over
    the buy cost. Both can be negative.
    """
    net_resale = market_price * (1 - fee_percent / 100) - fee_fixed
    profit = net_resale - buy_total
    roi = (profit / buy_total * 100) if buy_total > 0 else 0.0
    return profit, roi


def _item_variant(item: MarketItem, dimension: str) -> str | None:
    """The variant value for a dimension, preferring eBay's structured aspects
    (authoritative, multi-category) over parsing the title."""
    aspects = {k.lower(): v for k, v in (item.aspects or {}).items()}
    if dimension == "capacity":
        for name in _CAPACITY_ASPECTS:
            if name in aspects and (cap := normalize_capacity(aspects[name])):
                return cap
    elif dimension == "colour":
        for name in _COLOUR_ASPECTS:
            if name in aspects:
                colours = {t for t in aspects[name].lower().split() if t in COLOURS}
                if colours:
                    return ",".join(sorted(colours))
    return attributes(item.title).get(dimension)  # type: ignore[return-value]


def choose_cluster_dimensions(
    items: list[MarketItem],
    specified: set[str],
    *,
    min_sample: int,
    min_dispersion: float,
) -> list[str]:
    """Variant dimensions worth pricing separately (possibly several, or none).

    A dimension qualifies when the query left it open, at least two of its values
    are each well-populated (``min_sample``), and their medians actually differ
    (``min_dispersion``) — i.e. the split is both reliable and price-relevant.
    Items are then clustered by the *combination* of qualifying dimensions, so a
    phone is priced per model-line and capacity at once, while a price-irrelevant
    attribute (e.g. colour) or thin data never fragments the sample.
    """
    chosen = []
    for dimension in CLUSTER_DIMENSIONS:
        if dimension in specified:
            continue
        groups: dict[str, list[MarketItem]] = {}
        for item in items:
            value = _item_variant(item, dimension)
            if value:  # qualifier is always present ("base"); capacity/colour may be None
                groups.setdefault(value, []).append(item)
        populated = {v: g for v, g in groups.items() if len(g) >= min_sample}
        if len(populated) < 2:
            continue
        medians = [statistics.median([i.total_price for i in g]) for g in populated.values()]
        high, low = max(medians), min(medians)
        if high > 0 and (high - low) / high >= min_dispersion:
            chosen.append(dimension)
    return chosen


def variant_label(item: MarketItem, dimensions: list[str]) -> str:
    """Readable variant label for an item over the chosen dimensions.

    The plain base model and unknown attributes contribute nothing, so a base
    256GB reads "256gb" while a Pro Max 256GB reads "pro max · 256gb".
    """
    parts = []
    for dimension in dimensions:
        value = _item_variant(item, dimension)
        if dimension == "qualifier":
            if value and value != "base":
                parts.append(value)
        elif value:
            parts.append(value)
    return " · ".join(parts)


def cluster_by_variant(
    items: list[MarketItem], dimensions: list[str]
) -> dict[str, list[MarketItem]]:
    """Group items by their combined variant label (one bucket when no dimensions)."""
    if not dimensions:
        return {"": list(items)}
    clusters: dict[str, list[MarketItem]] = {}
    for item in items:
        clusters.setdefault(variant_label(item, dimensions), []).append(item)
    return clusters
