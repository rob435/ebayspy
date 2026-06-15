import asyncio

from ebayspy.models import Listing, MarketItem
from ebayspy.telegram import TelegramBot, format_seller_rating


def _bot(global_id: str = "EBAY-GB") -> TelegramBot:
    return TelegramBot("token", 1, global_id)


def test_format_seller_rating() -> None:
    assert format_seller_rating(98.6, 1004) == "98.6% (1,004)"
    assert format_seller_rating(100, 5) == "100% (5)"
    assert format_seller_rating("", "") == ""
    assert format_seller_rating(None, 1004) == "(1,004)"


def test_seller_line_builds_marketplace_hyperlink() -> None:
    bot = _bot("EBAY-GB")
    try:
        line = bot._seller_line("colourfu59", 98.6, 1004)
        assert 'href="https://www.ebay.co.uk/usr/colourfu59"' in line
        assert "colourfu59" in line
        assert "98.6% (1,004)" in line
    finally:
        asyncio.run(bot.close())


def test_seller_line_without_rating() -> None:
    bot = _bot()
    try:
        line = bot._seller_line("seller_x", "", "")
        assert "usr/seller_x" in line
        assert "⭐" not in line
    finally:
        asyncio.run(bot.close())


def test_ended_notification_includes_rating_and_dates() -> None:
    bot = _bot("EBAY-GB")
    captured: list[str] = []

    async def fake_send(chat_id, text, disable_preview=False, reply_markup=None):
        captured.append(text)

    bot.send_message = fake_send
    listing = Listing(
        item_id="1",
        seller="colourfu59",
        title="Dyson Airblade HU02",
        price="430.00 GBP",
        url="https://www.ebay.co.uk/itm/1",
        listed_at="2026-06-13T09:36:52.000Z",
        seller_feedback_percent="98.6",
        seller_feedback_score="1004",
    )
    try:
        asyncio.run(bot.notify_ended_listing("c", listing, "2026-06-14T20:01:00.000Z"))
        text = captured[0]
        assert "🔴 ENDED" in text
        assert 'href="https://www.ebay.co.uk/usr/colourfu59"' in text
        assert "98.6% (1,004)" in text
        assert "🕒 Listed 13 Jun 2026" in text
        assert "🏁 Ended 14 Jun 2026" in text
    finally:
        asyncio.run(bot.close())


def test_deal_notification_includes_rating() -> None:
    bot = _bot("EBAY-GB")
    captured: list[str] = []

    async def fake_send(chat_id, text, disable_preview=False, reply_markup=None):
        captured.append(text)

    bot.send_message = fake_send
    item = MarketItem(
        item_id="1",
        title="Dyson Airblade HU02",
        url="https://www.ebay.co.uk/itm/1",
        seller="bargain22",
        currency="GBP",
        item_price=300.0,
        total_price=309.99,
        shipping_cost=9.99,
        seller_feedback_percent=99.2,
        seller_feedback_score=540,
        buying_options=("FIXED_PRICE", "BEST_OFFER"),
    )
    try:
        asyncio.run(
            bot.notify_deal(
                "c",
                item,
                market_price=430.0,
                discount_percent=27.9,
                query="dyson airblade hu02",
                profit=65.0,
                roi=21.0,
            )
        )
        text = captured[0]
        assert "💸 DEAL FOUND" in text
        assert "usr/bargain22" in text
        assert "99.2% (540)" in text
        assert "Est. flip profit" in text and "+21% ROI" in text
        assert "Accepts offers" in text
    finally:
        asyncio.run(bot.close())


def test_deal_notification_shows_distribution_and_comps() -> None:
    bot = _bot("EBAY-GB")
    captured: list[str] = []

    async def fake_send(chat_id, text, disable_preview=False, reply_markup=None):
        captured.append(text)

    bot.send_message = fake_send
    item = MarketItem(
        item_id="1", title="Dyson Airblade HU02", url="https://www.ebay.co.uk/itm/1",
        seller="s", currency="GBP", item_price=300.0, total_price=300.0,
    )
    try:
        asyncio.run(
            bot.notify_deal(
                "c", item, market_price=430.0, discount_percent=30, query="dyson airblade hu02",
                distribution=(380.0, 430.0, 510.0), comps=[440.0, 425.0, 450.0],
            )
        )
        text = captured[0]
        assert "range £380.00–£510.00" in text
        assert "🧾 Recently sold: £440.00 · £425.00 · £450.00" in text
    finally:
        asyncio.run(bot.close())
