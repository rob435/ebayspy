from __future__ import annotations

import asyncio
import logging
import signal

from .config import Config
from .ebay import EbayClient
from .models import Listing
from .storage import (
    Store,
    format_status_rows,
    is_valid_ebay_username,
    is_valid_telegram_username,
    normalize_ebay_username,
    normalize_telegram_username,
)
from .telegram import TelegramBot

log = logging.getLogger(__name__)


class EbaySpyService:
    def __init__(self, config: Config) -> None:
        config.require_telegram()
        config.require_ebay()
        self.config = config
        self.store = Store(config.sqlite_path)
        self.ebay = EbayClient(
            app_id=config.ebay_app_id,
            client_secret=config.ebay_client_secret,
            global_id=config.ebay_global_id,
            timeout_seconds=config.http_timeout_seconds,
            max_items=config.max_items_per_seller,
            detail_concurrency=config.detail_concurrency,
        )
        self.telegram = TelegramBot(config.telegram_bot_token, config.http_timeout_seconds)
        self.stop_event = asyncio.Event()
        self._check_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.telegram.close()
        await self.ebay.close()
        self.store.close()

    def seed_config_sellers(self) -> None:
        for seller in self.config.seed_sellers:
            self.store.add_seller(seller)

    def configured_chats(self) -> list[str]:
        chats = []
        if self.config.telegram_chat_id:
            chats.append(self.config.telegram_chat_id)
        for row in self.store.list_chat_rows():
            chat_id = str(row["chat_id"])
            username = row["username"]
            if chat_id not in chats and self.is_authorized_chat(chat_id, username):
                chats.append(chat_id)
        return chats

    def is_authorized_chat(self, chat_id: str, username: str | None) -> bool:
        allowed_usernames = self.allowed_usernames()
        if not self.config.telegram_allowed_chat_ids and not allowed_usernames:
            return True
        if chat_id in self.config.telegram_allowed_chat_ids:
            return True
        if normalize_telegram_username(username) in allowed_usernames:
            return True
        return False

    def is_admin_chat(self, chat_id: str, username: str | None) -> bool:
        if chat_id in self.config.telegram_allowed_chat_ids:
            return True
        return normalize_telegram_username(username) in self.config.telegram_allowed_usernames

    def allowed_usernames(self) -> set[str]:
        return set(self.config.telegram_allowed_usernames) | set(self.store.list_allowed_usernames())

    async def run_forever(self) -> None:
        self.seed_config_sellers()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        command_task = asyncio.create_task(self.telegram.poll_commands(self.handle_command))
        try:
            while not self.stop_event.is_set():
                await self.check_once()
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.config.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
            await self.close()

    async def check_once(self) -> int:
        async with self._check_lock:
            return await self._check_all_sellers()

    async def _check_all_sellers(self) -> int:
        self.seed_config_sellers()
        sellers = self.store.list_sellers()
        if not sellers:
            log.info("No sellers configured")
            return 0

        chats = self.configured_chats()
        if not chats:
            log.warning("No Telegram chats configured yet. Send /start to the bot.")

        total_alert_count = 0
        for seller_index, seller in enumerate(sellers):
            if seller_index and self.config.seller_check_delay_seconds > 0:
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=self.config.seller_check_delay_seconds,
                    )
                    break
                except TimeoutError:
                    pass

            seller_new_count = 0
            seller_ended_count = 0
            try:
                listings = await self.ebay.seller_listings(seller)
            except Exception as exc:
                log.exception("Failed checking seller %s", seller)
                self.store.record_check(seller, listing_count=0, new_count=0, error=str(exc))
                continue

            current_seller = await self._detect_username_change(seller, listings, chats)
            if current_seller:
                seller = current_seller

            first_scan = not self.store.seller_has_successful_check(seller)
            notify_existing = self.config.notify_existing_on_first_run or not first_scan
            active_item_ids = {listing.item_id for listing in listings}
            listings_truncated = len(listings) >= self.config.max_items_per_seller
            for listing in reversed(listings):
                if self.store.is_seen(listing.item_id):
                    continue
                if notify_existing:
                    sent = await self._notify_chats(chats, listing)
                    if sent:
                        seller_new_count += 1
                        total_alert_count += 1
                self.store.mark_seen(listing, notified=notify_existing)

            if not first_scan:
                for listing, previous_quantity, current_quantity in (
                    self.store.quantity_increase_candidates(listings)
                ):
                    sent = await self._notify_quantity_increase_chats(
                        chats, listing, previous_quantity, current_quantity
                    )
                    if sent:
                        total_alert_count += 1

                if not listings_truncated:
                    for listing in self.store.ended_candidates(seller, active_item_ids):
                        active = await self.ebay.item_active(listing.item_id)
                        if active is True:
                            log.info(
                                "Suppressing ended alert for %s on %s: "
                                "still active per getItem",
                                listing.item_id,
                                seller,
                            )
                            continue
                        if active is None:
                            log.warning(
                                "Skipping ended alert for %s on %s: "
                                "getItem could not verify",
                                listing.item_id,
                                seller,
                            )
                            continue
                        sent = await self._notify_ended_chats(chats, listing)
                        if sent:
                            seller_ended_count += 1
                            total_alert_count += 1
                        self.store.mark_ended_notified(listing.item_id)

            self.store.upsert_active_listings(listings)
            self.store.record_check(seller, len(listings), seller_new_count, seller_ended_count)
            log.info(
                "Checked %s: %s active listings, %s new alerted, %s ended alerted",
                seller,
                len(listings),
                seller_new_count,
                seller_ended_count,
            )
        return total_alert_count

    async def _seed_seller_baseline(self, seller: str) -> int | None:
        try:
            listings = await self.ebay.seller_listings(seller)
        except Exception:
            log.exception("Failed seeding baseline for seller %s", seller)
            return None
        for listing in listings:
            self.store.mark_seen(listing, notified=False)
        self.store.upsert_active_listings(listings)
        self.store.record_check(seller, len(listings), new_count=0)
        return len(listings)

    async def _detect_username_change(
        self, watched_seller: str, listings: list[Listing], chats: list[str]
    ) -> str | None:
        observed = self._observed_changed_seller(watched_seller, listings)
        if not observed and not listings:
            observed = await self._probe_current_seller_from_known_items(watched_seller)
        if not observed:
            return None

        changed = self.store.rename_seller(watched_seller, observed)
        if changed:
            notice_key = f"seller_rename_notice:{watched_seller.lower()}:{observed.lower()}"
            if not self.store.get_meta(notice_key):
                await self._notify_text(
                    chats,
                    (
                        "Possible eBay username change detected.\n"
                        f"Updated watchlist: {watched_seller} -> {observed}"
                    ),
                )
                self.store.set_meta(notice_key, "sent")
            log.info("Detected possible seller username change: %s -> %s", watched_seller, observed)
        return observed

    def _observed_changed_seller(self, watched_seller: str, listings: list[Listing]) -> str | None:
        watched = watched_seller.lower()
        observed = {
            listing.seller.strip()
            for listing in listings
            if listing.seller.strip() and listing.seller.strip().lower() != watched
        }
        return sorted(observed, key=str.lower)[0] if len(observed) == 1 else None

    async def _probe_current_seller_from_known_items(self, watched_seller: str) -> str | None:
        watched = watched_seller.lower()
        observed: set[str] = set()
        for item_id in self.store.recent_active_item_ids(watched_seller):
            try:
                current_seller = await self.ebay.item_seller(item_id)
            except Exception:
                log.debug("Could not probe seller for item %s", item_id, exc_info=True)
                continue
            if current_seller and current_seller.lower() != watched:
                observed.add(current_seller)
        return sorted(observed, key=str.lower)[0] if len(observed) == 1 else None

    async def _notify_text(self, chats: list[str], text: str) -> None:
        for chat_id in chats:
            try:
                await self.telegram.send_message(chat_id, text, disable_preview=True)
            except Exception:
                log.exception("Failed sending Telegram text alert to chat %s", chat_id)

    async def _notify_chats(self, chats: list[str], listing: Listing) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_listing(chat_id, listing)
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram alert to chat %s", chat_id)
        return sent_any

    async def _notify_ended_chats(self, chats: list[str], listing: Listing) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_ended_listing(chat_id, listing)
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram ended alert to chat %s", chat_id)
        return sent_any

    async def _notify_quantity_increase_chats(
        self, chats: list[str], listing: Listing, previous_quantity: int, current_quantity: int
    ) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_quantity_increase(
                    chat_id, listing, previous_quantity, current_quantity
                )
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram quantity alert to chat %s", chat_id)
        return sent_any

    def status_text(self) -> str:
        return format_status_rows(self.store.status_rows())

    async def handle_command(
        self, chat_id: str, username: str | None, command: str, arg: str
    ) -> str:
        if not self.is_authorized_chat(chat_id, username):
            log.warning(
                "Rejected Telegram command %s from unauthorized chat %s username %s",
                command,
                chat_id,
                username or "",
            )
            return "This bot is invite-only."
        if command in {"/start", "/help"}:
            self.store.add_chat(chat_id, username)
            return (
                "ebayspy is connected.\n"
                "Commands: /add seller, /remove seller, /list, /status, /check, /help\n"
                "Admin: /invite @username, /uninvite @username, /invites"
            )
        if command == "/invite":
            if not self.is_admin_chat(chat_id, username):
                return "Only an admin can invite users."
            if not arg:
                return "Usage: /invite @username"
            invited = normalize_telegram_username(arg.split()[0])
            if not is_valid_telegram_username(invited):
                return "That does not look like a valid Telegram username."
            added = self.store.add_allowed_username(invited)
            return f"Invited @{invited}." if added else f"@{invited} was already invited."
        if command == "/uninvite":
            if not self.is_admin_chat(chat_id, username):
                return "Only an admin can remove invites."
            if not arg:
                return "Usage: /uninvite @username"
            removed_username = normalize_telegram_username(arg.split()[0])
            if not is_valid_telegram_username(removed_username):
                return "That does not look like a valid Telegram username."
            if removed_username in self.config.telegram_allowed_usernames:
                return f"@{removed_username} is a configured admin and cannot be removed from chat."
            removed = self.store.remove_allowed_username(removed_username)
            return f"Removed @{removed_username}." if removed else f"@{removed_username} was not invited."
        if command == "/invites":
            if not self.is_admin_chat(chat_id, username):
                return "Only an admin can list invites."
            dynamic = self.store.list_allowed_usernames()
            admins = sorted(self.config.telegram_allowed_usernames)
            lines = []
            if admins:
                lines.append("Admins: " + ", ".join(f"@{name}" for name in admins))
            lines.append(
                "Invited: " + ", ".join(f"@{name}" for name in dynamic)
                if dynamic
                else "Invited: none"
            )
            return "\n".join(lines)
        if command == "/add":
            if not arg:
                return "Usage: /add sellername"
            seller = normalize_ebay_username(arg)
            if not is_valid_ebay_username(seller):
                return "That does not look like a valid eBay username."
            if self.store.has_seller(seller):
                return f"Already watching seller: {seller}"
            try:
                exists = await self.ebay.seller_exists(seller)
            except Exception as exc:
                log.warning(
                    "Could not validate eBay seller %s with %s",
                    seller,
                    exc.__class__.__name__,
                )
                return "I could not verify that seller with eBay right now. Try again in a minute."
            if exists is None:
                log.warning("Adding eBay seller %s without external existence confirmation", seller)
            self.store.add_chat(chat_id, username)
            added = self.store.add_seller(seller)
            if not added:
                return f"Already watching seller: {seller}"
            baseline_count = await self._seed_seller_baseline(seller)
            baseline_text = (
                f" Seeded {baseline_count} current listings as already seen."
                if baseline_count is not None
                else " I added it, but could not seed the current listings yet."
            )
            if exists is None:
                return (
                    f"Added seller: {seller}. eBay would not confirm the profile, "
                    f"so the next check will verify listings.{baseline_text}"
                )
            return f"Added seller: {seller}.{baseline_text}"
        if command == "/remove":
            if not arg:
                return "Usage: /remove sellername"
            removed = self.store.remove_seller(arg.split()[0])
            return "Removed." if removed else "Seller was not in the watchlist."
        if command == "/list":
            sellers = self.store.list_sellers()
            return "Watching:\n" + "\n".join(sellers) if sellers else "No sellers yet."
        if command == "/status":
            return self.status_text()
        if command == "/check":
            self.store.add_chat(chat_id, username)
            count = await self.check_once()
            return f"Check complete. Alerts sent: {count}"
        return "Unknown command. Try /help"
