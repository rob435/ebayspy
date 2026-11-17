from ebayspy.market import (
    choose_cluster_dimensions,
    cluster_by_variant,
    condition_bucket,
    deal_threshold,
    label_from_values,
    variant_key,
    variant_values,
    discount_percent_for,
    estimate_resale_profit,
    find_deals,
    market_price,
    offer_candidates,
    price_distribution,
    variant_label,
)
from ebayspy.models import MarketItem


def _offer_item(item_id: str, total: float, *, offers: bool) -> MarketItem:
    return MarketItem(
        item_id=item_id,
        title=f"Item {item_id}",
        url=f"https://example.test/itm/{item_id}",
        seller="seller",
        currency="GBP",
        item_price=total,
        total_price=total,
        buying_options=("FIXED_PRICE", "BEST_OFFER") if offers else ("FIXED_PRICE",),
    )


def test_offer_candidates_flags_best_offer_above_threshold() -> None:
    # Market 100, list-deal threshold 85 (15% off). A BEST_OFFER item listed at 95
    # is not a list deal, but a 15% offer off ITS OWN price (80.75) would be ->
    # flagged. A non-offer item is not flagged, and an item already below the
    # threshold is find_deals' job.
    items = [
        _offer_item("offer", 95, offers=True),
        _offer_item("plain", 95, offers=False),
        _offer_item("already", 80, offers=True),
    ]
    flagged = offer_candidates(items, 100.0, 15, 15)
    assert [(i.item_id, est) for i, est in flagged] == [("offer", 80.75)]


def test_offer_estimate_tracks_the_listing_not_the_market() -> None:
    # The estimate is a discount off what THIS seller asked: nobody asking 130
    # takes 85 because the market median is 100. 99 -> 84.15 clears the 85 bar;
    # 110 -> 93.5 and 130 -> 110.5 do not.
    items = [
        _offer_item("near", 99, offers=True),
        _offer_item("above", 110, offers=True),
        _offer_item("far", 130, offers=True),
    ]
    flagged = offer_candidates(items, 100.0, 15, 15)
    assert [(i.item_id, round(est, 2)) for i, est in flagged] == [("near", 84.15)]


def test_offer_candidates_fire_with_default_settings() -> None:
    # Defaults are a 15% deal bar and a 10% expected offer. The old market-anchored
    # estimate (90) could never clear the bar (85), so the feature was inert. A
    # listing at 94 -> 84.6 now qualifies.
    flagged = offer_candidates([_offer_item("o", 94, offers=True)], 100.0, 15, 10)
    assert [i.item_id for i, _ in flagged] == ["o"]


def test_offer_discount_applies_to_item_price_not_shipping() -> None:
    item = MarketItem(
        item_id="s", title="Item", url="u", seller="x", currency="GBP",
        item_price=80, total_price=96, shipping_cost=16,
        buying_options=("FIXED_PRICE", "BEST_OFFER"),
    )
    # 10% off the 80 item price = 72, plus the 16 postage that is not negotiable.
    assert offer_candidates([item], 100.0, 10, 10) == [(item, 88.0)]


def test_offer_candidates_empty_without_price_or_items() -> None:
    assert offer_candidates([_offer_item("o", 95, offers=True)], None, 15, 15) == []
    assert offer_candidates([], 100.0, 15, 15) == []


def test_price_distribution() -> None:
    p10, p50, p90 = price_distribution(range(1, 101))  # 1..100
    assert round(p50) == 50
    assert p10 < p50 < p90
    assert price_distribution([]) is None
    assert price_distribution([42]) == (42, 42, 42)


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


# --- fair-value era: conditions, unresolved variants, sticky dimensions --------


def test_condition_bucket_from_id_and_text() -> None:
    assert condition_bucket("New", "1000") == "new"
    assert condition_bucket("Opened – never used", "1500") == "open box"
    assert condition_bucket("Seller refurbished", "2500") == "refurb"
    assert condition_bucket("Used", "3000") == "used"
    assert condition_bucket("For parts or not working", "7000") == "parts"
    # No id (older payloads): fall back to the wording.
    assert condition_bucket("For parts or not working") == "parts"
    assert condition_bucket("Certified - Refurbished") == "refurb"
    assert condition_bucket("New other (see details)") == "open box"
    assert condition_bucket("Brand New") == "new"
    assert condition_bucket("Pre-owned") == "used"
    assert condition_bucket("") == "" and condition_bucket("Mystery") == ""
    # Trading cards reuse generic ids for these, so the word wins.
    assert condition_bucket("Graded", "2750") == "graded"
    assert condition_bucket("Ungraded", "4000") == "ungraded"


def _conditioned(item_id: str, total: float, condition_id: str) -> MarketItem:
    return MarketItem(
        item_id=item_id, title="Sony WH-1000XM5", url="u", seller=f"s-{item_id}",
        currency="GBP", item_price=total, total_price=total, condition_id=condition_id,
    )


def test_condition_splits_new_from_used() -> None:
    # 12 used around 200 and 8 new around 300: one median would price neither.
    items = [_conditioned(f"u{i}", 200 + i, "3000") for i in range(12)]
    items += [_conditioned(f"n{i}", 300 + i, "1000") for i in range(8)]
    dims = choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12)
    assert dims == ["condition"]
    clusters = cluster_by_variant(items, dims)
    assert {label: len(group) for label, group in clusters.items()} == {"used": 12, "new": 8}
    # A watch that pins condition:new|used does not split on it again.
    assert choose_cluster_dimensions(
        items, {"condition"}, min_sample=5, min_dispersion=0.12
    ) == []


def test_unresolved_items_are_left_out_of_clusters() -> None:
    # A title with no capacity could be either variant; lumping those into a
    # catch-all bucket priced a mix of variants as one market and fired false deals.
    items = [_phone(f"a{i}", 300, "128GB") for i in range(6)]
    items += [_phone(f"b{i}", 400, "256GB") for i in range(6)]
    unknown = _phone("x", 300, "Unlocked")
    clusters = cluster_by_variant([*items, unknown], ["capacity"])
    assert set(clusters) == {"128gb", "256gb"}
    assert all(item.item_id != "x" for group in clusters.values() for item in group)
    assert variant_key(unknown, ["capacity"]) is None
    assert variant_key(unknown, []) == ""  # with no dimensions everything resolves


def test_label_from_values_roundtrips_stored_attributes() -> None:
    item = _phone("p", 700, "Pro Max 256GB Blue")
    values = variant_values(item)
    assert values["qualifier"] == "pro max" and values["capacity"] == "256gb"
    assert label_from_values(values, ["qualifier", "capacity"]) == "pro max · 256gb"
    assert label_from_values({"qualifier": "base", "capacity": "128gb"}, ["qualifier", "capacity"]) == "128gb"
    assert label_from_values({"qualifier": "base"}, ["qualifier", "capacity"]) is None


def test_ram_is_not_read_as_storage_capacity() -> None:
    item = MarketItem(
        item_id="a", title="Samsung Galaxy S23", url="u", seller="s", currency="GBP",
        item_price=400, total_price=400, aspects={"Memory": "8 GB"},
    )
    assert variant_key(item, ["capacity"]) is None


def test_correlated_attribute_does_not_split_on_its_own() -> None:
    # "Titanium" only ships on the Pro. Marginally, colour looks price-relevant;
    # inside each model line it makes no difference, so it must not be chosen.
    items = [_phone(f"b{i}", 300 + i, "Black") for i in range(8)]
    items += [_phone(f"p{i}", 600 + i, "Pro Titanium") for i in range(8)]
    assert choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12) == [
        "qualifier"
    ]


def test_chosen_dimensions_are_sticky_until_disproved() -> None:
    split = [_phone(f"a{i}", 300 + i, "128GB") for i in range(6)]
    split += [_phone(f"b{i}", 400 + i, "256GB") for i in range(6)]
    thin = split[:6] + split[6:8]  # 256GB drops to two listings this check
    kwargs = {"min_sample": 5, "min_dispersion": 0.12}
    # Fresh, the thin sample cannot justify a split...
    assert choose_cluster_dimensions(thin, set(), **kwargs) == []
    # ...but a split already in use survives a thin check (labels stay stable),
    assert choose_cluster_dimensions(thin, set(), previous=["capacity"], **kwargs) == ["capacity"]
    # and is dropped only on evidence that capacity has stopped moving the price.
    flat = [_phone(f"a{i}", 300 + i, "128GB") for i in range(6)]
    flat += [_phone(f"b{i}", 302 + i, "256GB") for i in range(6)]
    assert choose_cluster_dimensions(flat, set(), previous=["capacity"], **kwargs) == []


def test_split_needs_statistical_support_not_just_a_gap() -> None:
    # Two groups of five whose medians differ by 13%, but each scattered ±35%:
    # that gap is well inside the noise, so the market is not fragmented on it.
    noisy_a = [200, 260, 300, 350, 410]
    noisy_b = [230, 290, 340, 400, 470]
    items = [_phone(f"a{i}", p, "128GB") for i, p in enumerate(noisy_a)]
    items += [_phone(f"b{i}", p, "256GB") for i, p in enumerate(noisy_b)]
    assert choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12) == []


def test_deal_threshold_and_find_deals_respect_the_confidence_gate() -> None:
    assert deal_threshold(100, 15) == 85
    assert deal_threshold(100, 15, predictive_sd=0.30, confidence_z=1.0) < 75
    items = [_item("a", 84), _item("b", 70)]
    assert [i.item_id for i in find_deals(items, 100, 15)] == ["b", "a"]
    gated = find_deals(items, 100, 15, predictive_sd=0.30, confidence_z=1.0)
    assert [i.item_id for i in gated] == ["b"]  # 16% under is noise in a 30% market


def test_resale_profit_subtracts_postage() -> None:
    # Market 100, bought at 85: +1.90 on paper, a loss once you post it.
    paper, _ = estimate_resale_profit(100, 85, 12.8, 0.30)
    real, _ = estimate_resale_profit(100, 85, 12.8, 0.30, 10.0)
    assert round(paper, 2) == 1.90 and round(real, 2) == -8.10


def _generation(item_id: str, total: float, generation: str) -> MarketItem:
    return MarketItem(
        item_id=item_id, title="Amazon Echo Dot Smart Speaker", url="u", seller=f"s-{item_id}",
        currency="GBP", item_price=total, total_price=total, generation=generation,
    )


def test_unpinned_generations_are_priced_separately() -> None:
    # A watch on "echo dot": the 2nd, 3rd and 4th generation trade at different
    # levels. They are all comparables, but never one blended market.
    items = [_generation(f"a{i}", 20 + i * 0.2, "2") for i in range(6)]
    items += [_generation(f"b{i}", 30 + i * 0.2, "3") for i in range(8)]
    items += [_generation(f"c{i}", 45 + i * 0.2, "4") for i in range(7)]
    items += [_generation(f"d{i}", 31 + i, "base") for i in range(3)]  # none stated
    dims = choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12)
    assert dims == ["generation"]
    clusters = cluster_by_variant(items, dims)
    assert {label: len(group) for label, group in clusters.items()} == {
        "gen 2": 6, "gen 3": 8, "gen 4": 7, "": 3,
    }
    assert label_from_values({"generation": "3", "capacity": "32gb"}, ["generation", "capacity"]) == (
        "gen 3 · 32gb"
    )
    # Items the service has not stamped (no generation) never trigger the split.
    plain = [_phone(f"p{i}", 300 + i, "128GB") for i in range(12)]
    assert "generation" not in choose_cluster_dimensions(
        plain, set(), min_sample=5, min_dispersion=0.12
    )


def test_many_valued_attribute_does_not_split_on_the_gap_between_extremes() -> None:
    # Six colours, identical pricing, ordinary scatter. The dearest colour's
    # median will sit well above the cheapest's by chance alone; measured across
    # all the groups there is nothing there, so the market is not split by colour.
    import random

    rng = random.Random(3)
    items = []
    for colour in ("Black", "Silver", "Gold", "Red", "Blue", "Green"):
        for i in range(7):
            price = 200 * (1 + rng.uniform(-0.25, 0.25))
            items.append(_phone(f"{colour}{i}", price, f"64GB {colour}"))
    medians = {}
    for colour in ("black", "silver", "gold", "red", "blue", "green"):
        prices = sorted(i.total_price for i in items if colour in i.title.lower())
        medians[colour] = prices[len(prices) // 2]
    assert (max(medians.values()) - min(medians.values())) / max(medians.values()) > 0.12
    assert choose_cluster_dimensions(items, set(), min_sample=5, min_dispersion=0.12) == []


def test_split_that_would_strand_the_market_in_thin_cells_is_refused() -> None:
    # Capacity genuinely moves the price, but with four capacities over twelve
    # listings only one cell would be big enough to price. Better one honest wide
    # market than a split that prices a third of it.
    spec = [("32GB", 100, 5), ("64GB", 130, 3), ("128GB", 160, 2), ("256GB", 200, 2)]
    items = [
        _phone(f"{cap}-{i}", base + i, cap) for cap, base, count in spec for i in range(count)
    ]
    more = items + [_phone(f"64GB-x{i}", 131 + i, "64GB") for i in range(3)]
    kwargs = {"min_sample": 5, "min_dispersion": 0.12}
    # 5 + 6 of 15 priceable (73%) is under the 80% that must stay priceable...
    assert choose_cluster_dimensions(more, set(), **kwargs) == []
    # ...whereas a clean two-way split keeps everything priceable and is taken.
    clean = [i for i in more if "32GB" in i.title or "64GB" in i.title]
    assert choose_cluster_dimensions(clean, set(), **kwargs) == ["capacity"]


def test_product_identity_is_split_even_when_the_gap_is_noisy() -> None:
    # Two generations, six listings each, medians ~45% apart but each scattered
    # widely. For a flavour (capacity, colour) that would not be enough to split.
    # For a generation it is: these are different products, and blending them
    # makes every listing of the cheaper one look like a deal.
    older = [150, 190, 230, 260, 300, 380]
    newer = [280, 340, 400, 450, 520, 640]
    by_generation = [_generation(f"a{i}", p, "5") for i, p in enumerate(older)]
    by_generation += [_generation(f"b{i}", p, "6") for i, p in enumerate(newer)]
    kwargs = {"min_sample": 5, "min_dispersion": 0.12}
    assert choose_cluster_dimensions(by_generation, set(), **kwargs) == ["generation"]
    by_capacity = [_phone(f"a{i}", p, "128GB") for i, p in enumerate(older)]
    by_capacity += [_phone(f"b{i}", p, "256GB") for i, p in enumerate(newer)]
    assert choose_cluster_dimensions(by_capacity, set(), **kwargs) == []
