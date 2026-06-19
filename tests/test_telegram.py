import asyncio

import httpx

from ebayspy.models import Listing, MarketItem
from ebayspy.telegram import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    TelegramBot,
    TelegramError,
    _truncate_html,
    format_seller_rating,
)


def _bot(global_id: str = "EBAY-GB") -> TelegramBot:
    return TelegramBot("token", 1, global_id)


class _CapturingClient:
    """Records outgoing requests so message hardening can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))

        class _Resp:
            def raise_for_status(self_inner) -> None:
                pass

            def json(self_inner) -> dict:
                return {"result": []}

        return _Resp()

    async def aclose(self) -> None:
        pass


def test_truncate_html_clips_long_text_without_breaking_tags() -> None:
    text = "\n".join(f"<b>line {i}</b>" for i in range(2000))
    out = _truncate_html(text, MESSAGE_LIMIT)
    assert len(out) <= MESSAGE_LIMIT
    assert out.endswith("…")
    # Never ends inside a tag (would make Telegram reject the whole message).
    assert out.rfind("<") <= out.rfind(">")
    # Short text is returned untouched.
    assert _truncate_html("hello", MESSAGE_LIMIT) == "hello"


def test_send_message_truncates_to_telegram_limit() -> None:
    bot = _bot()
    bot.client = _CapturingClient()
    asyncio.run(bot.send_message("c", "x" * (MESSAGE_LIMIT + 5000)))
    sent = bot.client.calls[0][2]["json"]["text"]
    assert len(sent) <= MESSAGE_LIMIT


def test_send_photo_truncates_caption() -> None:
    bot = _bot()
    bot.client = _CapturingClient()
    asyncio.run(bot.send_photo("c", "http://img", "y" * (CAPTION_LIMIT + 500)))
    caption = bot.client.calls[0][2]["json"]["caption"]
    assert len(caption) <= CAPTION_LIMIT


def test_api_error_scrubs_bot_token() -> None:
    bot = TelegramBot("SECRET-TOKEN-123", 1)

    class _LeakyClient:
        async def request(self, method, url, **kwargs):
            # httpx puts the full token-bearing URL into the error message.
            raise httpx.ConnectError(f"connection failed for {url}")

        async def aclose(self) -> None:
            pass

    bot.client = _LeakyClient()
    try:
        asyncio.run(bot.send_message("c", "hello"))
        raise AssertionError("expected TelegramError")
    except TelegramError as exc:
        assert "SECRET-TOKEN-123" not in str(exc)
        assert "***" in str(exc)
    finally:
        asyncio.run(bot.close())


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
