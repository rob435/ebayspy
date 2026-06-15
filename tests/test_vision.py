from ebayspy import vision


def test_is_condition_upgrade() -> None:
    # Poor stated condition + a "new"-looking photo = a possible gem.
    assert vision.is_condition_upgrade("For parts or not working", "new")
    assert vision.is_condition_upgrade("Spares or repair", "new")
    # Not a gem when the photo doesn't look new, or the listing already says new.
    assert not vision.is_condition_upgrade("For parts", "broken")
    assert not vision.is_condition_upgrade("Brand New", "new")
    assert not vision.is_condition_upgrade("Used", "new")  # "used" alone isn't a gem signal


def test_vision_degrades_gracefully_when_disabled() -> None:
    vision.disable()
    assert vision.available() is False
    assert vision.match_score("https://example.test/x.jpg", "iphone 13") is None
    assert vision.classify_condition("https://example.test/x.jpg") is None


def test_new_classifiers_return_none_when_disabled() -> None:
    vision.disable()
    url = "https://example.test/x.jpg"
    assert vision.is_stock_photo(url) is None
    assert vision.is_damaged(url) is None
    assert vision.item_count_hint(url) is None
    assert vision.vision_flags(url, "used") is None
    assert vision.vision_flags("", "used") is None  # empty url too


def test_compose_note_combines_flags() -> None:
    flags = {
        "condition": ("used", 0.3),
        "upgrade": False,
        "stock": ("stock", 0.7),
        "damage": ("damaged", 0.7),
        "count": ("multiple", 0.7),
    }
    note = vision.compose_note(
        flags, "used parts", stock_threshold=0.55, damage_threshold=0.55,
        count_hint=True, count_threshold=0.55,
    )
    assert "Image looks used" in note
    assert "dropship/scam" in note
    assert "damage" in note
    assert "multiple items" in note


def test_compose_note_clean_image_is_empty() -> None:
    flags = {
        "condition": None,
        "upgrade": False,
        "stock": ("real", 0.6),
        "damage": ("pristine", 0.6),
        "count": ("single", 0.6),
    }
    assert vision.compose_note(
        flags, "new", stock_threshold=0.55, damage_threshold=0.55,
        count_hint=True, count_threshold=0.55,
    ) == ""


def test_compose_note_stock_flag_only_on_used_listings() -> None:
    flags = {"condition": None, "upgrade": False, "stock": ("stock", 0.9),
             "damage": None, "count": None}
    # A stock render on a NEW listing is normal -> no warning.
    assert vision.compose_note(
        flags, "Brand new sealed", stock_threshold=0.55, damage_threshold=0.55,
        count_hint=False, count_threshold=0.55,
    ) == ""
