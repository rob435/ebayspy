"""Pure helpers for pricing a market watch and spotting below-market deals.

The statistics live in :mod:`ebayspy.pricing` (robust location, evidence
fusion, confidence). This module is the layer above it: which listings belong
to the same variant, which of them are deals against a fair value, and what a
flip would realistically net.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence

from . import pricing
from .matching import attributes, canonical_colours, normalize_capacity
from .models import MarketItem

# Considered in reading order for the variant label. Each dimension is included
# only when it independently splits the market (see choose_cluster_dimensions),
# so a watch is priced per model-line *and* capacity when both matter, but not
# fragmented on an attribute (e.g. colour) that does not move the price.
CLUSTER_DIMENSIONS = ("qualifier", "generation", "size", "capacity", "condition", "colour")

# eBay aspect names (lowercased) that authoritatively carry each variant value.
# "Memory" is deliberately absent: on phones and laptops it is RAM, not storage.
_CAPACITY_ASPECTS = (
    "storage capacity", "capacity", "hard drive capacity", "ssd capacity",
    "internal storage",
)
_COLOUR_ASPECTS = ("colour", "color")

# eBay conditionId → price-relevant bucket. New, open-box, refurbished and used
# units of one product trade at different levels; "parts" is never a comparable.
_CONDITION_BY_ID = {
    "1000": "new",
    "1500": "open box", "1750": "open box",
    "2000": "refurb", "2010": "refurb", "2020": "refurb", "2030": "refurb", "2500": "refurb",
    "2750": "used", "3000": "used", "4000": "used", "5000": "used", "6000": "used",
    "7000": "parts",
}

# Items priced far from the rough median are usually a different product
# (a spare part, an accessory, or a multi-item bundle) rather than a comparable.
TRIM_LOW_RATIO = pricing.BAND_LOW
TRIM_HIGH_RATIO = pricing.BAND_HIGH

# A new dimension must separate its groups by at least this many standard errors
# (as well as by ``min_dispersion``) before the market is split on it.
_SPLIT_SIGNIFICANCE_Z = 2.0
# ...and at least this share of the listings must actually state it. Splitting on
# an attribute most titles omit would leave most of the market unpriced.
_MIN_STATED_SHARE = 0.5
# ...and adding it must keep at least this share of the currently priceable
# listings priceable: a finer split is worthless if its cells are too thin.
_MIN_COVERAGE_KEPT = 0.8
# Model line, generation and size/grade are product identity, not flavour: a
# Fenix 5 is not a cheap Fenix 6, nor a 60ml bottle a cheap 100ml, nor a PSA 7 a
# cheap PSA 9. Once two of them are each well-populated and materially
# apart they are split even if the gap is statistically noisy and even if it
# leaves thinner ones unpriced — an unpriced listing is honest, a listing
# "54% under" a blend of other products is a false deal.
_IDENTITY_DIMENSIONS = frozenset({"qualifier", "generation", "size"})


def condition_bucket(condition: str = "", condition_id: str = "") -> str:
    """Price-relevant condition bucket: new / open box / refurb / used / parts.

    Trading cards reuse the generic ids for "Graded"/"Ungraded", so those two
    words win over the id. Unknown wording returns "" (unresolved).
    """
    text = (condition or "").strip().lower()
    if text in {"graded", "ungraded"}:
        return text
    bucket = _CONDITION_BY_ID.get(str(condition_id or "").strip())
    if bucket:
        return bucket
    if not text:
        return ""
    if "parts" in text or "not working" in text:
        return "parts"
    if "refurb" in text:
        return "refurb"
    if "open box" in text or (
        text.startswith("new") and any(word in text for word in ("other", "defect", "without"))
    ):
        return "open box"
    if text.startswith(("new", "brand new")):
        return "new"
    if any(word in text for word in ("used", "pre-owned", "preowned", "good", "acceptable",
                                     "excellent", "like new")):
        return "used"
    return ""


def market_price(totals: Iterable[float]) -> float | None:
    """Robust centre of total prices (clipped weighted median), or None when
    there is no data. See :func:`ebayspy.pricing.robust_estimate`."""
    estimate = pricing.robust_estimate(pricing.Obs(value) for value in totals if value)
    return estimate.price if estimate else None


def price_distribution(values: Iterable[float]) -> tuple[float, float, float] | None:
    """Return (p10, p50, p90) of the prices, or None when empty — shows the buyer
    the spread (how good the deal is) and the resale ceiling, not just the median."""
    ordered = sorted(value for value in values if value and value > 0)
    if not ordered:
        return None

    def percentile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * fraction
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)

    return percentile(0.10), percentile(0.50), percentile(0.90)


def deal_threshold(
    price: float, discount_percent: float, predictive_sd: float = 0.0, confidence_z: float = 0.0
) -> float:
    """Highest total price that counts as a deal against ``price``.

    The configured discount always applies; with ``confidence_z`` the edge must
    also clear that many predictive standard deviations, so thin or noisy
    markets demand a bigger gap. See :func:`ebayspy.pricing.deal_ceiling`.
    """
    plain = price * (1 - min(max(discount_percent, 0.0), 99.0) / 100)
    return min(plain, pricing.deal_ceiling(price, 0.0, predictive_sd, confidence_z))


def find_deals(
    items: list[MarketItem],
    price: float | None,
    discount_percent: float,
    min_ratio: float = TRIM_LOW_RATIO,
    *,
    predictive_sd: float = 0.0,
    confidence_z: float = 0.0,
) -> list[MarketItem]:
    """Items priced at least ``discount_percent`` below ``price``.

    A floor of ``price * min_ratio`` excludes "too good to be true" listings that
    are almost always the wrong item (parts/accessories) or an outright scam,
    rather than a genuine bargain. Results are ordered cheapest first.
    """
    if not price or price <= 0:
        return []
    threshold = deal_threshold(price, discount_percent, predictive_sd, confidence_z)
    floor = price * min_ratio
    deals = [item for item in items if floor <= item.total_price <= threshold]
    deals.sort(key=lambda item: item.total_price)
    return deals


def offer_candidates(
    items: list[MarketItem],
    price: float | None,
    list_discount_percent: float,
    offer_discount_percent: float,
    min_ratio: float = TRIM_LOW_RATIO,
    *,
    threshold: float | None = None,
) -> list[tuple[MarketItem, float]]:
    """Best-Offer listings whose LIST price isn't a deal but a plausible accepted
    offer would be. Returns (item, estimated_offer_total), cheapest list first.

    The estimate is taken off the listing's *own* asking price — sellers accept
    a discount on what they asked, not a number derived from the market — so it
    is ``item price × (1 - offer_discount_percent/100)`` plus its shipping. An
    item qualifies when its list total sits above the deal threshold (so it is
    not already a ``find_deals`` deal) and that estimate lands at or below it
    while staying above the too-good-to-be-true floor.
    """
    if not price or price <= 0:
        return []
    if threshold is None:
        threshold = deal_threshold(price, list_discount_percent)
    floor = price * min_ratio
    keep = 1 - min(max(offer_discount_percent, 0.0), 99.0) / 100
    deals = []
    for item in items:
        if not item.accepts_offers or item.total_price <= threshold:
            continue
        shipping = max(0.0, item.total_price - item.item_price)
        estimate = (item.total_price - shipping) * keep + shipping
        if floor <= estimate <= threshold:
            deals.append((item, estimate))
    deals.sort(key=lambda pair: pair[0].total_price)
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
    market_price: float,
    buy_total: float,
    fee_percent: float,
    fee_fixed: float,
    shipping: float = 0.0,
) -> tuple[float, float]:
    """Estimate flip profit and ROI if bought at ``buy_total`` and resold at
    ``market_price`` (the buyer's total).

    Proceeds are net of the seller fee (a percentage of the total plus a fixed
    per-order charge) and of the postage you pay to send it. Returns
    (profit, roi_percent); ROI is profit over the buy cost. Both can be negative.
    """
    profit = pricing.net_proceeds(market_price, fee_percent, fee_fixed, shipping) - buy_total
    roi = (profit / buy_total * 100) if buy_total > 0 else 0.0
    return profit, roi


def _item_variant(item, dimension: str) -> str | None:
    """The variant value for a dimension, preferring eBay's structured aspects
    (authoritative, multi-category) over parsing the title."""
    aspects = {k.lower(): v for k, v in (getattr(item, "aspects", None) or {}).items()}
    if dimension == "capacity":
        for name in _CAPACITY_ASPECTS:
            if name in aspects and (cap := normalize_capacity(aspects[name])):
                return cap
    elif dimension == "colour":
        for name in _COLOUR_ASPECTS:
            if name in aspects:
                colours = canonical_colours(set(aspects[name].lower().split()))
                if colours:
                    return ",".join(sorted(colours))
    elif dimension == "condition":
        return condition_bucket(
            getattr(item, "condition", ""), getattr(item, "condition_id", "")
        ) or None
    elif dimension in ("generation", "size"):
        # Relative to the watch's query, so they are stamped on the item by the
        # service rather than parsed here (see MarketItem.generation / .size).
        return getattr(item, dimension, "") or None
    return attributes(item.title).get(dimension)  # type: ignore[return-value]


def variant_values(
    item, dimensions: Sequence[str] = CLUSTER_DIMENSIONS
) -> dict[str, str | None]:
    """Every dimension's value for an item (None where it cannot be told)."""
    return {dimension: _item_variant(item, dimension) for dimension in dimensions}


def label_from_values(
    values: Mapping[str, str | None], dimensions: Sequence[str]
) -> str | None:
    """Variant label over ``dimensions``, or None when any of them is unknown.

    An item whose capacity (say) cannot be told is *unresolved*: it could be any
    variant, so it must never be priced as — or alerted against — one of them.
    """
    parts = []
    for dimension in dimensions:
        value = values.get(dimension)
        if not value:
            return None
        part = _label_part(dimension, value)
        if part:
            parts.append(part)
    return " · ".join(parts)


def _label_part(dimension: str, value: str) -> str:
    """How one dimension's value reads in a label ("" when it adds nothing)."""
    if dimension in ("qualifier", "generation") and value == "base":
        return ""
    return f"gen {value}" if dimension == "generation" and value.isdigit() else value


def variant_key(item, dimensions: Sequence[str]) -> str | None:
    """Cluster key for an item, or None when it is unresolved on a dimension."""
    return label_from_values(variant_values(item, dimensions), dimensions)


def variant_label(item, dimensions: Sequence[str]) -> str:
    """Readable variant label for an item over the chosen dimensions.

    The plain base model and unknown attributes contribute nothing, so a base
    256GB reads "256gb" while a Pro Max 256GB reads "pro max · 256gb".
    """
    parts = [
        part
        for dimension in dimensions
        if (value := _item_variant(item, dimension)) and (part := _label_part(dimension, value))
    ]
    return " · ".join(parts)


def _dimension_effect(
    items: Sequence[MarketItem], chosen: Sequence[str], dimension: str, min_sample: int
) -> tuple[float, bool] | None:
    """How much ``dimension`` moves the price *within* the cells already chosen.

    Returns (effect, significant), or None when no cell holds two well-populated
    values and so cannot show it either way.

    ``effect`` is the typical shift of a group's median from its cell's centre,
    in log units — for two equal groups, half the log of their price ratio. It
    is measured across *all* of a cell's groups rather than between the dearest
    and the cheapest: the gap between extremes grows with the number of groups
    even when nothing is going on, which is how a six-colour phone ends up
    "priced by colour". ``significant`` asks whether that between-group variance
    is at least four times what the sampling error of the medians alone would
    produce. Both are pooled over cells weighted by size, so one thin cell
    neither vetoes nor forces a split. Testing inside cells is what stops a
    correlated attribute (a colour that only ships on the Pro) from looking
    price-relevant on its own.
    """
    cells: dict[str, dict[str, list[float]]] = {}
    for item in items:
        cell = variant_key(item, chosen)
        value = _item_variant(item, dimension)
        if cell is None or not value:
            continue
        cells.setdefault(cell, {}).setdefault(value, []).append(item.total_price)
    between = noise = weight = 0.0
    for groups in cells.values():
        estimates = [
            estimate
            for prices in groups.values()
            if len(prices) >= min_sample
            and (estimate := pricing.robust_estimate(pricing.Obs(p) for p in prices))
        ]
        if len(estimates) < 2:
            continue
        size = sum(e.n for e in estimates)
        centre = sum(e.n * math.log(e.price) for e in estimates) / size
        between += sum(e.n * (math.log(e.price) - centre) ** 2 for e in estimates)
        noise += sum(e.n * e.se**2 for e in estimates)
        weight += size
    if not weight:
        return None
    return math.sqrt(between / weight), between >= _SPLIT_SIGNIFICANCE_Z**2 * noise


def _priced_share(items: Sequence[MarketItem], dimensions: Sequence[str], min_sample: int) -> float:
    """Of the listings that state every one of ``dimensions``, the share that
    lands in a cluster big enough to price. (Listings that omit an attribute are
    the stated-share rule's concern; this one is about cells being too thin.)"""
    clusters = cluster_by_variant(items, dimensions)
    placed = sum(len(group) for group in clusters.values())
    if not placed:
        return 0.0
    return sum(len(group) for group in clusters.values() if len(group) >= min_sample) / placed


def choose_cluster_dimensions(
    items: list[MarketItem],
    specified: set[str],
    *,
    min_sample: int,
    min_dispersion: float,
    previous: Sequence[str] = (),
) -> list[str]:
    """Variant dimensions worth pricing separately (possibly several, or none).

    Dimensions are considered in order and each is tested *conditionally* on the
    ones already chosen. A new dimension qualifies when the query left it open,
    most listings state it, its groups' prices differ both materially
    (``min_dispersion``, the relative gap between two groups' medians) and
    significantly, and splitting on it does not strand the market in clusters
    too small to price. Model line and generation — which name different
    products rather than flavours of one — need only the material gap. Items
    are then clustered by the *combination* of qualifying dimensions.

    ``previous`` makes the choice sticky: a dimension already in use is dropped
    only on positive evidence that it has stopped mattering (effect under half
    the threshold), never merely because this sample is too thin to show it.
    Without that, labels — and with them mutes, history and trends — would flip
    meaning from one check to the next. The one exception: a dimension that
    fewer than half the listings state is released, since holding on to it
    would leave the watch unable to price most of its market.
    """
    # Two equal groups whose medians differ by ``min_dispersion`` each sit this
    # far (in log units) from their common centre.
    threshold = -math.log(1 - min(max(min_dispersion, 0.0), 0.99)) / 2
    chosen: list[str] = []
    for dimension in CLUSTER_DIMENSIONS:
        if dimension in specified or not items:
            continue
        stated = sum(1 for item in items if _item_variant(item, dimension)) / len(items)
        if stated < _MIN_STATED_SHARE:
            continue  # sticky or not: it cannot place most of this market
        evidence = _dimension_effect(items, chosen, dimension, min_sample)
        if dimension in previous:
            keep = evidence is None or evidence[0] >= threshold / 2
        elif dimension in _IDENTITY_DIMENSIONS:
            keep = evidence is not None and evidence[0] >= threshold
        else:
            keep = (
                evidence is not None
                and evidence[0] >= threshold
                and evidence[1]
                and _priced_share(items, [*chosen, dimension], min_sample)
                >= _MIN_COVERAGE_KEPT * _priced_share(items, chosen, min_sample)
            )
        if keep:
            chosen.append(dimension)
    return chosen


def cluster_by_variant(items: Sequence, dimensions: Sequence[str]) -> dict[str, list]:
    """Group items by their combined variant label (one bucket when no dimensions).

    Items unresolved on a chosen dimension are left out: they cannot be priced
    like-for-like, and lumping them into a catch-all bucket would price a mix of
    variants as if it were one market.
    """
    if not dimensions:
        return {"": list(items)}
    clusters: dict[str, list] = {}
    for item in items:
        key = variant_key(item, dimensions)
        if key is not None:
            clusters.setdefault(key, []).append(item)
    return clusters
