from ebayspy.models import MarketItem
from ebayspy.risk import assess


def _item(**kw) -> MarketItem:
    base = dict(
        item_id="1", title="t", url="u", seller="s", currency="GBP",
        item_price=300.0, total_price=300.0,
    )
    base.update(kw)
    return MarketItem(**base)


def test_clean_listing_low_risk() -> None:
    score, reasons = assess(
        _item(seller_feedback_score=5000, seller_feedback_percent=99.6, item_location="GB"),
        market_price=350.0, home_country="GB",
    )
    assert score == 0 and reasons == []


def test_new_seller_too_cheap_foreign_high_risk() -> None:
    score, reasons = assess(
        _item(
            total_price=120.0,  # ~34% of market -> far below
            seller_feedback_score=2,
            seller_feedback_percent=88.0,
            item_location="CN",
        ),
        market_price=350.0, home_country="GB",
    )
    assert score >= 80
    joined = " ".join(reasons).lower()
    assert "feedback" in joined and "below market" in joined and "ships from cn" in joined


def test_all_red_flags_hit_the_suppression_ceiling() -> None:
    # An all-red-flags listing must reach the 100 cap so it meets the default
    # MARKET_RISK_MAX (100) and the service's ``>=`` suppression actually fires.
    score, _ = assess(
        _item(
            total_price=100.0,  # ~29% of market
            seller_feedback_score=1,
            seller_feedback_percent=70.0,
            item_location="CN",
        ),
        market_price=350.0, home_country="GB",
    )
    assert score == 100
