from ebayspy.formatting import (
    format_listing_type,
    format_money,
    format_price,
    format_seller_rating,
    format_timestamp,
    spec_line,
    truncate_text,
)


def test_truncate_text_prefers_a_line_boundary() -> None:
    text = "\n".join(f"line {i}" for i in range(2000))
    out = truncate_text(text, 4096)
    assert len(out) <= 4096
    assert out.endswith("…")
    # Cut at a newline, so the last kept line is intact.
    assert out[:-1].rstrip().endswith(tuple(f"line {i}" for i in range(2000)))
    assert truncate_text("hello", 4096) == "hello"


def test_truncate_text_hard_cuts_a_single_long_line() -> None:
    out = truncate_text("x" * 5000, 2000)
    assert len(out) == 2000 and out.endswith("…")


def test_format_price() -> None:
    assert format_price("430.00 GBP") == "£430.00"
    assert format_price("1234.5 USD") == "$1,234.50"
    assert format_price("99 XYZ") == "99.00 XYZ"
    assert format_price("") == "Unknown"
    assert format_price(None) == "Unknown"
    assert format_price("free") == "free"


def test_format_money() -> None:
    assert format_money(430.0, "GBP") == "£430.00"
    assert format_money(12.5, "CHF") == "CHF 12.50"
    assert format_money(7, "XYZ") == "7.00 XYZ"


def test_format_seller_rating() -> None:
    assert format_seller_rating(98.6, 1004) == "98.6% (1,004)"
    assert format_seller_rating(100, 5) == "100% (5)"
    assert format_seller_rating("", "") == ""
    assert format_seller_rating(None, 1004) == "(1,004)"


def test_format_listing_type() -> None:
    assert format_listing_type("FIXED_PRICE") == "Buy It Now"
    assert format_listing_type("FIXED_PRICE,BEST_OFFER") == "Buy It Now · Best Offer"
    assert format_listing_type("SOMETHING_NEW") == "Something New"
    assert format_listing_type("") == ""


def test_format_timestamp() -> None:
    assert format_timestamp("2026-06-13T09:36:52.000Z") == "13 Jun 2026, 09:36 UTC"
    assert format_timestamp("2026-06-13T10:36:52+01:00") == "13 Jun 2026, 09:36 UTC"
    assert format_timestamp("not a date") == "not a date"
    assert format_timestamp(None) == ""


def test_spec_line_picks_known_aspects_in_order() -> None:
    aspects = {"Colour": "Blue", "Brand": "Apple", "Storage Capacity": "256 GB", "Weight": "1kg"}
    assert spec_line(aspects) == "Apple · 256 GB · Blue"
    assert spec_line({}) == ""
