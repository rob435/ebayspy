from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .models import Listing, MarketItem

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, str | None, str, str], Awaitable[str]]
CallbackHandlerType = Callable[[str, str | None, str], Awaitable[str]]

EBAY_DOMAINS = {
    "EBAY-US": "ebay.com",
    "EBAY-GB": "ebay.co.uk",
    "EBAY-DE": "ebay.de",
    "EBAY-FR": "ebay.fr",
    "EBAY-IT": "ebay.it",
    "EBAY-ES": "ebay.es",
    "EBAY-IE": "ebay.ie",
    "EBAY-NL": "ebay.nl",
    "EBAY-AT": "ebay.at",
    "EBAY-BE": "ebay.be",
    "EBAY-AU": "ebay.com.au",
    "EBAY-CA": "ebay.ca",
    "EBAY-CH": "ebay.ch",
    "EBAY-PL": "ebay.pl",
}

CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
    "JPY": "¥",
    "CNY": "¥",
    "CHF": "CHF ",
}

LISTING_TYPE_LABELS = {
    "FIXED_PRICE": "Buy It Now",
    "AUCTION": "Auction",
    "AUCTION_WITH_BIN": "Auction + Buy It Now",
    "BEST_OFFER": "Best Offer",
    "CLASSIFIED_AD": "Classified Ad",
}


def format_price(price: str | None) -> str:
    """Turn an eBay "430.00 GBP" string into a friendly "£430.00"."""
    if not price or not price.strip():
        return "Unknown"
    tokens = price.split()
    currency = tokens[-1] if len(tokens) >= 2 else ""
    if len(currency) == 3 and currency.isalpha() and currency.isupper():
        amount = " ".join(tokens[:-1])
        symbol = CURRENCY_SYMBOLS.get(currency)
        try:
            amount = f"{float(amount):,.2f}"
        except ValueError:
            pass
        if symbol:
            return f"{symbol}{amount}"
        return f"{amount} {currency}"
    return price.strip()


# eBay aspect names worth surfacing on a deal, in display order.
_SPEC_ASPECTS = ("Brand", "Model", "Storage Capacity", "Colour", "Color", "Size", "Type")


def _spec_line(aspects: dict[str, str]) -> str:
    if not aspects:
        return ""
    lower = {k.lower(): (k, v) for k, v in aspects.items()}
    seen: set[str] = set()
    bits = []
    for name in _SPEC_ASPECTS:
        entry = lower.get(name.lower())
        if entry and entry[1] not in seen:
            seen.add(entry[1])
            bits.append(entry[1])
    return " · ".join(bits[:4])


def format_seller_rating(percent: object, score: object) -> str:
    """Render eBay's native seller feedback as "98.6% (1004)".

    The percentage is eBay's positive-feedback rating; the count in parentheses
    is the total feedback received.
    """
    parts = []
    try:
        pct = float(percent)
        if pct > 0:
            parts.append(f"{pct:g}%")
    except (TypeError, ValueError):
        pass
    try:
        count = int(float(score))
        if count > 0:
            parts.append(f"({count:,})")
    except (TypeError, ValueError):
        pass
    return " ".join(parts)


def format_money(value: float, currency: str) -> str:
    """Format a numeric amount with its currency symbol (e.g. 430.0 GBP -> £430.00)."""
    symbol = CURRENCY_SYMBOLS.get(currency)
    try:
        amount = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}".strip()


def format_listing_type(listing_type: str) -> str:
    """Map eBay buying options to readable labels (FIXED_PRICE -> Buy It Now)."""
    labels = [
        LISTING_TYPE_LABELS.get(token, token.replace("_", " ").title())
        for token in (part.strip() for part in listing_type.split(","))
        if token
    ]
    return " · ".join(labels)


def format_timestamp(value: str | None) -> str:
    """Format an ISO 8601 timestamp as "13 Jun 2026, 09:36 UTC"."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.day} {parsed:%b %Y}, {parsed:%H:%M} UTC"


class TelegramBot:
    def __init__(
        self,
        token: str,
        timeout_seconds: int,
        ebay_global_id: str = "EBAY-US",
        send_photos: bool = True,
    ) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=timeout_seconds)
        self.offset = 0
        self.ebay_domain = EBAY_DOMAINS.get(ebay_global_id.upper(), "ebay.com")
        self.send_photos = send_photos

    def _seller_line(self, seller: str, percent: object, score: object) -> str:
        name = html.escape(seller) if seller else "Unknown"
        if seller:
            url = f"https://www.{self.ebay_domain}/usr/{quote(seller, safe='')}"
            name = f'<a href="{html.escape(url)}">{name}</a>'
        rating = format_seller_rating(percent, score)
        return f"👤 {name} · ⭐ {html.escape(rating)}" if rating else f"👤 {name}"

    async def close(self) -> None:
        await self.client.aclose()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        disable_preview: bool = False,
        reply_markup: dict | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = await self.client.post(f"{self.base_url}/sendMessage", json=payload)
        response.raise_for_status()

    async def send_photo(
        self, chat_id: str, photo: str, caption: str, reply_markup: dict | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = await self.client.post(f"{self.base_url}/sendPhoto", json=payload)
        response.raise_for_status()

    async def _deliver(
        self,
        chat_id: str,
        text: str,
        *,
        image_url: str | None = None,
        reply_markup: dict | None = None,
        disable_preview: bool = False,
    ) -> None:
        """Send as a photo with caption when an image is available, else as text.

        Telegram caps photo captions at 1024 chars, so longer alerts fall back to
        a normal message; a failed photo send (dead image URL) also falls back so
        the alert always lands.
        """
        if self.send_photos and image_url and len(text) <= 1024:
            try:
                await self.send_photo(chat_id, image_url, text, reply_markup)
                return
            except Exception:
                log.debug("Photo send failed, falling back to text", exc_info=True)
        await self.send_message(
            chat_id, text, disable_preview=disable_preview, reply_markup=reply_markup
        )

    async def notify_listing(self, chat_id: str, listing: Listing) -> None:
        await self._notify_listing_event(chat_id, listing, header="🆕 NEW LISTING")

    async def notify_ended_listing(
        self, chat_id: str, listing: Listing, ended_at: str | None = None
    ) -> None:
        await self._notify_listing_event(
            chat_id, listing, header="🔴 ENDED", ended_at=ended_at
        )

    async def notify_quantity_increase(
        self, chat_id: str, listing: Listing, previous_quantity: int, current_quantity: int
    ) -> None:
        await self._notify_listing_event(
            chat_id,
            listing,
            header="📈 QUANTITY INCREASED",
            subheader=f"{previous_quantity} → {current_quantity} available",
        )

    async def _notify_listing_event(
        self,
        chat_id: str,
        listing: Listing,
        header: str,
        subheader: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        parts = [f"<b>{html.escape(header)}</b>"]
        if subheader:
            parts.append(html.escape(subheader))
        parts.append("")
        parts.append(f"<b>{html.escape(listing.title)}</b>")

        details = [
            f"💰 {html.escape(format_price(listing.price))}",
            self._seller_line(
                listing.seller,
                listing.seller_feedback_percent,
                listing.seller_feedback_score,
            ),
        ]
        listing_type = format_listing_type(listing.listing_type)
        if listing_type:
            details.append(f"🏷️ {html.escape(listing_type)}")
        if listing.quantity_available:
            details.append(f"📦 {html.escape(listing.quantity_available)} available")
        if listing.category:
            details.append(f"📂 {html.escape(listing.category)}")
        listed = format_timestamp(listing.listed_at)
        if listed:
            details.append(f"🕒 Listed {html.escape(listed)}")
        ended = format_timestamp(ended_at)
        if ended:
            details.append(f"🏁 Ended {html.escape(ended)}")
        parts.append("")
        parts.extend(details)

        description = listing.description.strip()
        if description:
            if len(description) > 500:
                description = description[:497].rstrip() + "..."
            parts.append("")
            parts.append(f"📝 {html.escape(description)}")

        parts.append("")
        parts.append(f'🔗 <a href="{html.escape(listing.url)}">View on eBay</a>')
        await self._deliver(chat_id, "\n".join(parts), image_url=listing.image_url)

    async def notify_deal(
        self,
        chat_id: str,
        item: MarketItem,
        *,
        market_price: float,
        discount_percent: float,
        query: str,
        variant: str | None = None,
        profit: float | None = None,
        roi: float | None = None,
        watch_id: int | None = None,
        ending_soon: bool = False,
        trend: str = "",
        demand: str = "",
        distribution: tuple[float, float, float] | None = None,
        comps: list[float] | None = None,
        low_competition: bool = False,
        risk: str = "",
        lot_quantity: int | None = None,
        vision: str = "",
    ) -> None:
        saving = market_price - item.total_price
        cur = item.currency
        if lot_quantity:
            per_unit = item.total_price / lot_quantity
            unit_market = market_price / lot_quantity
            price_line = (
                f"💰 {html.escape(format_money(item.total_price, cur))} for {lot_quantity} units"
                f" (≈ {html.escape(format_money(per_unit, cur))}/unit)"
            )
            market_label = (
                f"📊 Lot worth ≈ {html.escape(format_money(market_price, cur))}"
                f" ({html.escape(format_money(unit_market, cur))}/unit market)"
            )
            header = "📦 LOT DEAL"
        else:
            bid_note = " current bid" if item.is_auction else ""
            price_line = f"💰 {html.escape(format_money(item.total_price, cur))}{bid_note}"
            if item.shipping_cost:
                price_line += (
                    f" (incl. {html.escape(format_money(item.shipping_cost, cur))} shipping)"
                )
            # When the watch spans variants, the market figure is for this item's
            # own variant (e.g. 256GB), so the comparison is always like-for-like.
            market_label = f"📊 Market ≈ {html.escape(format_money(market_price, cur))}"
            if variant:
                market_label += f" for {html.escape(variant)}"
            if distribution:
                low, _mid, high = distribution
                market_label += (
                    f" (range {html.escape(format_money(low, cur))}"
                    f"–{html.escape(format_money(high, cur))})"
                )
            if ending_soon:
                header = "⏰ AUCTION ENDING SOON"
            elif item.is_auction:
                header = "🔨 AUCTION DEAL"
            else:
                header = "💸 DEAL FOUND"
        parts = [
            f"<b>{header}</b>",
            html.escape(f"{discount_percent:.0f}% below market on “{query}”"),
            "",
            f"<b>{html.escape(item.title)}</b>",
            "",
            price_line,
            market_label,
            f"✅ You save ≈ {html.escape(format_money(saving, item.currency))}",
        ]
        if profit is not None:
            roi_text = f" ({roi:+.0f}% ROI)" if roi is not None else ""
            parts.append(
                f"💵 Est. flip profit ≈ {html.escape(format_money(profit, item.currency))}{roi_text}"
            )
        if comps:
            joined = " · ".join(format_money(price, item.currency) for price in comps)
            parts.append(f"🧾 Recently sold: {html.escape(joined)}")
        if trend:
            parts.append(html.escape(trend))
        if demand:
            parts.append(html.escape(demand))
        if item.accepts_offers:
            parts.append("💬 Accepts offers — you may get it lower")
        if item.is_auction:
            ends = format_timestamp(item.end_date)
            bids = f"{item.bid_count} bid(s)" if item.bid_count is not None else "no bids yet"
            parts.append(f"⏰ Ends {html.escape(ends)} · {bids}" if ends else f"⏰ {bids}")
            if low_competition:
                parts.append("🏁 Low competition — win it uncontested")
        if item.condition:
            parts.append(f"🏷️ {html.escape(item.condition)}")
        if vision:
            parts.append(html.escape(vision))
        specs = _spec_line(item.aspects)
        if specs:
            parts.append(f"📋 {html.escape(specs)}")
        parts.append(
            self._seller_line(
                item.seller, item.seller_feedback_percent, item.seller_feedback_score
            )
        )
        listed = format_timestamp(item.listed_at)
        if listed:
            parts.append(f"🕒 Listed {html.escape(listed)}")
        if risk:
            parts.append(html.escape(risk))
        parts.append("")
        parts.append(f'🔗 <a href="{html.escape(item.url)}">Buy on eBay</a>')
        await self._deliver(
            chat_id,
            "\n".join(parts),
            image_url=item.image_url,
            reply_markup=self._deal_buttons(item, watch_id),
        )

    async def notify_arbitrage(self, chat_id: str, query: str, result: dict) -> None:
        home = result["home_currency"]

        def place(marketplace: str) -> str:
            return marketplace.replace("EBAY_", "")

        parts = [
            "<b>🌍 ARBITRAGE OPPORTUNITY</b>",
            html.escape(f"{result['gap_percent']:.0f}% gap on “{query}”"),
            "",
            f"🟢 Buy in {html.escape(place(result['buy_marketplace']))}: "
            f"{html.escape(format_money(result['buy_price'], result['buy_currency']))} "
            f"(≈ {html.escape(format_money(result['buy_home'], home))})",
            f"🔴 Sell in {html.escape(place(result['sell_marketplace']))}: "
            f"{html.escape(format_money(result['sell_price'], result['sell_currency']))} "
            f"(≈ {html.escape(format_money(result['sell_home'], home))})",
            "",
            "⚠️ Before fees, shipping, import duty & FX — sanity-check the net.",
        ]
        await self._deliver(chat_id, "\n".join(parts))

    def _deal_buttons(self, item: MarketItem, watch_id: int | None) -> dict | None:
        """Inline keyboard for a deal: open the listing, mute the variant, or
        flag it as the wrong item (which teaches the watch to exclude it)."""
        rows: list[list[dict]] = [[{"text": "🔗 Open on eBay", "url": item.url}]]
        if watch_id is not None:
            rows.append(
                [
                    {"text": "🙈 Mute variant", "callback_data": f"mv:{watch_id}:{item.item_id}"},
                    {"text": "🚫 Not the item", "callback_data": f"bl:{watch_id}:{item.item_id}"},
                ]
            )
        return {"inline_keyboard": rows}

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
        except Exception:
            log.debug("answerCallbackQuery failed", exc_info=True)

    async def poll_commands(
        self, handler: CommandHandler, callback_handler: CallbackHandlerType | None = None
    ) -> None:
        while True:
            try:
                await self._poll_once(handler, callback_handler)
            except Exception:
                log.exception("Telegram command polling failed")
                await asyncio.sleep(10)

    async def _poll_once(
        self, handler: CommandHandler, callback_handler: CallbackHandlerType | None = None
    ) -> None:
        response = await self.client.get(
            f"{self.base_url}/getUpdates",
            params={
                "offset": self.offset,
                "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=35,
        )
        response.raise_for_status()
        payload = response.json()
        for update in payload.get("result", []):
            self.offset = max(self.offset, int(update["update_id"]) + 1)
            if update.get("callback_query"):
                await self._handle_callback(update["callback_query"], callback_handler)
                continue
            message: dict[str, Any] = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            sender = message.get("from") or {}
            username = sender.get("username")
            if not chat_id or not text.startswith("/"):
                continue
            command, _, arg = text.partition(" ")
            reply = await handler(chat_id, username, command.split("@", 1)[0].lower(), arg.strip())
            if reply:
                await self.send_message(chat_id, html.escape(reply), disable_preview=True)

    async def _handle_callback(
        self, callback: dict[str, Any], callback_handler: CallbackHandlerType | None
    ) -> None:
        callback_id = str(callback.get("id", ""))
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        username = (callback.get("from") or {}).get("username")
        if callback_handler is None or not chat_id or not data:
            await self.answer_callback(callback_id)
            return
        toast = await callback_handler(chat_id, username, data)
        await self.answer_callback(callback_id, toast or "")
