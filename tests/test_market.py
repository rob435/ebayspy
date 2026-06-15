from ebayspy.market import (
    choose_cluster_dimensions,
    cluster_by_variant,
    discount_percent_for,
    estimate_resale_profit,
    find_deals,
    market_price,
    variant_label,
)
from ebayspy.models import MarketItem


def _item(item_id: str, total: float, title: str | None = None) -> MarketItem:
    return MarketItem(
        item_id=item_id,
        title=title or f"Item {item_id}",
        url=f"https://example.test/itm/{item_id}",
        seller="seller",
        currency="GBP",
        item_price=total,
        total_price=total,
    )


def test_market_price_is_median_of_totals() -> None:
    assert market_price([100, 200, 300]) == 200


def test_market_price_trims_outliers() -> None:
    # The 5 and the 5000 are non-comparable (a spare part and a bundle) and
    # should not drag the median away from the ~200 cluster.
    price = market_price([5, 180, 200, 220, 5000])
    assert price == 200


def test_market_price_none_when_empty() -> None:
    assert market_price([]) is None
    assert market_price([0, -5]) is None


def test_find_deals_applies_threshold_and_floor() -> None:
    items = [
        _item("cheap-scam", 30),   # below the 40% floor -> excluded (wrong item)
        _item("great", 70),        # 30% below 100 -> deal
        _item("ok", 84),           # 16% below -> deal at 15% threshold
        _item("nodeal", 95),       # only 5% below -> not a deal
    ]
    deals = find_deals(items, price=100, discount_percent=15)

    assert [item.item_id for item in deals] == ["great", "ok"]


def test_find_deals_orders_cheapest_first() -> None:
    items = [_item("a", 80), _item("b", 60), _item("c", 70)]
    deals = find_deals(items, price=100, discount_percent=15)

    assert [item.item_id for item in deals] == ["b", "c", "a"]


def test_find_deals_empty_without_price() -> None:
    assert find_deals([_item("a", 10)], price=None, discount_percent=15) == []


def test_discount_percent_for() -> None:
    assert round(discount_percent_for(80, 100), 6) == 20
    assert discount_percent_for(100, 100) == 0
    assert discount_percent_for(120, 100) == 0  # never negative
    assert discount_percent_for(50, 0) == 0


def test_estimate_resale_profit() -> None:
    # Buy at 240, market 400, 12.8% + £0.30 fees.
    profit, roi = estimate_resale_profit(400, 240, 12.8, 0.30)
    assert round(profit, 2) == round(400 * 0.872 - 0.30 - 240, 2)
    assert profit > 0 and roi > 0
    # A break-even-ish buy yields near-zero / negative profit.
    loss, loss_roi = estimate_resale_profit(400, 360, 12.8, 0.30)
    assert loss < 0 and loss_roi < 0


def _phone(item_id: str, total: float, descriptor: str) -> MarketItem:
    return _item(item_id, total, title=f"Apple iPhone 13 {descriptor}")


def test_choose_cluster_dimensions_splits_on_populated_dispersed_capacity() -> None:
    items = [_phone(f"a{i}", 300 + i, "128GB") for i in range(6)]
    items += [_phone(f"b{i}", 400 + i, "256GB") for i in range(6)]

    assert choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12) == ["capacity"]


def test_choose_cluster_dimensions_splits_on_qualifier_and_capacity() -> None:
    # Base 128/256 and Pro 128/256, each populated and price-dispersed.
    items = [_phone(f"b1-{i}", 300 + i, "128GB") for i in range(6)]
    items += [_phone(f"b2-{i}", 400 + i, "256GB") for i in range(6)]
    items += [_phone(f"p1-{i}", 600 + i, "Pro 128GB") for i in range(6)]
    items += [_phone(f"p2-{i}", 700 + i, "Pro 256GB") for i in range(6)]

    dims = choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12)
    assert dims == ["qualifier", "capacity"]


def test_choose_cluster_dimensions_skips_thin_or_flat_splits() -> None:
    thin = [_phone(f"a{i}", 300, "128GB") for i in range(6)] + [_phone("b", 400, "256GB")]
    assert choose_cluster_dimensions(thin, set(), min_sample=5, min_dispersion=0.12) == []

    flat = [_phone(f"a{i}", 300, "128GB") for i in range(6)]
    flat += [_phone(f"b{i}", 305, "256GB") for i in range(6)]
    assert choose_cluster_dimensions(flat, set(), min_sample=5, min_dispersion=0.12) == []


def test_choose_cluster_dimensions_respects_specified() -> None:
    items = [_phone(f"a{i}", 300 + i, "128GB") for i in range(6)]
    items += [_phone(f"b{i}", 400 + i, "256GB") for i in range(6)]
    assert choose_cluster_dimensions(items, {"capacity"}, min_sample=5, min_dispersion=0.12) == []


def test_variant_label_drops_base_and_unknowns() -> None:
    assert variant_label(_phone("a", 300, "256GB"), ["qualifier", "capacity"]) == "256gb"
    assert variant_label(_phone("b", 700, "Pro Max 256GB"), ["qualifier", "capacity"]) == (
        "pro max · 256gb"
    )


def test_clustering_prefers_structured_aspects_over_title() -> None:
    # Title omits capacity, but eBay's structured aspect carries it authoritatively.
    item = MarketItem(
        item_id="a",
        title="Apple iPhone 13 (Unlocked)",
        url="u",
        seller="s",
        currency="GBP",
        item_price=400,
        total_price=400,
        aspects={"Storage Capacity": "256 GB", "Colour": "Midnight"},
    )
    clusters = cluster_by_variant([item], ["capacity"])
    assert list(clusters.keys()) == ["256gb"]
    assert variant_label(item, ["capacity", "colour"]) == "256gb · midnight"


def test_cluster_by_variant_groups_by_composite_label() -> None:
    items = [
        _phone("a", 300, "128GB"),
        _phone("b", 700, "Pro 256GB"),
        _phone("c", 310, "128GB"),
    ]
    clusters = cluster_by_variant(items, ["qualifier", "capacity"])

    assert {label: [i.item_id for i in g] for label, g in clusters.items()} == {
        "128gb": ["a", "c"],
        "pro · 256gb": ["b"],
    }
    assert cluster_by_variant(items, []) == {"": items}
