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
    # A hyphenated model code is unified into one token (SM-G991B -> smg991b) so
    # its hyphen/no-hyphen written forms all match; the voltage stays a non-code.
    assert model_numbers("Samsung Galaxy SM-G991B 256GB") == {"smg991b"}
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


def test_colour_spelling_synonyms_fold_together() -> None:
    # UK "grey" and US "gray" name the same colour; both must canonicalize so a
    # query in one spelling still matches a listing in the other.
    assert attributes("iPhone 13 Gray")["colour"] == "grey"
    assert attributes("iPhone 13 Grey")["colour"] == "grey"
    assert is_comparable("iphone 13 grey", "Apple iPhone 13 128GB Gray")
    assert is_comparable("iphone 13 gray", "Apple iPhone 13 128GB Grey")


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


def test_ordinal_folds_to_digit() -> None:
    # A human reads "AirPods Pro 2nd Gen" as the "2" the query asked for.
    assert canonicalize("airpods pro 2nd generation") == "airpods pro 2 generation"
    assert is_comparable("airpods pro 2", "Apple AirPods Pro 2nd Gen")
    assert is_comparable("ipad 9th", "Apple iPad 9 Wi-Fi 64GB Space Grey")
    # The fold must not collapse genuinely different generations.
    assert not is_comparable("airpods pro 2", "Apple AirPods Pro 3rd Gen")


def test_plural_singular_folds_in_coverage() -> None:
    # "games" vs "game" must not cost a coverage point.
    assert is_comparable("playstation 5 games", "PlayStation 5 Game - Spider-Man 2")
    assert is_comparable("north face jackets", "The North Face Nuptse Jacket Black")


def test_model_number_separator_tolerance() -> None:
    # A hyphen/space difference in a real model code must not drop a comparable.
    assert is_comparable("sony wh-1000xm5", "Sony WH1000XM5 Headphones")
    assert is_comparable("samsung sm-g991b", "Samsung Galaxy SMG991B 128GB")
    # ...but a different code is still rejected, and bare numbers stay exact so
    # "13" never matches inside a longer number like "2013".
    assert not is_comparable("sony wh-1000xm5", "Sony WH-1000XM4 Headphones")
    assert not is_comparable("iphone 13", "Apple iPhone 2013 Vintage Model")


def test_oled_is_a_distinct_model_line() -> None:
    # A pinned OLED query must reject the cheaper Lite (would be a false deal).
    assert is_comparable("nintendo switch oled", "Nintendo Switch OLED Model White")
    assert not is_comparable("nintendo switch oled", "Nintendo Switch Lite Coral")
    # A plain Switch query still KEEPS both so they can be priced as clusters.
    assert is_comparable("nintendo switch", "Nintendo Switch OLED White")
    assert is_comparable("nintendo switch", "Nintendo Switch Lite Coral")
    assert attributes("Nintendo Switch OLED")["qualifier"] == "oled"


def test_series_designator_pinned_both_ways() -> None:
    # Xbox Series X vs S share every word but the lone letter; pin it exactly.
    assert is_comparable("xbox series x", "Microsoft Xbox Series X 1TB Console")
    assert not is_comparable("xbox series x", "Xbox Series S 512GB")
    assert not is_comparable("xbox series s", "Xbox Series X 1TB")
    assert is_comparable("xbox one x", "Microsoft Xbox One X 1TB")
    assert not is_comparable("xbox one x", "Microsoft Xbox One S 500GB")


def test_mark_designator_pinned_both_ways() -> None:
    # "Mark II" / "mk2" / "Mark 2" denote one product; a base watch and a Mark II
    # watch must not cross-match (either direction would be a false deal).
    assert is_comparable("canon eos r6 mark ii", "Canon EOS R6 Mark II Body")
    assert is_comparable("canon eos r6 mark ii", "Canon EOS R6 mk2 Body")
    assert not is_comparable("canon eos r6 mark ii", "Canon EOS R6 Body")
    assert not is_comparable("canon eos r6", "Canon EOS R6 Mark II Body")
    assert not is_comparable("canon eos r6 mark ii", "Canon EOS R6 Mark III Body")


def test_plus_notation_is_the_plus_model_line() -> None:
    # "S21+" means the Plus model; tokenizing drops the symbol unless spelled out.
    assert is_comparable("samsung galaxy s21 plus", "Samsung Galaxy S21+ 128GB")
    assert not is_comparable("samsung galaxy s21 plus", "Samsung Galaxy S21 Ultra 5G")
    # A plain S21 query still keeps the Plus for clustering (it's a qualifier).
    assert is_comparable("samsung galaxy s21", "Samsung Galaxy S21+ 128GB")


def test_roman_generation_pinned_both_ways() -> None:
    # Sony A7 II / III / IV are different cameras; a bare roman generation must
    # pin exactly so an "A7 III" watch never catches the cheaper A7 II.
    assert is_comparable("sony a7 iii", "Sony Alpha A7 III Mirrorless Body")
    assert not is_comparable("sony a7 iii", "Sony Alpha A7 II Body")
    assert not is_comparable("sony a7 iii", "Sony Alpha A7 Body")
    # "Mark II" and "mk2" stay equivalent — the roman isn't double-counted.
    assert is_comparable("canon eos r6 mark ii", "Canon EOS R6 mk2 Body")


def test_hyphenated_words_survive_model_code_join() -> None:
    # Ordinary hyphenated words (no digit) must not be glued together.
    assert is_comparable("mens t shirt large", "Mens T-Shirt Large Black")
    assert is_comparable("wi fi router", "Wi-Fi Router Dual Band AC1200")
    # A hyphenated *model code* (with a digit) unifies across written forms.
    assert is_comparable("ford f-150", "Ford F150 Pickup")
    assert is_comparable("ford f150", "Ford F-150 Pickup")


def test_graded_card_grade_is_pinned() -> None:
    # The grade is the price driver: PSA 10 must reject PSA 9, and the glued form
    # "PSA10" must match the spaced "PSA 10".
    assert is_comparable("charizard base set psa 10", "Charizard Base Set PSA 10 Holo")
    assert not is_comparable("charizard base set psa 10", "Charizard Base Set PSA 9 Holo")
    assert is_comparable("pokemon charizard psa 10", "Pokemon Charizard PSA10 Gem Mint")


def test_perfume_concentration_is_a_distinct_line() -> None:
    # EDP ≠ EDT, and the long form "Eau de Parfum" folds to EDP.
    assert is_comparable("dior sauvage edp 100ml", "Dior Sauvage Eau de Parfum 100ml")
    assert not is_comparable("dior sauvage edp 100ml", "Dior Sauvage EDT 100ml")
    # A generic perfume query keeps both concentrations for per-cluster pricing.
    assert is_comparable("dior sauvage 100ml", "Dior Sauvage EDP 100ml")
    assert is_comparable("dior sauvage 100ml", "Dior Sauvage EDT 100ml")


def test_volume_is_pinned_when_named() -> None:
    assert is_comparable("dior sauvage edp 100ml", "Dior Sauvage EDP 100ml Spray")
    assert not is_comparable("dior sauvage edp 100ml", "Dior Sauvage EDP 60ml")
    assert is_comparable("coca cola 2l", "Coca Cola 2L Bottle")
    assert not is_comparable("coca cola 2l", "Coca Cola 1.5L Bottle")
    # A watch that doesn't name a size is unaffected by the title's size.
    assert is_comparable("dior sauvage edp", "Dior Sauvage EDP 100ml")


def test_long_reference_number_accepts_variant_suffix() -> None:
    # Rolex 116610 → 116610LN (a sub-variant), but a different ref is rejected.
    assert is_comparable("rolex submariner 116610", "Rolex Submariner 116610LN Black")
    assert not is_comparable("rolex submariner 116610", "Rolex Submariner 114060 No Date")


def test_aperture_slash_notation_matches() -> None:
    assert is_comparable("canon 50mm f1.8", "Canon EF 50mm f/1.8 STM Lens")
    assert not is_comparable("canon 50mm f1.8", "Canon EF 50mm f/1.4 USM Lens")


def test_filter_comparable_keeps_only_matches() -> None:
    items = [
        _item("a", "Dyson Airblade HU02 Hand Dryer"),
        _item("b", "Wall Bracket for Dyson Airblade HU02"),
        _item("c", "Dyson Airblade HU03 Hand Dryer"),
        _item("d", "Dyson Airblade HU02 Nickel BNIB"),
    ]
    kept = filter_comparable("dyson airblade hu02", items)

    assert [item.item_id for item in kept] == ["a", "d"]
