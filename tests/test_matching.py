from ebayspy import semantic
from ebayspy.matching import (
    attributes,
    canonicalize,
    content_tokens,
    discriminators,
    filter_comparable,
    fuzzy_ratio,
    is_comparable,
    model_numbers,
    register_aliases,
    specified_dimensions,
)
from ebayspy.models import MarketItem


def test_discriminators_pin_numbers_not_units() -> None:
    assert discriminators("iphone 13") == {"13"}
    assert discriminators("dyson airblade hu02") == {"hu02"}
    # storage/voltage units are not identity pins (handled elsewhere / incidental)
    assert discriminators("laptop 16gb 240v") == set()


def test_discriminator_pin_rejects_adjacent_model() -> None:
    # The number is the discriminator: an iPhone 13 watch must reject the 14.
    assert is_comparable("iphone 13", "Apple iPhone 13 128GB Blue")
    assert not is_comparable("iphone 13", "Apple iPhone 14 Pro 128GB")
    assert not is_comparable("ps5", "Sony PS4 Console")  # ps5->playstation 5, 5 pinned


def test_fuzzy_relevance_handles_word_order() -> None:
    assert round(fuzzy_ratio("north face nuptse jacket", "nuptse jacket north face"), 2) == 1.0
    # Reworded title passes on fuzzy even if exact coverage is lower.
    assert is_comparable(
        "north face nuptse jacket",
        "Mens Nuptse Jacket by The North Face Black",
        fuzzy_threshold=0.85,
    )


def test_semantic_relevance_folds_in_when_provided(monkeypatch) -> None:
    items = [MarketItem(item_id="x", title="totally reworded description", url="u",
                        seller="s", currency="GBP", item_price=1, total_price=1)]
    # No token/fuzzy overlap -> only semantics can keep it.
    monkeypatch.setattr(semantic, "similarities", lambda q, ts: [0.9])
    assert filter_comparable("north face nuptse jacket", items, semantic_threshold=0.6)
    monkeypatch.setattr(semantic, "similarities", lambda q, ts: [0.1])
    assert filter_comparable("north face nuptse jacket", items, semantic_threshold=0.6) == []


def test_semantic_absent_degrades_gracefully(monkeypatch) -> None:
    # When the model is unavailable, similarities returns None and rules carry on.
    monkeypatch.setattr(semantic, "similarities", lambda q, ts: None)
    items = [MarketItem(item_id="a", title="The North Face Nuptse Jacket", url="u",
                        seller="s", currency="GBP", item_price=1, total_price=1)]
    assert [i.item_id for i in filter_comparable(
        "north face nuptse jacket", items, semantic_threshold=0.6)] == ["a"]


def test_aliases_match_shorthand_and_full_name() -> None:
    assert canonicalize("PS5 console") == "playstation 5 console"
    # A ps5 query matches a "PlayStation 5" title and vice versa.
    assert is_comparable("ps5 console", "Sony PlayStation 5 Console Disc Edition")
    assert is_comparable("playstation 5 console", "Sony PS5 Console")


def test_register_aliases_extends_map() -> None:
    register_aliases([("rtx4090", "geforce rtx 4090")])
    assert canonicalize("rtx4090 founders") == "geforce rtx 4090 founders"


def test_model_numbers_extracts_codes_not_units() -> None:
    assert model_numbers("Dyson Airblade HU02 200-240V") == {"hu02"}
    assert model_numbers("Samsung Galaxy SM-G991B 256GB") == {"g991b"}
    assert model_numbers("plain words only") == set()


def test_content_tokens_drop_stopwords() -> None:
    assert content_tokens("Brand New Genuine Dyson Airblade") == {"dyson", "airblade"}


def test_exact_model_listing_is_comparable() -> None:
    assert is_comparable(
        "dyson airblade hu02", "Dyson Airblade HU02 Hand Dryer Nickel 200-240V | BNIB"
    )


def test_wrong_model_variant_rejected() -> None:
    assert not is_comparable("dyson airblade hu02", "Dyson Airblade HU03 Hand Dryer White")


def test_missing_model_rejected() -> None:
    # No model number in the title -> we cannot confirm it is the HU02.
    assert not is_comparable("dyson airblade hu02", "Dyson Airblade Hand Dryer V Nickel")


def test_accessories_and_parts_rejected() -> None:
    assert not is_comparable("dyson airblade hu02", "Wall Bracket for Dyson Airblade HU02")
    assert not is_comparable("dyson airblade hu02", "Replacement Filter for Dyson Airblade HU02")
    assert not is_comparable("dyson airblade hu02", "Dyson Airblade HU02 Charger Adapter")


def test_lots_and_damaged_rejected() -> None:
    assert not is_comparable("dyson airblade hu02", "Job Lot x3 Dyson Airblade HU02 Dryers")
    assert not is_comparable(
        "dyson airblade hu02", "Dyson Airblade HU02 Spares or Repair Faulty Not Working"
    )


def test_query_term_is_exempt_from_exclusion() -> None:
    # Watching the filter itself: "filter" in the query must not exclude it.
    assert is_comparable("dyson airblade hu02 filter", "Genuine Dyson Airblade HU02 Filter")


def test_units_are_not_hard_required() -> None:
    # "240v" is a spec/unit, not a model: a title without it still matches via coverage.
    assert is_comparable("dyson airblade hu02 240v", "Dyson Airblade HU02 Hand Dryer")


def test_coverage_for_queries_without_model_numbers() -> None:
    assert is_comparable("north face nuptse jacket", "The North Face Nuptse Jacket Black Medium")
    assert not is_comparable("north face nuptse jacket", "The North Face Backpack Rucksack")


def test_extra_excludes_reject_unwanted_variant() -> None:
    assert is_comparable("samsung tv qe55", "Samsung QE55 4K Smart TV")
    assert not is_comparable(
        "samsung tv qe55", "Samsung QE55 4K Smart TV Refurbished", extra_excludes=["refurbished"]
    )


def _item(item_id: str, title: str) -> MarketItem:
    return MarketItem(
        item_id=item_id,
        title=title,
        url=f"https://example.test/{item_id}",
        seller="s",
        currency="GBP",
        item_price=100.0,
        total_price=100.0,
    )


def test_attributes_extracts_capacity_colour_qualifiers() -> None:
    a = attributes("iPhone 13 Pro Max 256GB Midnight")
    assert a["capacity"] == "256gb"
    assert a["colour"] == "midnight"
    assert a["qualifiers"] == {"pro", "max"}
    assert a["qualifier"] == "pro max"  # canonical reading order
    assert attributes("iPhone 13 128GB")["qualifier"] == "base"

    assert attributes("iPhone 13 1 TB Blue")["capacity"] == "1tb"
    # Ambiguous capacity (two values) collapses to None for clustering.
    assert attributes("fits 128GB and 256GB models")["capacity"] is None


def test_specified_dimensions() -> None:
    assert specified_dimensions("iphone 13 256gb blue") == {"capacity", "colour"}
    assert specified_dimensions("iphone 13 pro") == {"qualifier"}
    assert specified_dimensions("iphone 13") == set()


def test_unspecified_qualifier_keeps_other_lines_for_clustering() -> None:
    # "iphone 13" now KEEPS the Pro / Mini lines so they can be priced as their
    # own clusters, rather than excluding them.
    assert is_comparable("iphone 13", "Apple iPhone 13 128GB Blue")
    assert is_comparable("iphone 13", "Apple iPhone 13 Pro 128GB")
    assert is_comparable("iphone 13", "Apple iPhone 13 Mini 256GB")


def test_specified_qualifier_is_pinned_exactly() -> None:
    # A Pro query matches only the Pro, not the plain 13 nor the Pro Max.
    assert is_comparable("iphone 13 pro", "Apple iPhone 13 Pro 256GB")
    assert not is_comparable("iphone 13 pro", "Apple iPhone 13 128GB")
    assert not is_comparable("iphone 13 pro", "Apple iPhone 13 Pro Max 256GB")


def test_specified_variant_attributes_required() -> None:
    assert is_comparable("iphone 13 blue", "Apple iPhone 13 128GB Blue")
    assert not is_comparable("iphone 13 blue", "Apple iPhone 13 128GB Red")
    assert is_comparable("iphone 13 256gb", "Apple iPhone 13 256GB Midnight")
    assert not is_comparable("iphone 13 256gb", "Apple iPhone 13 128GB Midnight")


def test_unspecified_variant_attributes_not_required() -> None:
    # No colour/capacity in the query -> any colour/capacity is comparable.
    assert is_comparable("iphone 13", "Apple iPhone 13 128GB Red")
    assert is_comparable("iphone 13", "Apple iPhone 13 512GB Green")


def test_filter_comparable_keeps_only_matches() -> None:
    items = [
        _item("a", "Dyson Airblade HU02 Hand Dryer"),
        _item("b", "Wall Bracket for Dyson Airblade HU02"),
        _item("c", "Dyson Airblade HU03 Hand Dryer"),
        _item("d", "Dyson Airblade HU02 Nickel BNIB"),
    ]
    kept = filter_comparable("dyson airblade hu02", items)

    assert [item.item_id for item in kept] == ["a", "d"]
