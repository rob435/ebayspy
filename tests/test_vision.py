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
