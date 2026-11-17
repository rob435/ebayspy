from ebayspy import semantic
from ebayspy.matching import (
    Verdict,
    accepts_damaged,
    attributes,
    canonicalize,
    content_tokens,
    discriminators,
    filter_comparable,
    fuzzy_ratio,
    generation_of,
    is_comparable,
    lot_quantity,
    match_verdict,
    normalize,
    register_aliases,
    specified_dimensions,
    tokenize,
    volumes,
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


def test_discriminators_extract_codes_not_units() -> None:
    # (Replaces the removed test-only ``model_numbers`` helper.) A hyphenated
    # model code is unified into one token (SM-G991B -> smg991b) so its written
    # forms all match; units — including a hyphen-joined voltage range — are not.
    assert discriminators("Dyson Airblade HU02 240V") == {"hu02"}
    assert discriminators("Samsung Galaxy SM-G991B 256GB") == {"smg991b"}
    assert discriminators("plain words only") == set()


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


# ---------------------------------------------------------------------------
# Normalisation bugs (each was a confirmed matcher bug)
# ---------------------------------------------------------------------------


def test_decimals_survive_tokenizing() -> None:
    assert tokenize("Nike Air Max 90 UK 9.5")[-1] == "9.5"
    assert not is_comparable("nike air max 90 uk 9", "Nike Air Max 90 UK 9.5")
    assert not is_comparable("charizard base set bgs 9", "Charizard Base Set BGS 9.5")
    assert volumes(normalize("coca cola 1.5l")) == {"1.5l"}
    assert not is_comparable("coca cola 1.5l", "Coca Cola 5L Bottle")
    assert not is_comparable("seagate expansion 1.5tb", "Seagate Expansion 5TB Hard Drive")


def test_model_code_match_is_bounded() -> None:
    assert not is_comparable("fujifilm x100v", "Fujifilm X100VI Silver")
    assert is_comparable("fujifilm x100v", "Fujifilm X100V Silver")
    # glued after a word, but not after a code prefix of its own
    assert is_comparable("iphone 13", "APPLE IPHONE13 128GB BLUE")
    assert not is_comparable("iphone 13", "Apple iPhone 11 A13 Bionic 64GB")
    # a long code keeps its regional / kit suffixes; the reverse drop works too
    assert is_comparable("lg oled55c3", "LG OLED55C36LC 55 Inch C3 TV")
    assert is_comparable("ninja af300uk", "Ninja AF300 Dual Air Fryer")
    assert is_comparable("sony wh-1000xm5", "Sony 1000XM5 headphones silver")
    assert not is_comparable("sony wh-1000xm5", "Sony WF-1000XM5 Earbuds")
    # short codes: a letter suffix is a different model
    assert not is_comparable("sony a7 iii", "Sony A7R III Body")


def test_coverage_reuses_code_tolerance() -> None:
    assert is_comparable("dewalt dcd796", "DeWalt DCD796N 18V XR Combi Drill")
    assert is_comparable("dewalt dcd796", "Dewalt DCD 796 18v combi drill")
    assert is_comparable("dyson v11 absolute", "Dyson V 11 Absolute Cordless Vacuum")


def test_alias_expansion_is_single_pass() -> None:
    assert canonicalize("xbox series x") == "xbox series x"
    assert canonicalize("series x console") == "xbox series x console"
    register_aliases([("pw 11th gen", "kindle paperwhite 11th gen")])
    assert canonicalize("PW 11th Gen") == "kindle paperwhite 11 gen"


def test_one_x_is_not_series_x() -> None:
    assert not is_comparable("xbox series x", "Microsoft Xbox One X 1TB Console")
    assert is_comparable("xbox series x", "Xbox SeriesX console 1TB")


def test_mark_and_bare_roman_are_one_generation() -> None:
    assert is_comparable("sony a7 iii", "Sony a7 Mark III Full Frame Camera")
    assert is_comparable("sony a7 iii", "Sony A7III Body")
    # a roman numeral in some other name is not the product's generation
    assert is_comparable("playstation 4", "PlayStation 4 Final Fantasy VII Remake Pack 500GB")


def test_air_in_an_appliance_name_is_not_a_qualifier() -> None:
    assert is_comparable("ninja foodi max af400uk", "Ninja AF400UK Foodi MAX Dual Zone Air Fryer")
    assert attributes("Ninja Air Fryer AF300UK")["qualifiers"] == set()
    assert is_comparable("nike air max 90 uk 9", "Nike AirMax 90 UK 9")


def test_query_exemption_is_by_word_not_substring() -> None:
    assert not is_comparable("xbox only", "Xbox Only Box Only")


def test_capacity_ignores_ram_but_ram_query_pins() -> None:
    assert attributes("MacBook Air M2 16GB RAM 512GB SSD")["capacity"] == "512gb"
    assert is_comparable("raspberry pi 4 8gb", "Raspberry Pi 4 Model B 8GB RAM")


def test_volume_reads_ounces() -> None:
    assert is_comparable("dior sauvage edp 100ml", "Dior Sauvage Eau de Parfum 3.4 oz Spray")
    assert is_comparable("dior sauvage edp 3.4oz", "Dior Sauvage EDP 100ml 3.4 fl oz")


def test_aperture_written_forms() -> None:
    for title in ("Canon EF 50mm 1:1.8 STM", "Canon EF 50 mm F 1.8 STM", "Canon 50mm 1.8 STM"):
        assert is_comparable("canon ef 50mm f1.8 stm", title), title


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------


def test_lot_quantity_reads_quantity_forms() -> None:
    assert lot_quantity("2 x Apple iPhone 13 Pro 256GB") == 2
    assert lot_quantity("2x DeWalt DCD796 Combi Drills") == 2
    assert lot_quantity("Canon EF 50mm f1.8 STM Lens x2") == 2
    assert lot_quantity("Dior Sauvage EDP 100ml x 2") == 2
    assert lot_quantity("Pack of 2 Apple AirPods Pro") == 2
    assert lot_quantity("2 Pairs Nike Air Max 90") == 2
    assert lot_quantity("Job Lot 4 Xbox Series X Consoles") == 4
    assert lot_quantity("Bundle of 8 Apple iPhone 7") == 8
    assert lot_quantity("DeWalt DCD796 + DCF887 Twin Pack") == 2


def test_lot_quantity_does_not_overfire() -> None:
    for title in (
        "MSI GeForce RTX 4070 Ventus 2X 12G OC",
        "Gaming RTX 4070 PC 32GB",
        "Apple iPhone X 128 GB Silver",
        "Xbox Series X 2 Controllers",
        "AirPods Pro 2 Left Right Pair",
        "Sony Headphones paired once",
        "LEGO Star Wars UCS Millennium Falcon 75192 7541 Pieces",
    ):
        assert lot_quantity(title) is None, title


def test_quantities_are_rejected_unless_lots_allowed() -> None:
    assert match_verdict("dewalt dcd796", "2x DeWalt DCD796 Combi Drill").reason == "quantity:2"
    assert is_comparable("dewalt dcd796", "2x DeWalt DCD796 Combi Drill", allow_lots=True)
    assert not is_comparable("iphone 13", "Job Lot Apple iPhone 13 Grade B")
    # a query that itself names the multipack keeps it
    assert is_comparable("duracell aa 4 pack", "Duracell AA Batteries 4 Pack")
    # a single console sold as a "bundle" is one unit with extras
    assert is_comparable("nintendo switch", "Nintendo Switch Bundle Used Tested And Working")


# ---------------------------------------------------------------------------
# Accessories, parts, packaging, media
# ---------------------------------------------------------------------------


def test_compatibility_phrases_are_accessories() -> None:
    v = match_verdict("canon ef 50mm f1.8 stm", "Lens Hood ES-68 for Canon EF 50mm f/1.8 STM")
    assert v.reason == "accessory:for-phrase"
    assert not is_comparable("dewalt dcd796", "DeWalt DCB184 5Ah Battery for DCD796")
    assert not is_comparable("ninja af300uk", "Cosori Dual Basket Air Fryer like Ninja AF300UK")
    assert not is_comparable("lego 75192", "Lepin 05132 Millennium Falcon Compatible 75192")
    # title-initial and non-English markers
    assert not is_comparable("fitbit charge 2", "For Fitbit Charge 2 Bracelet Wristband Leather")
    assert not is_comparable("garmin fenix", "Silikonarmband für Garmin Fenix 5s Smartwatch")
    assert not is_comparable("fitbit versa", "Pulsera cristal para Fitbit Versa Band Correa")
    assert not is_comparable("garmin fenix", "Cinturino in silicone per Garmin Fenix")


def test_compatibility_phrases_do_not_overfire() -> None:
    assert is_comparable("dior sauvage edp 100ml", "Dior Sauvage EDP 100ml Spray for Men")
    assert is_comparable("iphone 13", "Apple iPhone 13 128GB Like New")
    # the query names the accessory itself
    assert is_comparable("iphone 14 case", "Silicone Case for iPhone 14 with MagSafe")


def test_accessory_nouns_in_context() -> None:
    assert match_verdict("xbox series x", "Xbox Series X Controller Carbon Black").reason == (
        "accessory:controller"
    )
    assert not is_comparable("lg oled55c3", "LG OLED55C3 Remote Control Magic")
    assert not is_comparable("iphone 13", "Apple iPhone 13 Logic Board 128GB Working")
    assert not is_comparable("macbook air m2", "MacBook Air M2 Screen Assembly A2681")
    assert not is_comparable("dji mini 3 pro", "DJI Mini 3 Pro Intelligent Flight Battery")
    assert not is_comparable("iphone 13", "Tempered Glass Screen Protector iPhone 13")
    assert not is_comparable("samsung galaxy watch", "Screen Protector Samsung Galaxy Watch 46mm")


def test_accessory_nouns_do_not_overfire() -> None:
    # in-box extras after a marker or a count
    assert is_comparable("iphone 13 pro 256gb", "Apple iPhone 13 Pro 256GB Boxed with Charger")
    assert is_comparable("airpods pro 2", "Apple AirPods Pro 2nd Generation with MagSafe Case")
    assert is_comparable("dyson v11 absolute", "Dyson V11 Absolute + Docking Station Wall Mount")
    assert is_comparable("sony a7 iii", "Sony A7 III Camera Body Black 2 Batteries Charger")
    assert is_comparable("iphone xs", "Apple iPhone XS Max 256GB inkl. Apple Leather Case und OVP")
    assert is_comparable("lg oled55c3", "LG OLED55C3 4K Smart TV 2023 Remote Included")
    # the word is a feature, part of the name, or the configuration of a unit
    assert is_comparable("ninja af300uk", "Ninja Air Fryer AF300UK Filter-Free Easy Clean")
    assert is_comparable(
        "ravensburger 1000 piece jigsaw", "Ravensburger Mount Fuji 1000 Piece Jigsaw Puzzle"
    )
    assert is_comparable("wi fi router", "Wi-Fi Router Dual Band AC1200")
    assert is_comparable("iphone 13 pro", "Apple iPhone 13 Pro 256GB Battery 79% Read")
    assert is_comparable("nintendo switch", "Nintendo Switch Grey Console Improved Battery")
    assert is_comparable("dji mini 3 pro", "DJI Mini 3 Pro (DJI RC) Smart Controller Drone")
    assert is_comparable(
        "nintendo switch", "Nintendo Switch Handheld Console 32GB Neon Joy-Con Controllers"
    )
    assert is_comparable(
        "apple watch series 3", "Apple Watch Series 3 42mm Space Grey Aluminium Case Sport Band"
    )
    assert is_comparable(
        "samsung galaxy watch", "Samsung Galaxy Watch SM-R800 46mm Silver Case Classic Buckle"
    )


def test_compatibility_lists_are_accessories() -> None:
    assert not is_comparable(
        "samsung galaxy s9", "Original Samsung Galaxy S6 S7 S8 S9 Note 8 Headphones Headset"
    )
    assert not is_comparable("iphone 7", "Lightning Earphones Pop-Up Iphone 7 8 Plus X 11 12")
    assert is_comparable("iphone 7", "Apple iPhone 7 32GB 2 Year Warranty")
    assert is_comparable("kindle paperwhite 11th gen", "Kindle Paperwhite 5 11th Gen")


def test_component_only_and_missing_component() -> None:
    assert match_verdict("airpods pro 2", "AirPods Pro 2 Left Earbud Only").reason == (
        "part:earbud only"
    )
    assert not is_comparable("xbox series x", "Xbox Series X Console Only No Controller")
    assert not is_comparable("nintendo switch", "Nintendo Switch System Console Tablet Only")
    assert is_comparable("rolex submariner 116610", "Rolex Submariner 116610LN Watch Only No Box")
    assert is_comparable("rolex submariner 114060", "Rolex Submariner 114060 No Date Ceramic")
    for title in (
        "Apple Watch Series 3 42mm GPS Only with Sport Band",
        "Apple Watch Series 3 42mm Cellular Only",
        "Apple Watch Series 3 42mm Collection Only",
    ):
        assert is_comparable("apple watch series 3", title), title


def test_packaging() -> None:
    assert match_verdict("iphone 13", "iPhone 13 Original Box Only").reason == (
        "packaging:box only"
    )
    assert not is_comparable("rolex submariner 116610", "Rolex 116610LN Box and Papers Only")
    assert not is_comparable("dior sauvage edp 100ml", "Dior Sauvage EDP 100ml Empty Bottle")
    assert not is_comparable("airpods max", "Airpods Max Box - Space grey - No Headphones")
    assert is_comparable(
        "rolex submariner 116610", "Rolex Submariner 116610LN 40mm 2015 Box & Papers"
    )


def test_media_and_hosts() -> None:
    assert match_verdict("xbox series x", "Starfield Xbox Series X Game").reason == "media:game"
    assert not is_comparable("ninja af300uk", "Ninja Foodi AF300UK Recipe Book")
    assert is_comparable("playstation 4", "Sony PlayStation 4 Pro 1TB Video Game Console")
    assert is_comparable("nintendo switch", "Nintendo Switch Games Console Blue Red")
    assert match_verdict("rtx 4070", "Gaming PC Ryzen 7 RTX 4070 32GB").reason == "contains:pc"
    assert not is_comparable("canon ef 50mm f1.8 stm", "Canon EOS 250D with EF 50mm f/1.8 STM")


# ---------------------------------------------------------------------------
# Damage and replicas
# ---------------------------------------------------------------------------


def test_damage_vocabulary_is_stemmed_and_phrased() -> None:
    for title in (
        "Dyson V11 Absolute Pulsing Battery Fault",
        "DeWalt DCD796 Smells Burning Stops Working",
        "Rolex Submariner 116610LN Not Running",
        "iPhone 13 Pro 256GB Smashed Screen",
        "Anker 737 Power Bank Faulty Won't Charge",
        "MacBook Air M2 MDM Locked",
        "LG OLED55C3 Lines on Screen",
        "Kindle Paperwhite Dead Pixels",
        "Dior Sauvage EDP 100ml Approx 80% Full",
        "Apple iPhone 7 Black ic locked",
        "iPhone XS Unlocked NO FACE ID",
        "AS-IS Samsung Galaxy Watch 42mm",
        "Apple iPad Air 2 Backlight Issue",
    ):
        assert match_verdict("x", title).reason.startswith("damage:"), title


def test_damage_does_not_overfire() -> None:
    assert is_comparable("iphone 13", "Apple iPhone 13 128GB Fully Working iCloud Clean")
    assert is_comparable("lego 75192", "LEGO 75192 Millennium Falcon Box Damaged Sealed")


def test_accepts_damaged() -> None:
    assert accepts_damaged("iphone 13 spares or repair")
    assert accepts_damaged("faulty ps5")
    assert accepts_damaged("iphone 13 for parts")
    assert not accepts_damaged("iphone 13 pro 256gb")
    # such a watch keeps the faulty listings
    assert is_comparable("iphone 13 faulty", "Apple iPhone 13 Cracked Screen Faulty")


def test_replica_vocabulary() -> None:
    for title in (
        "AirPods Pro 2 Replica 1:1 Copy",
        "Rolex Submariner 116610 Homage Automatic",
        "Dior Sauvage EDP 100ml Inspired Alternative Dupe",
        "Charizard Base Set PSA 9 Custom Proxy Card",
        "Nintendo Switch OLED 64GB Console - Display Model",
        "Copy of DeWalt DCD796 Drill Unbranded",
    ):
        assert match_verdict("x", title).reason.startswith("replica:"), title


# ---------------------------------------------------------------------------
# Identity a title adds, and pinned variants
# ---------------------------------------------------------------------------


def test_titles_adding_a_different_identity() -> None:
    assert match_verdict("rtx 4070", "MSI GeForce RTX 4070 Ti Gaming X").reason == (
        "identity:tier ti"
    )
    assert not is_comparable("rtx 4070", "RTX 4070 SUPER 12GB Asus Dual")
    assert not is_comparable("charizard base set psa 9", "1st Edition Charizard Base Set PSA 9")
    assert not is_comparable("charizard base set psa 9", "Charizard Base Set Japanese PSA 9")
    assert not is_comparable("nike air max 90 uk 9", "Nike Air Max 90 Kids UK 9 Junior")
    assert not is_comparable("canon ef 50mm f1.8 stm", "Canon RF 50mm f/1.8 STM")
    # an edition of a device is a variant, not a kids' size
    assert is_comparable("echo dot", "Amazon Echo Dot Kids Edition Blue")
    # cooler names are not tiers
    assert is_comparable("rtx 4070", "Zotac Gaming RTX 4070 Twin Edge OC 12GB")


def test_grade_is_pinned_with_its_grader_and_size_with_its_system() -> None:
    assert not is_comparable("charizard base set psa 9", "Charizard Base Set Holo BGS 9 Mint")
    assert is_comparable("charizard base set psa 9", "Charizard 1999 Base Unlimited PSA Mint 9")
    assert is_comparable("nike air max 90 uk 9", "Nike Air Max 90 Size 9 UK")
    assert is_comparable("nike air max 90 uk 9", "Nike Air Max 90 UK9 Infrared")


def test_measurement_named_by_query_is_pinned() -> None:
    assert not is_comparable("apple watch series 8 45mm", "Apple Watch Series 8 41mm GPS")
    assert is_comparable("apple watch series 8 45mm", "Apple Watch Series 8 45mm GPS")


def test_unpinned_generation_is_a_variant() -> None:
    assert is_comparable("echo dot", "Amazon Echo Dot (3rd Generation) Charcoal")
    assert is_comparable("fitbit versa", "Fitbit Versa 2 Smartwatch")
    # a generation the query pins is still exact
    assert not is_comparable("fitbit charge 2", "Fitbit Charge 3 Tracker")
    assert not is_comparable("airpods pro 2", "Apple AirPods Pro 3 2025")


def test_generation_of() -> None:
    assert generation_of("echo dot", "Amazon Echo Dot (3rd Generation) Charcoal") == "3"
    assert generation_of("garmin fenix", "Garmin Fenix 6S Pro Solar") == "6"
    assert generation_of("fitbit versa", "Fitbit Versa 2 Smartwatch") == "2"
    assert generation_of("airpods", "Apple AirPods 2nd Generation") == "2"
    assert generation_of("canon eos r6", "Canon EOS R6 Mark II Body") == "2"
    assert generation_of("echo dot", "Echo Dot Smart Speaker") == "base"
    assert generation_of("nintendo switch", "Nintendo Switch 2 Controllers") == "base"


def test_verdict_reasons() -> None:
    assert match_verdict("iphone 13", "Apple iPhone 13 128GB") == Verdict(True, "ok")
    assert match_verdict("iphone 13", "Apple iPhone 14 128GB").reason == "identity:missing 13"
    assert match_verdict("iphone 13", "iPhone 13 Faulty").reason == "damage:faulty"
    assert match_verdict("iphone 13 256gb", "iPhone 13 128GB").reason == "variant:capacity"
    reason = match_verdict("north face nuptse jacket", "North Face Backpack").reason
    assert reason.startswith("relevance")


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def test_semantic_is_computed_lazily(monkeypatch) -> None:
    calls = []

    def fake(query, titles):
        calls.append(list(titles))
        return [0.9] * len(titles)

    monkeypatch.setattr(semantic, "similarities", fake)
    items = [
        _item("a", "The North Face Nuptse Jacket"),        # passes lexically
        _item("b", "North Face Nuptse Jacket Faulty Zip"),  # fails a guardrail
        _item("c", "totally reworded description"),         # fails relevance only
    ]
    kept = filter_comparable("north face nuptse jacket", items, semantic_threshold=0.6)
    assert [i.item_id for i in kept] == ["a", "c"]
    assert calls == [["totally reworded description"]]


def test_query_is_compiled_once_per_filter() -> None:
    from ebayspy import matching

    matching._compile.cache_clear()
    items = [_item(str(i), f"Apple iPhone 13 128GB unit {i}") for i in range(20)]
    filter_comparable("iphone 13", items)
    assert matching._compile.cache_info().misses == 1


def test_semantic_load_failure_retries_after_cooldown(monkeypatch) -> None:
    import sys
    import types

    attempts = []

    class Broken:
        @staticmethod
        def from_pretrained(name):
            attempts.append(name)
            raise OSError("network down")

    monkeypatch.setitem(sys.modules, "model2vec", types.SimpleNamespace(StaticModel=Broken))
    monkeypatch.setattr(semantic, "_model", None)
    monkeypatch.setattr(semantic, "_unavailable", False)
    monkeypatch.setattr(semantic, "_failed_at", None)
    # A service built earlier in the run with semantics off flips this global.
    monkeypatch.setattr(semantic, "_disabled", False)
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    assert semantic.similarities("q", ["t"]) is None
    assert semantic.similarities("q", ["t"]) is None  # inside the cool-down
    assert len(attempts) == 1
    clock[0] += semantic.RETRY_SECONDS + 1
    assert semantic.similarities("q", ["t"]) is None
    assert len(attempts) == 2


def test_unpinned_edition_letter_is_a_variant_of_the_named_line() -> None:
    from ebayspy.matching import generation_of

    # "xbox one" names the line; One S and One X are editions of it — matched,
    # and told apart for pricing by generation_of.
    assert is_comparable("xbox one", "Microsoft Xbox One X 1TB Black Console")
    assert is_comparable("xbox one", "Microsoft Xbox One S 500GB White Console")
    assert generation_of("xbox one", "Microsoft Xbox One X 1TB Black Console") == "x"
    assert generation_of("xbox one", "Microsoft Xbox One S 500GB White Console") == "s"
    assert generation_of("xbox one", "Microsoft Xbox One 500GB Black Console") == "base"
    # A pinned edition still has to agree...
    assert not is_comparable("xbox one x", "Microsoft Xbox One S 500GB White Console")
    assert is_comparable("xbox one x", "Microsoft Xbox One X 1TB Black Console")
    # ...and a letter on a line the query never named is another product.
    assert not is_comparable("xbox one", "Microsoft Xbox Series X 1TB Console")
    assert not is_comparable("xbox series x console", "Microsoft Xbox One X Console 1TB")
