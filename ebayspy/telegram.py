from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .models import Listing

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, str | None, str, str], Awaitable[str]]


class TelegramBot:
    def __init__(self, token: str, timeout_seconds: int) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=timeout_seconds)
        self.offset = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def send_message(self, chat_id: str, text: str, disable_preview: bool = False) -> None:
        response = await self.client.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
        )
        response.raise_for_status()

    async def notify_listing(self, chat_id: str, listing: Listing) -> None:
        await self._notify_listing_event(chat_id, listing, title="New eBay listing")

    async def notify_ended_listing(self, chat_id: str, listing: Listing) -> None:
        await self._notify_listing_event(chat_id, listing, title="Ended or sold eBay listing")

    async def notify_quantity_increase(
        self, chat_id: str, listing: Listing, previous_quantity: int, current_quantity: int
    ) -> None:
        await self._notify_listing_event(
            chat_id,
            listing,
            title=f"eBay quantity increased: {previous_quantity} -> {current_quantity}",
        )

    async def _notify_listing_event(self, chat_id: str, listing: Listing, title: str) -> None:
        description = listing.description.strip()
        if len(description) > 500:
            description = description[:497].rstrip() + "..."
        parts = [
            f"<b>{html.escape(title)}</b>",
            f"<b>Title:</b> {html.escape(listing.title)}",
            f"<b>Price:</b> {html.escape(listing.price or 'Unknown')}",
            f"<b>Seller:</b> {html.escape(listing.seller)}",
        ]
        if listing.listing_type:
            parts.append(f"<b>Listing type:</b> {html.escape(listing.listing_type)}")
        if listing.category:
            parts.append(f"<b>Category:</b> {html.escape(listing.category)}")
        if listing.quantity_available:
            parts.append(f"<b>Qty available:</b> {html.escape(listing.quantity_available)}")
        if listing.listed_at:
            parts.append(f"<b>Listed:</b> {html.escape(listing.listed_at)}")
        if description:
            parts.append(f"<b>Description:</b> {html.escape(description)}")
        parts.append(f'<a href="{html.escape(listing.url)}">Open listing</a>')
        await self.send_message(chat_id, "\n".join(parts))

    async def poll_commands(self, handler: CommandHandler) -> None:
        while True:
            try:
                await self._poll_once(handler)
            except Exception:
                log.exception("Telegram command polling failed")
                await asyncio.sleep(10)

    async def _poll_once(self, handler: CommandHandler) -> None:
        response = await self.client.get(
            f"{self.base_url}/getUpdates",
            params={"offset": self.offset, "timeout": 30, "allowed_updates": json.dumps(["message"])},
            timeout=35,
        )
        response.raise_for_status()
        payload = response.json()
        for update in payload.get("result", []):
            self.offset = max(self.offset, int(update["update_id"]) + 1)
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
