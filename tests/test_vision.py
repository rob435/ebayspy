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


def test_reference_helpers_degrade_when_disabled() -> None:
    vision.disable()
    # Preload is a safe no-op when vision is off.
    assert vision.preload() is False
    # Image↔image match and reference picking both no-op without the model.
    assert vision.image_match("https://example.test/a.jpg", "https://example.test/b.jpg") is None
    assert vision.image_match("", "https://example.test/b.jpg") is None
    assert vision.pick_reference([]) is None
    assert vision.pick_reference(["https://example.test/a.jpg"]) is None


def test_pick_reference_medoid_picks_the_central_image() -> None:
    """The medoid (highest mean cosine to the rest) is chosen, so an odd-one-out
    can't become the reference. Drives the selection with stubbed embeddings."""
    import numpy as np

    vectors = {
        "a": np.array([1.0, 0.0, 0.0]),
        "b": np.array([0.99, 0.14, 0.0]),  # nearly identical to a — the cluster centre
        "c": np.array([0.97, 0.0, 0.24]),  # also near a
        "odd": np.array([0.0, 0.0, 1.0]),  # the outlier
    }

    def fake_vector(url: str):
        v = vectors[url]
        return v / np.linalg.norm(v)

    monkey = vision._safe_image_vector
    loaded = vision._load
    try:
        vision._safe_image_vector = fake_vector  # type: ignore[assignment]
        vision._load = lambda: True  # type: ignore[assignment]
        assert vision.pick_reference(["a", "b", "c", "odd"]) in {"a", "b", "c"}
        assert vision.pick_reference(["a", "b", "c", "odd"]) != "odd"
    finally:
        vision._safe_image_vector = monkey  # type: ignore[assignment]
        vision._load = loaded  # type: ignore[assignment]


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
