from __future__ import annotations

import asyncio
import html
import json
import logging
import signal
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import NamedTuple

from . import demand as demand_metrics
from . import insights as insights_metrics
from . import pricing
from . import risk as risk_metrics
from . import semantic
from . import vision
from .config import Config
from .ebay import MARKETPLACE_CURRENCY, EbayClient
from .fx import FxConverter
from .market import (
    choose_cluster_dimensions,
    cluster_by_variant,
    condition_bucket,
    deal_threshold,
    discount_percent_for,
    estimate_resale_profit,
    find_arbitrage,
    find_deals,
    label_from_values,
    market_price,
    offer_candidates,
    variant_key,
    variant_values,
)
from .matching import (
    accepts_damaged,
    attributes,
    canonicalize,
    content_tokens,
    filter_comparable,
    generation_of,
    lot_quantity,
    match_verdict,
    register_aliases,
    specified_dimensions,
    tokenize,
)
from .models import Listing, MarketItem
from .storage import (
    Store,
    _parse_iso_utc,
    format_health_rows,
    format_interval,
    format_market_rows,
    format_observed_rows,
    format_price_floor,
    format_seller_rows,
    format_status_rows,
    is_valid_ebay_username,
    is_valid_telegram_username,
    looks_like_floor,
    normalize_ebay_username,
    normalize_market_query,
    normalize_telegram_username,
    parse_interval,
    parse_price_floor,
    passes_min_price,
)
from .telegram import TelegramBot, format_money

log = logging.getLogger(__name__)

# A tappable list (/watches, /list, /observing) renders one 🗑 remove button per
# row. Cap how many buttons ride along so a huge list can't blow past Telegram's
# inline-keyboard limits; the message text still lists every entry.
_MAX_TABLE_BUTTONS = 50


class _Deal(NamedTuple):
    """A deal candidate. ``price`` is the market value to compare ``item`` against
    (per-variant median for normal deals, or N×unit-market for a lot of N)."""

    item: MarketItem
    price: float
    variant: str | None
    stage: str
    ending_soon: bool = False
    low_competition: bool = False
    lot_quantity: int | None = None
    # For "offer" candidates: the estimated accepted-offer price (the list price
    # itself is not the deal). None for every other stage.
    offer_estimate: float | None = None
    # The fair value the deal was judged against (carries its uncertainty).
    quote: pricing.FairValue | None = None


@dataclass
class _Snapshot:
    """One check's view of a watched market, from raw sample to quotes."""

    items: list[MarketItem]  # everything the search returned
    comparable: list[MarketItem]  # the watched product (fixed-price + auctions)
    fixed: list[MarketItem]
    auctions: list[MarketItem]
    dimensions: list[str]  # variant dimensions the market is split on
    clusters: dict[str, list[MarketItem]]  # fixed-price listings per variant
    truncated: bool  # the search returned a full page (a window, not the market)
    quotes: dict[str, pricing.FairValue] = field(default_factory=dict)
    sold_tag: str | None = None  # real sales-velocity read, when available
    comps: dict[str, list[float]] = field(default_factory=dict)  # recent sold prices
    rejected: dict[str, str] = field(default_factory=dict)  # item_id -> why (dry run)


class EbaySpyService:
    def __init__(self, config: Config, *, store=None, ebay=None, telegram=None) -> None:
        """Wire the service. Collaborators can be injected (a dry run uses an
        in-memory store, a replayed or live eBay client and a recording
        Telegram); credentials are only required for the ones built here."""
        if telegram is None:
            config.require_telegram()
        if ebay is None:
            config.require_ebay()
        self.config = config
        self.store = store if store is not None else Store(config.sqlite_path)
        self.ebay = ebay if ebay is not None else EbayClient(
            app_id=config.ebay_app_id,
            client_secret=config.ebay_client_secret,
            global_id=config.ebay_global_id,
            timeout_seconds=config.http_timeout_seconds,
            max_items=config.max_items_per_seller,
            detail_concurrency=config.detail_concurrency,
        )
        self.telegram = telegram if telegram is not None else TelegramBot(
            config.telegram_bot_token,
            config.http_timeout_seconds,
            config.ebay_global_id,
            config.telegram_send_photos,
        )
        self.stop_event = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._started_at = datetime.now(timezone.utc)
        # watch_id -> tight poll interval while an auction is near its end
        self._auction_turbo: dict[int, int] = {}
        self._arbitrage_due: dict[int, float] = {}
        self.fx = FxConverter(dict(config.fx_rates))
        register_aliases(config.market_aliases)
        if not config.market_semantic:
            semantic.disable()
        if not config.market_vision:
            vision.disable()

    async def close(self) -> None:
        await self.telegram.close()
        await self.ebay.close()
        self.store.close()

    def seed_config_sellers(self) -> None:
        for seller in self.config.seed_sellers:
            self.store.add_seller(seller)

    def seed_observe_sellers(self) -> None:
        for seller in self.config.observe_sellers:
            self.store.add_observed_seller(seller)

    def seed_market_watches(self) -> None:
        for query in self.config.market_watches:
            if not self.store.has_market_watch(query):
                self.store.add_market_watch(query)

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
        self.seed_observe_sellers()
        self.seed_market_watches()
        await self.fx.refresh(self.ebay.client)
        loop = asyncio.get_running_loop()
        # Warm the CLIP model off the event loop so the first deal check (and the
        # /help command, which froze on the inline model download before) never
        # blocks on the load. Fire-and-forget: failure just leaves vision lazy.
        # Held on self so the task isn't garbage-collected mid-load.
        if self.config.market_vision and self.config.market_vision_preload:
            self._preload_task = asyncio.create_task(self._preload_vision())
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        command_task = asyncio.create_task(
            self.telegram.poll_commands(self.handle_command, self.handle_callback)
        )
        observe_task = asyncio.create_task(self.run_observers())
        market_task = asyncio.create_task(self.run_market_watchers())
        next_backup = (
            loop.time() + self.config.backup_interval_seconds
            if self.config.backup_interval_seconds > 0
            else None
        )
        try:
            while not self.stop_event.is_set():
                await self.check_once()
                if next_backup is not None and loop.time() >= next_backup:
                    try:
                        path = self.store.backup(self.config.backup_dir, self.config.backup_keep)
                        log.info("Wrote DB backup %s", path)
                    except Exception:
                        log.exception("DB backup failed")
                    next_backup = loop.time() + self.config.backup_interval_seconds
                await self._maybe_send_heartbeat()
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.config.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            command_task.cancel()
            observe_task.cancel()
            market_task.cancel()
            await asyncio.gather(
                command_task, observe_task, market_task, return_exceptions=True
            )
            await self.close()

    async def check_once(self) -> int:
        async with self._check_lock:
            count = await self._check_all_sellers()
        self._record_poll_health(count)
        return count

    def _record_poll_health(self, alert_count: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # A poll in which every seller errored is not a healthy poll: leave the
        # "last ok" stamp alone so /health and the heartbeat show the outage.
        if not getattr(self, "_poll_all_failed", False):
            self.store.set_meta("last_poll_ok_at", now)
        self.store.set_meta("last_poll_alert_count", str(alert_count))
        if alert_count > 0:
            self.store.set_meta("last_alert_at", now)

    def health_text(self) -> str:
        return format_health_rows(
            self.store.health_snapshot(),
            started_at=self._started_at,
            heartbeat_enabled=self.config.heartbeat_enabled,
        )

    async def _maybe_send_heartbeat(self) -> None:
        """Send a daily liveness heartbeat when enabled and nothing has been heard
        within the interval, so a silent crash/stall is noticed."""
        if not self.config.heartbeat_enabled:
            return
        interval = self.config.heartbeat_interval_seconds
        now = datetime.now(timezone.utc)
        for key in ("last_alert_at", "last_heartbeat_at"):
            stamp = self.store.get_meta(key)
            parsed = _parse_iso_utc(stamp) if stamp else None
            if parsed is None:
                continue
            elapsed = (now - parsed).total_seconds()
            # A future-dated stamp (clock skew after a restore) must NOT count as
            # recent liveness, or a real stall would stay silent.
            if 0 <= elapsed < interval:
                return  # recent activity already proves liveness
        await self._notify_text(self.configured_chats(), "💓 Heartbeat\n" + self.health_text())
        self.store.set_meta("last_heartbeat_at", now.isoformat())

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
        failed_sellers = 0
        self._poll_all_failed = False
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
                self.store.record_check(
                    seller, listing_count=0, new_count=0,
                    error=str(exc) or exc.__class__.__name__,
                )
                failed_sellers += 1
                self._poll_all_failed = failed_sellers == len(sellers)
                continue

            current_seller = await self._detect_username_change(seller, listings, chats)
            if current_seller:
                seller = current_seller

            # Items below the seller's price floor never alert (new, restock, drop
            # or ended) — that's how a spammy seller's cheap junk is filtered out.
            min_price = self.store.get_seller_min_price(seller)
            first_scan = not self.store.seller_has_successful_check(seller)
            notify_existing = self.config.notify_existing_on_first_run or not first_scan
            active_item_ids = {listing.item_id for listing in listings}
            listings_truncated = len(listings) >= self.config.max_items_per_seller
            for listing in reversed(listings):
                if self.store.is_seen(listing.item_id):
                    continue
                notified = False
                if notify_existing and chats and passes_min_price(listing.price, min_price):
                    sent = await self._notify_chats(chats, listing)
                    if not sent:
                        # Every chat send failed (e.g. Telegram outage); leave the
                        # item unseen so the next poll retries instead of silently
                        # dropping a genuinely new listing.
                        continue
                    seller_new_count += 1
                    total_alert_count += 1
                    notified = True
                # With no chat configured yet, seed silently so a chat added later
                # starts fresh rather than being flooded with the whole backlog.
                self.store.mark_seen(listing, notified=notified)

            # Items whose restock / price-drop alert failed to send keep their old
            # stored value, so the next poll sees the change again and retries.
            undelivered_restock: set[str] = set()
            undelivered_drop: set[str] = set()
            if not first_scan:
                for listing, previous_quantity, current_quantity in (
                    self.store.quantity_increase_candidates(listings)
                ):
                    if not passes_min_price(listing.price, min_price):
                        continue
                    sent = await self._notify_quantity_increase_chats(
                        chats, listing, previous_quantity, current_quantity
                    )
                    if sent:
                        total_alert_count += 1
                    elif chats:
                        undelivered_restock.add(listing.item_id)

                # Price drops are read BEFORE upsert_active_listings overwrites the
                # stored price; the floor is recorded only on a successful send so a
                # failed alert retries next poll instead of being lost.
                for listing, old_price, new_price, pct in self.store.price_drop_candidates(
                    listings, self.config.seller_price_drop_percent
                ):
                    # Compare the post-drop price against the floor: a drop that lands
                    # below the threshold is still junk we don't want to hear about.
                    if min_price and new_price < min_price:
                        continue
                    sent = await self._notify_price_drop_chats(
                        chats, listing, old_price, new_price, pct
                    )
                    if sent:
                        self.store.mark_price_drop_alerted(listing.item_id, new_price)
                        total_alert_count += 1
                    elif chats:
                        undelivered_drop.add(listing.item_id)

                if not listings_truncated:
                    for listing, last_seen_at in self.store.ended_candidates(
                        seller, active_item_ids
                    ):
                        if not passes_min_price(listing.price, min_price):
                            continue
                        active, ended_at = await self.ebay.item_ended_state(listing.item_id)
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
                        sent = await self._notify_ended_chats(
                            chats, listing, ended_at or last_seen_at
                        )
                        if sent:
                            seller_ended_count += 1
                            total_alert_count += 1
                            # Only suppress future ended alerts once the listing's
                            # single ended notification has actually been delivered;
                            # a failed send is retried on the next poll.
                            self.store.mark_ended_notified(listing.item_id)

            self.store.upsert_active_listings(
                listings, keep_price=undelivered_drop, keep_quantity=undelivered_restock
            )
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

    async def run_observers(self) -> None:
        """Fast lane: poll observe-list sellers for newly listed items only.

        Each seller has its own interval (or the configured default). Only the
        cheap search call runs every tick; per-item detail is fetched solely for
        listings that are genuinely new, so a fast cadence stays affordable.
        """
        loop = asyncio.get_running_loop()
        next_due: dict[str, float] = {}
        while not self.stop_event.is_set():
            now = loop.time()
            rows = self.store.list_observed_sellers()
            current = {row["username"]: row for row in rows}
            for username in [key for key in next_due if key not in current]:
                del next_due[username]
            if current:
                chats = self.configured_chats()
                for username, row in current.items():
                    if self.stop_event.is_set():
                        break
                    due_at = next_due.get(username, now)
                    if loop.time() >= due_at:
                        await self._observe_seller(username, chats)
                        next_due[username] = loop.time() + self._observe_interval_for(row)
                    else:
                        next_due[username] = due_at
            sleep_for = self._observe_sleep_seconds(next_due, loop.time())
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=sleep_for)
            except TimeoutError:
                pass

    def _observe_interval_for(self, row) -> int:
        interval = row["interval_seconds"] or self.config.observe_interval_seconds
        return max(self.config.observe_min_interval_seconds, interval)

    @staticmethod
    def _observe_sleep_seconds(next_due: dict[str, float], now: float) -> float:
        # Cap the wait so sellers added or retuned from Telegram are picked up promptly.
        tick = 10.0
        if not next_due:
            return tick
        return max(1.0, min(tick, min(next_due.values()) - now))

    async def _observe_seller(self, seller: str, chats: list[str]) -> int:
        try:
            listings = await self.ebay.seller_listings(seller, hydrate=False)
        except Exception as exc:
            log.exception("Failed observing seller %s", seller)
            self.store.record_observe_check(
                seller, new_count=0, error=str(exc) or exc.__class__.__name__
            )
            return 0

        min_price = self.store.get_observed_min_price(seller)
        first_observe = not self.store.observed_seller_has_successful_check(seller)
        new_listings = [
            listing for listing in reversed(listings) if not self.store.is_seen(listing.item_id)
        ]
        sent_count = 0
        if new_listings and first_observe:
            # Seed the baseline silently so we only alert on items listed from now on.
            for listing in new_listings:
                self.store.mark_seen(listing, notified=False)
        elif new_listings:
            # Drop sub-floor items here (the search summary already carries a price),
            # so we never spend a per-item hydrate call on junk we won't alert on.
            for listing in new_listings:
                if not passes_min_price(listing.price, min_price):
                    self.store.mark_seen(listing, notified=False)
            to_alert = [
                listing for listing in new_listings if passes_min_price(listing.price, min_price)
            ]
            for listing in await self.ebay.hydrate_listings(to_alert):
                sent = await self._notify_chats(chats, listing)
                if not sent:
                    # Leave it unseen so the next pass retries the alert instead
                    # of silently losing it to a transient Telegram failure.
                    continue
                self.store.mark_seen(listing, notified=True)
                sent_count += 1
        self.store.record_observe_check(seller, new_count=sent_count)
        log.info(
            "Observed %s: %s active listings, %s new alerted", seller, len(listings), sent_count
        )
        return sent_count

    async def _seed_observe_baseline(self, seller: str) -> int | None:
        try:
            listings = await self.ebay.seller_listings(seller, hydrate=False)
        except Exception:
            log.exception("Failed seeding observe baseline for seller %s", seller)
            return None
        for listing in listings:
            self.store.mark_seen(listing, notified=False)
        self.store.record_observe_check(seller, new_count=0)
        return len(listings)

    async def run_market_watchers(self) -> None:
        """Periodically reprice each market watch and alert on below-market deals.

        One search call per watch per tick samples live Buy-It-Now listings; the
        market price and any deals are derived from that sample with no per-item
        detail calls, so the cadence stays cheap.
        """
        loop = asyncio.get_running_loop()
        next_due: dict[int, float] = {}
        while not self.stop_event.is_set():
            rows = self.store.list_market_watches()
            current = {row["id"]: row for row in rows}
            for watch_id in [key for key in next_due if key not in current]:
                del next_due[watch_id]
            if current:
                broadcast = self.configured_chats()
                for watch_id, row in current.items():
                    if self.stop_event.is_set():
                        break
                    due_at = next_due.get(watch_id, loop.time())
                    if loop.time() >= due_at:
                        watch_chats = self._chats_for_watch(row, broadcast)
                        if watch_chats:
                            # Isolate each watch: an unexpected failure in one must
                            # not kill the whole market-watcher task (it is never
                            # restarted), which would silently end all deal alerts.
                            try:
                                await self._check_market_watch(row, watch_chats)
                                await self._maybe_check_arbitrage(row, watch_chats, loop.time())
                            except Exception:
                                log.exception("Market watch %s failed", watch_id)
                        # A watch with an auction near its end polls on the tight
                        # turbo cadence so the final-call snipe alert lands in time.
                        interval = self._auction_turbo.get(watch_id) or self._market_interval_for(row)
                        next_due[watch_id] = loop.time() + interval
                    else:
                        next_due[watch_id] = due_at
            sleep_for = self._observe_sleep_seconds(next_due, loop.time())
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=sleep_for)
            except TimeoutError:
                pass

    def _market_interval_for(self, row) -> int:
        interval = row["interval_seconds"] or self.config.market_interval_seconds
        return max(self.config.market_min_interval_seconds, interval)

    def _chats_for_watch(self, row, broadcast: list[str]) -> list[str]:
        """A watch added by a specific user alerts that user plus configured
        admins; one with no owner (env/CLI seeded) broadcasts to everyone."""
        owner = row["owner_chat_id"]
        if not owner:
            return broadcast
        # An owner who has since been uninvited must stop receiving alerts.
        chats = [owner] if self._chat_still_authorized(owner) else []
        for admin in self.config.telegram_allowed_chat_ids:
            if admin not in chats:
                chats.append(admin)
        return chats

    def _chat_still_authorized(self, chat_id: str) -> bool:
        if chat_id == self.config.telegram_chat_id:
            return True
        usernames = {str(r["chat_id"]): r["username"] for r in self.store.list_chat_rows()}
        return self.is_authorized_chat(chat_id, usernames.get(chat_id))

    def _home_marketplace(self) -> str:
        return self.config.ebay_global_id.upper().replace("-", "_")

    def _home_country(self) -> str:
        return self._home_marketplace().replace("EBAY_", "")

    async def _maybe_check_arbitrage(self, row, chats: list[str], now: float) -> None:
        """Run the cross-marketplace check on its own slower cadence (it costs one
        search per marketplace), only for watches that opted in."""
        if not (row["markets"] or "").strip():
            return
        if now < self._arbitrage_due.get(row["id"], 0.0):
            return
        self._arbitrage_due[row["id"]] = now + self.config.market_arbitrage_interval_seconds
        await self._check_arbitrage(row, chats)

    async def _check_arbitrage(self, row, chats: list[str]) -> None:
        """Compare the same item across marketplaces and flag a buy-low/sell-high gap."""
        extra = [m.strip().upper() for m in (row["markets"] or "").split(",") if m.strip()]
        home = self._home_marketplace()
        marketplaces = list(dict.fromkeys([home, *extra]))
        if len(marketplaces) < 2:
            return
        search_query = canonicalize(row["query"])
        home_currency = MARKETPLACE_CURRENCY.get(home, "USD")
        quotes: list[tuple[str, float, str]] = []
        for marketplace in marketplaces:
            currency = MARKETPLACE_CURRENCY.get(marketplace, "USD")
            try:
                # The watch's price bounds are in the home currency; each
                # marketplace filters in its own.
                items = await self.ebay.search_market(
                    search_query,
                    condition=row["condition"],
                    min_price=self._convert_bound(row["min_price"], home_currency, currency),
                    max_price=self._convert_bound(row["max_price"], home_currency, currency),
                    limit=self.config.market_sample_size,
                    category_ids=row["category_id"] or None,
                    marketplace=marketplace,
                )
            except Exception:
                log.warning("Arbitrage search failed for %s on %s", row["query"], marketplace)
                continue
            fixed = [item for item in self._comparable(row, items) if not item.is_auction]
            price = market_price(item.total_price for item in fixed)
            if price is not None and len(fixed) >= self.config.market_min_sample:
                quotes.append((marketplace, price, currency))
        if len(quotes) < 2:
            return
        result = find_arbitrage(
            quotes,
            self.fx.convert,
            self.config.market_arbitrage_threshold,
            home_currency,
        )
        if result:
            for chat_id in chats:
                try:
                    await self.telegram.notify_arbitrage(chat_id, row["query"], result)
                except Exception:
                    log.exception("Failed sending arbitrage alert to %s", chat_id)

    def _convert_bound(self, value: float | None, frm: str, to: str) -> float | None:
        """A price bound in another marketplace's currency (None = unbounded,
        including when the rate is unknown — better no bound than a wrong one)."""
        if value is None or frm == to:
            return value
        return self.fx.convert(value, frm, to)

    def _watch_exclude_terms(self, row) -> list[str]:
        raw = row["exclude_terms"] or ""
        return [term.strip() for term in raw.split(",") if term.strip()]

    def _comparable(
        self,
        row,
        items: list,
        *,
        allow_lots: bool = False,
        rejected: dict[str, str] | None = None,
    ) -> list:
        """Listings that are the watched product: the title matcher, minus what
        the user blocked, minus for-parts units (unless the watch hunts them),
        minus strays from outside the product's dominant eBay category.

        Pass ``rejected`` to have it filled with item_id -> why each dropped
        listing was dropped (used by the dry run; costs nothing otherwise).
        """
        rules = {
            "extra_excludes": self._watch_exclude_terms(row),
            "coverage": self.config.market_match_coverage,
            "fuzzy_threshold": self.config.market_fuzzy_threshold,
            "allow_lots": allow_lots,
        }
        blocked = self.store.blocked_item_ids(row["id"])
        wants_damaged = accepts_damaged(row["query"])
        matched = []
        text_ok = filter_comparable(
            row["query"], items, semantic_threshold=self._semantic_threshold(), **rules
        )
        text_ok = self._reinstate_media(row["query"], items, text_ok, rules)
        for item in text_ok:
            if item.item_id in blocked:
                reason = "blocked: flagged not-the-item"
            elif not wants_damaged and condition_bucket(
                getattr(item, "condition", ""), getattr(item, "condition_id", "")
            ) == "parts":
                reason = "condition: for parts or not working"
            else:
                matched.append(item)
                continue
            if rejected is not None:
                rejected[item.item_id] = reason
        if rejected is not None:
            passed = {id(item) for item in text_ok}
            for item in items:
                if id(item) not in passed:
                    rejected[item.item_id] = match_verdict(
                        row["query"], item.title, **rules
                    ).reason
        # Stamp each match with the generation its title states relative to this
        # query, so a watch that pins none prices each generation separately.
        matched = [
            replace(item, generation=generation_of(row["query"], item.title)) for item in matched
        ]
        if allow_lots or row["category_id"]:
            return matched
        kept = self._category_consensus(matched)
        if rejected is not None and len(kept) != len(matched):
            keeping = {id(item) for item in kept}
            for item in matched:
                if id(item) not in keeping:
                    rejected[item.item_id] = (
                        f"category: filed under “{getattr(item, 'category_name', '')}”, "
                        "not the market's main category"
                    )
        return kept

    def _reinstate_media(self, query: str, items: list, accepted: list, rules: dict) -> list:
        """Give listings rejected only for saying "game" or "book" a second look.

        On a console watch "… Switch Game" is media *for* the product; on a
        watch for a game it *is* the product. No title rule can tell which, but
        prices can: games trade at a fraction of the console, whereas listings
        of the watched game cost the same whether or not their title happens to
        say "Game". So media-flagged listings that pass every other rule are
        taken back when they trade at the level of the accepted ones (or, when
        nothing else was accepted, when there are enough of them to be a market).
        """
        taken = {id(item) for item in accepted}
        flagged = [
            item for item in items
            if id(item) not in taken
            and match_verdict(query, item.title, **rules).reason.startswith("media:")
        ]
        if not flagged:
            return accepted
        candidates = filter_comparable(query, flagged, allow_media=True, **rules)
        level = market_price(item.total_price for item in candidates)
        if not level:
            return accepted
        base = market_price(item.total_price for item in accepted)
        if base:
            if not 0.6 <= level / base <= 1 / 0.6:
                return accepted
        elif len(candidates) < self.config.market_min_sample:
            return accepted
        taken |= {id(item) for item in candidates}
        return [item for item in items if id(item) in taken]

    def _category_consensus(self, items: list) -> list:
        """Drop listings outside the dominant category when one clearly dominates.

        Accessories and parts live in their own eBay categories, so once the
        title matcher has narrowed the sample, a listing filed somewhere else is
        usually not the product — a vocabulary-free check that works in any
        niche. Does nothing when no category holds a clear majority (a product
        genuinely split across two) or when the watch pins a category itself.
        """
        share = self.config.market_category_consensus
        if share <= 0 or len(items) < self.config.market_min_sample:
            return items
        cat_id, _name, top = self._dominant_category(items)
        if not cat_id or top < share:
            return items
        return [
            item for item in items
            if not getattr(item, "category_id", "") or item.category_id == cat_id
        ]

    async def _sample_market(self, row, *, explain: bool = False) -> _Snapshot:
        """Search, narrow to comparable listings, and split them into per-variant
        clusters. Pricing happens in :meth:`_quote_market`. With ``explain`` the
        snapshot also records why each rejected listing was rejected."""
        query = row["query"]
        category_ids = row["category_id"] or None
        include_auctions = self._watch_auctions_enabled(row)
        search_query = canonicalize(query)
        limit = self.config.market_sample_size
        items = await self.ebay.search_market(
            search_query,
            condition=row["condition"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            limit=limit,
            category_ids=category_ids,
            include_auctions=include_auctions,
        )
        # When eBay reports more matches than it returned (or, failing that, a
        # full page came back), the sample is a window onto a larger market, so
        # a listing dropping out of it is not evidence that the listing ended.
        total = getattr(items, "total", None)
        truncated = (
            total > len(items) if total is not None
            else len(items) >= min(max(limit, 1), 200)
        )
        if self.config.market_deal_scan:
            # Optional supplementary cheapest-first pass to catch deep-page deals.
            extra = await self.ebay.search_market(
                search_query,
                condition=row["condition"],
                min_price=row["min_price"],
                max_price=row["max_price"],
                limit=limit,
                sort="price",
                category_ids=category_ids,
                include_auctions=include_auctions,
            )
            seen = {item.item_id for item in items}
            items.extend(item for item in extra if item.item_id not in seen)
        rejected: dict[str, str] | None = {} if explain else None
        comparable = self._comparable(row, items, rejected=rejected)
        # Enrich with eBay's structured catalog data (aspects, ePID…) so variant
        # clustering uses authoritative values instead of guessing from titles.
        # The client caches per listing, so each one costs a call once, not once
        # per check, and the per-check budget rolls forward through the sample.
        if self.config.market_hydrate:
            comparable = await self.ebay.hydrate_market_items(
                comparable, self.config.market_hydrate_limit
            )
        # Fair value is set by fixed-price listings only; an auction's live bid
        # is artificially low mid-auction and would drag it down.
        fixed = [item for item in comparable if not item.is_auction]
        auctions = [item for item in comparable if item.is_auction]
        specified = specified_dimensions(query)
        if row["condition"]:
            specified.add("condition")
        previous = self.store.market_dimensions(row["id"])
        dimensions = choose_cluster_dimensions(
            fixed,
            specified,
            min_sample=self.config.market_min_sample,
            min_dispersion=self.config.market_min_dispersion,
            previous=previous,
        )
        if dimensions != previous:
            self.store.set_market_dimensions(row["id"], dimensions)
        return _Snapshot(
            items=items,
            comparable=comparable,
            fixed=fixed,
            auctions=auctions,
            dimensions=dimensions,
            clusters=cluster_by_variant(fixed, dimensions),
            truncated=truncated,
            rejected=rejected or {},
        )

    def _cleared_observations(self, row, dimensions: list[str]) -> dict[str, list[pricing.Obs]]:
        """Recently ended listings as transaction proxies, per current variant.

        Each is re-validated against today's matcher and exclude list (so a
        listing admitted under older rules, or since flagged "not the item",
        stops counting) and re-labelled under today's dimensions; newer endings
        weigh more.
        """
        window = self.config.market_cleared_window_days
        blocked = self.store.blocked_item_ids(row["id"])
        rows = [
            r for r in self.store.cleared_listings(row["id"], window)
            if r["title"] and r["item_id"] not in blocked
        ]
        if not rows:
            return {}
        valid = filter_comparable(
            row["query"],
            rows,
            key=lambda r: r["title"],
            extra_excludes=self._watch_exclude_terms(row),
            coverage=self.config.market_match_coverage,
            fuzzy_threshold=self.config.market_fuzzy_threshold,
        )
        out: dict[str, list[pricing.Obs]] = {}
        for r in valid:
            try:
                values = json.loads(r["attrs"])
            except (TypeError, ValueError):
                continue
            label = label_from_values(values, dimensions) if isinstance(values, dict) else None
            if label is None:
                continue
            weight = pricing.recency_weight(r["age_days"] or 0.0, window / 3)
            out.setdefault(label, []).append(pricing.Obs(r["price"], weight, r["seller"] or ""))
        return out

    async def _quote_market(self, row, snap: _Snapshot) -> None:
        """Price every variant that has enough evidence, fusing live asks with
        confirmed-ended listings and (when entitled) real sold prices."""
        cleared = self._cleared_observations(row, snap.dimensions)
        sold: dict[str, list[pricing.Obs]] = {}
        if self.config.market_price_source == "insights" and snap.clusters:
            sold = await self._fetch_sold(row, snap)
        for label, group in snap.clusters.items():
            fair = pricing.fair_value(
                (pricing.Obs(item.total_price, seller=item.seller) for item in group),
                cleared.get(label, ()),
                sold.get(label, ()),
            )
            # The gate counts effective evidence after clipping and after
            # discounting one seller's duplicates — not raw rows.
            evidence = sum(part.n_eff for part in fair.components) if fair else 0.0
            if fair is not None and evidence >= self.config.market_min_sample - 1e-9:
                snap.quotes[label] = fair
        self._store_headline(row["id"], snap)

    async def _fetch_sold(self, row, snap: _Snapshot) -> dict[str, list[pricing.Obs]]:
        """Real sold prices per variant from the Marketplace Insights API. Also
        fills the snapshot's sales-velocity tag and recent comps. Returns {} on
        any failure so the quote falls back to listings."""
        try:
            sold = await self.ebay.search_item_sales(
                canonicalize(row["query"]),
                condition=row["condition"],
                category_ids=row["category_id"] or None,
                limit=self.config.market_sample_size,
            )
        except Exception:
            log.warning("Insights sold-data lookup failed for %s; using listings", row["query"])
            return {}
        comparable = self._comparable(row, sold, allow_lots=False)
        # Sold prices are item-only while live totals include postage; add the
        # live sample's typical postage so the two are like for like.
        shipping = (
            statistics.median(item.shipping_cost or 0.0 for item in snap.fixed)
            if snap.fixed else 0.0
        )
        now = datetime.now(timezone.utc)
        out: dict[str, list[pricing.Obs]] = {}
        for label, group in cluster_by_variant(comparable, snap.dimensions).items():
            if label not in snap.clusters:
                continue
            recent = sorted(group, key=lambda s: s.sold_date or "", reverse=True)
            out[label] = [
                pricing.Obs(
                    sale.total_price + shipping,
                    pricing.recency_weight(self._age_days(sale.sold_date, now), 30.0),
                    sale.seller,
                )
                for sale in recent
            ]
            snap.comps[label] = [sale.total_price + shipping for sale in recent[:3]]
        snap.sold_tag, _ = insights_metrics.summarize_sold(comparable)
        return out

    @staticmethod
    def _age_days(value: str | None, now: datetime) -> float:
        parsed = _parse_iso_utc(value) if value else None
        return max(0.0, (now - parsed).total_seconds() / 86400) if parsed else 0.0

    def _store_headline(self, watch_id: int, snap: _Snapshot) -> None:
        """Persist the best-populated priced variant as the /watches headline."""
        if not snap.quotes:
            self.store.update_market_price(watch_id, None, len(snap.items), len(snap.fixed), None)
            return
        label = max(snap.quotes, key=lambda key: len(snap.clusters.get(key, ())))
        fair = snap.quotes[label]
        self.store.update_market_price(
            watch_id, fair.price, len(snap.items), len(snap.fixed), label or None,
            low=fair.low, high=fair.high, confidence=fair.confidence,
        )

    async def _record_lifecycle(self, row, snap: _Snapshot) -> None:
        """Record sightings of the fixed-price comparables and settle the ones
        that vanished — the raw material for demand and for cleared-price evidence.

        When the sample is the whole market, an absence beyond the grace period
        counts as an ending for the demand read. When it is only a window,
        absence proves nothing. Either way a bounded number are confirmed with
        getItem each check, and only confirmed endings inform the price.
        """
        watch_id = row["id"]
        self.store.record_market_sightings(
            watch_id,
            [
                (
                    item.item_id,
                    variant_key(item, snap.dimensions) or "",
                    item.total_price,
                    item.currency,
                    item.listed_at,
                    item.title,
                    item.seller,
                    json.dumps(variant_values(item)),
                )
                for item in snap.fixed
            ],
        )
        seen = {item.item_id for item in snap.fixed}
        grace = self.config.market_demand_grace_seconds
        if not snap.truncated:
            # Good enough for the demand read; NOT for pricing (below).
            self.store.mark_disappeared_listings(watch_id, seen, grace)
        # A listing also vanishes when a filter changes (a new category, a price
        # cap) or the matcher is retuned. Only a getItem that says "ended" turns
        # an absence into price evidence; one that says "still live" clears it.
        for item_id in self.store.disappeared_candidates(
            watch_id, seen, grace, self.config.market_verify_ended_limit
        ):
            active = await self.ebay.item_active(item_id)
            if active is not None:
                self.store.resolve_disappeared_listing(watch_id, item_id, ended=not active)
        self.store.prune_market_listings(watch_id)

    def _watch_auctions_enabled(self, row) -> bool:
        flag = row["include_auctions"]
        return self.config.market_auctions_default if flag is None else bool(flag)

    def _semantic_threshold(self) -> float | None:
        return self.config.market_semantic_threshold if self.config.market_semantic else None

    async def _preload_vision(self) -> None:
        """Background warm-up of the CLIP model so the first deal check is instant."""
        if await asyncio.to_thread(vision.preload):
            log.info("Vision model preloaded")

    async def _vision_check(
        self, item: MarketItem, query: str, reference_url: str | None = None
    ) -> tuple[bool, str]:
        """Optionally verify the listing photo. Returns (drop, note): drop=True when
        the image clearly isn't the product; note is a one-line image read (incl. a
        gem flag when the photo looks better than the listing's stated condition,
        and a stock-image match read when the watch has a reference image).
        Runs the CLIP work off the event loop; no-ops when vision is unavailable."""
        if not self.config.market_vision or not item.image_url or not vision.available():
            return False, ""
        # Strongest signal first: does the listing photo actually show the same
        # item as the reference stock image? A clear non-match drops the deal.
        ref_note = ""
        if self.config.market_vision_reference and reference_url:
            sim = await asyncio.to_thread(vision.image_match, reference_url, item.image_url)
            if sim is not None:
                if sim < self.config.market_vision_ref_threshold:
                    return True, ""  # photo doesn't match the actual item
                ref_note = f"📷 Matches the item ({sim * 100:.0f}%)"
        match = await asyncio.to_thread(vision.match_score, item.image_url, query)
        if match is not None and match < self.config.market_vision_match_threshold:
            return True, ""  # photo doesn't depict the product — likely mistitled/wrong
        flags = await asyncio.to_thread(vision.vision_flags, item.image_url, item.condition)
        if flags is None:
            return False, ref_note
        note = vision.compose_note(
            flags,
            item.condition,
            stock_threshold=self.config.market_vision_stock_threshold,
            damage_threshold=self.config.market_vision_damage_threshold,
            count_hint=self.config.market_vision_count_hint,
            count_threshold=self.config.market_vision_count_threshold,
        )
        return False, " · ".join(part for part in (ref_note, note) if part)

    async def _ensure_reference_image(
        self, row, comparable: list[MarketItem]
    ) -> str | None:
        """The watch's reference 'stock' image of the actual item. Uses a pinned
        one if set; otherwise auto-derives the medoid (most representative) photo
        from the comparable listings and persists it, so it's established once and
        reused free thereafter. Returns the reference URL (or None)."""
        existing = row["reference_image_url"]
        if existing:
            return existing
        if not (
            self.config.market_vision
            and self.config.market_vision_reference
            and self.config.market_vision_auto_reference
            and vision.available()
        ):
            return None
        urls = [item.image_url for item in comparable if item.image_url]
        if len(urls) < 3:  # too few photos to trust a medoid
            return None
        chosen = await asyncio.to_thread(
            vision.pick_reference, urls, sample=self.config.market_vision_reference_sample
        )
        if chosen:
            self.store.set_market_reference_image(row["id"], chosen)
            log.info("Auto-set reference image for watch %s: %s", row["id"], chosen)
        return chosen

    def _price_trend_text(self, watch_id: int, variant: str | None) -> str:
        pct = self.store.price_trend(watch_id, variant or "")
        if pct is None or abs(pct) < 1:
            return ""
        arrow = "📉" if pct < 0 else "📈"
        return f"{arrow} Market {pct:+.0f}% vs 7d ago"

    async def _finish_market_check(
        self, row, chats: list[str], *, deal_count: int, error: str | None = None,
        empty: bool = False,
    ) -> None:
        self.store.record_market_check(
            row["id"], deal_count=deal_count, error=error, empty=empty
        )
        problem = self.store.check_market_health(row["id"], self.config.market_health_threshold)
        if problem:
            await self._notify_text(
                chats, f"⚠️ Market watch #{row['id']} “{row['query']}” {problem}"
            )

    def _demand_summary(self, watch_id: int) -> tuple[str, str]:
        row = self.store.get_market_watch(watch_id)
        cadence = self._market_interval_for(row) if row is not None else 0
        stats = self.store.market_demand_stats(
            watch_id,
            self.config.market_demand_window_days,
            max(self.config.market_demand_grace_seconds, cadence),
        )
        return demand_metrics.summarize(
            stats,
            window_days=self.config.market_demand_window_days,
            min_events=self.config.market_demand_min_events,
        )

    def _seconds_until(self, end_date: str | None) -> float | None:
        if not end_date:
            return None
        try:
            parsed = datetime.fromisoformat(str(end_date).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed - datetime.now(timezone.utc)).total_seconds()

    def _auction_candidates(self, row, snap: _Snapshot, discount, muted) -> list[_Deal]:
        """Auction snipes below their own variant's fair value.

        Sends a heads-up the first time a bid is below market, and a separate
        final-call when it enters the snipe window; flags the watch for turbo
        polling so the final-call lands in time. An auction about to close with
        no/low competition qualifies at a gentler discount — few bids only mean
        "uncontested" once there is no time left for more to arrive.
        """
        watch_id = row["id"]
        ratio = self.config.market_min_deal_ratio
        snipe = self.config.market_snipe_window_seconds
        out: list[_Deal] = []
        for auction in snap.auctions:
            label = variant_key(auction, snap.dimensions)
            fair = snap.quotes.get(label) if label is not None else None
            if fair is None or self._is_muted(label, muted):
                continue
            secs = self._seconds_until(auction.end_date)
            if secs is not None and secs <= 0:
                continue  # already ended
            ending_soon = secs is not None and secs <= snipe
            low_comp = (
                ending_soon
                and auction.bid_count is not None
                and auction.bid_count <= self.config.market_low_bid_count
            )
            ceiling = deal_threshold(
                fair.price, discount, fair.predictive_sd, self.config.market_confidence_z
            )
            if low_comp:
                relaxed = min(discount, self.config.market_nobid_discount_percent)
                ceiling = max(ceiling, deal_threshold(fair.price, relaxed))
            if not (fair.price * ratio <= auction.total_price <= ceiling):
                continue
            # Set before the dedupe check: once the heads-up has gone out the
            # watch must KEEP polling tightly, or the final call is never seen.
            if secs is not None and secs <= snipe * 2:
                self._auction_turbo[watch_id] = self.config.market_turbo_interval_seconds
            stage = "final" if ending_soon else "deal"
            if self.store.deal_already_alerted(watch_id, auction.item_id, stage):
                continue
            out.append(
                _Deal(auction, fair.price, label or None, stage, ending_soon, low_comp, quote=fair)
            )
        return out

    def _watch_lots_enabled(self, row) -> bool:
        return bool(row["include_lots"])

    def _lot_candidates(self, row, snap: _Snapshot, discount, muted) -> list[_Deal]:
        """Job-lot/bundle deals: value a lot of N by N × the single-unit fair value.

        A lot is a listing the normal matcher rejected but the lot-tolerant one
        accepts. Anything that already prices as a single unit is excluded, so
        "console with 2 controllers" is never valued as two consoles.
        """
        if not self._watch_lots_enabled(row):
            return []
        watch_id = row["id"]
        ratio = self.config.market_min_deal_ratio
        singles = {item.item_id for item in snap.comparable}
        out: list[_Deal] = []
        for lot in self._comparable(row, snap.items, allow_lots=True):
            if lot.is_auction or lot.item_id in singles:
                continue
            quantity = lot_quantity(lot.title)
            if not quantity:
                continue
            label = variant_key(lot, snap.dimensions)
            fair = snap.quotes.get(label) if label is not None else None
            if fair is None or self._is_muted(label, muted):
                continue
            lot_value = fair.price * quantity
            ceiling = deal_threshold(
                lot_value, discount, fair.predictive_sd, self.config.market_confidence_z
            )
            if not (lot_value * ratio <= lot.total_price <= ceiling):
                continue
            if self.store.deal_already_alerted(watch_id, lot.item_id, "lot"):
                continue
            out.append(
                _Deal(lot, lot_value, label or None, "lot", lot_quantity=quantity, quote=fair)
            )
        return out

    @staticmethod
    def _is_muted(label: str, muted: set[str]) -> bool:
        """Whether a variant is muted. A mute names the attributes the user
        muted ("128gb"); it keeps applying when the market later splits on a
        further dimension and the label grows ("128gb · used")."""
        if label in muted:
            return True
        parts = set(label.split(" · ")) if label else set()
        return any(mute and set(mute.split(" · ")) <= parts for mute in muted)

    @staticmethod
    def _deal_score(deal: _Deal) -> float:
        """Edge in predictive standard deviations — comparable across variants,
        so a modest gap in a tight market can outrank a wide one in a noisy one."""
        paid = deal.offer_estimate if deal.offer_estimate is not None else deal.item.total_price
        spread = deal.quote.predictive_sd if deal.quote else pricing.MIN_SIGMA
        return pricing.edge_z(paid, deal.price, spread)

    def _deal_candidates(self, row, snap: _Snapshot, discount, muted) -> list[_Deal]:
        """Every un-alerted deal this check, strongest edge first, one per listing."""
        watch_id = row["id"]
        ratio = self.config.market_min_deal_ratio
        z = self.config.market_confidence_z
        found: list[_Deal] = []
        for label, fair in snap.quotes.items():
            if self._is_muted(label, muted):
                continue
            group = snap.clusters.get(label, [])
            for item in find_deals(
                group, fair.price, discount, ratio,
                predictive_sd=fair.predictive_sd, confidence_z=z,
            ):
                if not self.store.deal_already_alerted(watch_id, item.item_id, "deal"):
                    found.append(_Deal(item, fair.price, label or None, "deal", quote=fair))
            if not self.config.market_offer_aware:
                continue
            # Best-Offer candidates: the list price isn't a deal, but a plausible
            # accepted offer off that list price would be.
            ceiling = deal_threshold(fair.price, discount, fair.predictive_sd, z)
            for item, estimate in offer_candidates(
                group, fair.price, discount, self.config.market_expected_offer_discount,
                ratio, threshold=ceiling,
            ):
                if not self.store.deal_already_alerted(watch_id, item.item_id, "offer"):
                    found.append(
                        _Deal(item, fair.price, label or None, "offer",
                              offer_estimate=estimate, quote=fair)
                    )
        found.extend(self._auction_candidates(row, snap, discount, muted))
        found.extend(self._lot_candidates(row, snap, discount, muted))
        best: dict[str, _Deal] = {}
        for deal in found:
            current = best.get(deal.item.item_id)
            if current is None or self._deal_score(deal) > self._deal_score(current):
                best[deal.item.item_id] = deal
        return sorted(best.values(), key=self._deal_score, reverse=True)

    def _watch_discount(self, row) -> float:
        discount = (
            row["discount_percent"]
            if row["discount_percent"] is not None
            else self.config.market_discount_percent
        )
        if self.config.market_feedback_enabled:
            # 👍/👎 feedback nudges the required discount; flows through every deal
            # type since this single value drives fixed, auction and lot deals.
            discount = max(1, min(95, discount + self.store.get_discount_nudge(row["id"])))
        return discount

    async def _check_market_watch(self, row, chats: list[str]) -> int:
        # Turbo polling is re-armed by each check that still sees an auction
        # near its end. Cleared up front so a check that errors, or can no
        # longer price the market, cannot leave the watch polling every 45s.
        self._auction_turbo.pop(row["id"], None)
        try:
            snap = await self._sample_market(row)
            await self._quote_market(row, snap)
            # Lifecycle is recorded even when nothing can be priced yet: the
            # endings it captures are what eventually make a thin market priceable.
            await self._record_lifecycle(row, snap)
        except Exception as exc:
            log.exception("Failed pricing market watch %s (%s)", row["id"], row["query"])
            await self._finish_market_check(
                row, chats, deal_count=0, error=str(exc) or exc.__class__.__name__
            )
            return 0
        return await self._alert_deals(row, chats, snap)

    async def _alert_deals(self, row, chats: list[str], snap: _Snapshot) -> int:
        """Screen, rank and send the deals in a priced snapshot. Returns how many
        were sent. (Also the entry point a dry run drives, with a recording
        Telegram, so what it reports is exactly what would have been alerted.)"""
        watch_id = row["id"]
        query = row["query"]
        discount = self._watch_discount(row)
        quotes = snap.quotes
        if not quotes:
            await self._finish_market_check(row, chats, deal_count=0, empty=True)
            log.info(
                "Market watch %s (%s): %s/%s comparable across %s variant(s), "
                "none with the evidence needed to price (min %s) — holding",
                watch_id, query, len(snap.fixed), len(snap.items), len(snap.clusters),
                self.config.market_min_sample,
            )
            return 0

        for label, fair in quotes.items():
            self.store.record_price_sample(watch_id, label, fair.price)
        demand_tag, _ = self._demand_summary(watch_id)
        if snap.sold_tag:
            demand_tag = snap.sold_tag

        # The reference 'stock' image each listing photo is compared against,
        # established once per watch (pinned or auto-derived) and reused free.
        reference_url = await self._ensure_reference_image(row, snap.comparable)
        candidates = self._deal_candidates(
            row, snap, discount, self.store.muted_variants(watch_id)
        )
        first_check = not self.store.market_watch_has_successful_check(watch_id)
        sent_count = 0
        home_country = self._home_country()
        # The per-cycle cap counts alerts actually SENT. Listings that risk or
        # vision screens out must not occupy a slot, or a handful of scam
        # listings at the top of the ranking would block every real deal behind
        # them on every check, forever.
        for deal in candidates:
            if sent_count >= self.config.market_max_deals_per_cycle:
                break
            item, price, variant, fair = deal.item, deal.price, deal.variant, deal.quote
            lot_qty = deal.lot_quantity
            risk_score, risk_reasons = risk_metrics.assess(item, price, home_country)
            # ``>=`` so the documented suppression actually fires: the score is
            # capped at 100 and the default max is 100. Conservative by design
            # (only the worst tier); lower MARKET_RISK_MAX to suppress more.
            if risk_score >= self.config.market_risk_max:
                log.info(
                    "Suppressing high-risk deal %s on watch %s (risk %s: %s)",
                    item.item_id, watch_id, risk_score, "; ".join(risk_reasons),
                )
                continue
            risk_text = (
                f"⚠️ Risk {risk_score}/100 — {'; '.join(risk_reasons)}"
                if risk_score >= self.config.market_risk_warn and risk_reasons
                else ""
            )
            drop, vision_note = await self._vision_check(item, query, reference_url)
            if drop:
                log.info(
                    "Vision dropped deal %s on watch %s: image doesn't match the product",
                    item.item_id, watch_id,
                )
                # Remember the verdict so the photo is not re-examined every check.
                self.store.record_deal_alert(
                    watch_id, item.item_id, item.total_price, variant=variant,
                    title=item.title, stage="suppressed",
                )
                continue
            # Offer candidates are judged on the estimated accepted-offer price,
            # not the list price.
            paid = deal.offer_estimate if deal.offer_estimate is not None else item.total_price
            units = lot_qty or 1
            fee_fixed = self.config.market_resale_fee_fixed * units
            postage = self.config.market_resale_shipping * units
            profit, roi = estimate_resale_profit(
                price, paid, self.config.market_resale_fee_percent, fee_fixed, postage
            )
            quick_profit = None
            if fair is not None:
                quick_exit = pricing.quick_sale_price(price, fair.sigma)
                quick_profit, _ = estimate_resale_profit(
                    quick_exit, paid, self.config.market_resale_fee_percent, fee_fixed, postage
                )
            sent = await self._notify_deal_chats(
                chats,
                item,
                market_price=price,
                discount_percent=discount_percent_for(paid, price),
                query=query,
                variant=variant if snap.dimensions else None,
                profit=profit,
                roi=roi,
                watch_id=watch_id,
                ending_soon=deal.ending_soon,
                trend=self._price_trend_text(watch_id, variant),
                demand=demand_tag,
                comps=snap.comps.get(variant or ""),
                low_competition=deal.low_competition,
                risk=risk_text,
                lot_quantity=lot_qty,
                vision=vision_note,
                offer_estimate=deal.offer_estimate,
                quote=fair,
                quick_profit=quick_profit,
            )
            if sent:
                # Only mark the deal as alerted once it has actually been
                # delivered to at least one chat; otherwise a transient Telegram
                # failure would permanently suppress a real below-market deal.
                self.store.record_deal_alert(
                    watch_id, item.item_id, item.total_price, variant=variant,
                    title=item.title, stage=deal.stage,
                )
                sent_count += 1
        await self._finish_market_check(row, chats, deal_count=sent_count)
        log.info(
            "Market watch %s (%s): %s/%s comparable across %s priced variant(s)%s, "
            "%s deals alerted%s",
            watch_id,
            query,
            len(snap.fixed),
            len(snap.items),
            len(quotes),
            f" by {'+'.join(snap.dimensions)}" if snap.dimensions else "",
            sent_count,
            " (first check)" if first_check else "",
        )
        return sent_count

    async def _notify_deal_chats(self, chats: list[str], item: MarketItem, **details) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_deal(chat_id, item, **details)
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram deal alert to chat %s", chat_id)
        return sent_any

    async def _detect_username_change(
        self, watched_seller: str, listings: list[Listing], chats: list[str]
    ) -> str | None:
        observed = self._observed_changed_seller(watched_seller, listings)
        if not observed and not listings and self._rename_probe_due(watched_seller):
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

    def _rename_probe_due(self, seller: str) -> bool:
        """Whether to spend getItem calls checking an empty seller for a rename.

        A seller with nothing listed looks the same on every poll, so probing
        each time burns hundreds of calls a day for no new information; once a
        day is enough to catch a rename.
        """
        key = f"seller_rename_probe_at:{seller.lower()}"
        last = _parse_iso_utc(self.store.get_meta(key) or "")
        now = datetime.now(timezone.utc)
        if last is not None and (now - last).total_seconds() < 86400:
            return False
        self.store.set_meta(key, now.isoformat())
        return True

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
                await self.telegram.send_message(
                    chat_id, html.escape(text), disable_preview=True
                )
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

    async def _notify_ended_chats(
        self, chats: list[str], listing: Listing, ended_at: str | None = None
    ) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_ended_listing(chat_id, listing, ended_at)
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

    async def _notify_price_drop_chats(
        self, chats: list[str], listing: Listing, old_price: float, new_price: float, pct: float
    ) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_price_drop(chat_id, listing, old_price, new_price, pct)
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram price-drop alert to chat %s", chat_id)
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
                "\n"
                "WATCH a seller — alerts every poll: new, price drop, ended/sold, restock.\n"
                "/add seller [minprice] — e.g. /add techbargains_uk  ·  /add lithosale [100]\n"
                "/remove seller · /list · /status · /check · /health\n"
                "\n"
                "OBSERVE a seller — fast new-listing alerts only, on its own interval.\n"
                "/observe seller [interval] [minprice] — e.g. /observe techbargains_uk 90s [100]\n"
                "/unobserve seller · /observing · /interval seller <time>\n"
                "\n"
                "PRICE FLOOR — only alert on items at/above a price, to mute cheap spam.\n"
                "Add it in brackets when you /add or /observe, e.g. /add lithosale [100].\n"
                "/floor seller [100] retunes it later · /floor seller none clears it.\n"
                "\n"
                "MARKET DEALS — alert when something lists below the going rate.\n"
                "/watch terms [options]\n"
                "\n"
                "Options: condition:new|used · under:PRICE · discount:% · every:TIME · "
                "exclude:word · category:ID · auctions:on · lots:on · markets:GB,DE,US\n"
                "\n"
                "Examples:\n"
                "/watch dyson airblade hu02\n"
                "/watch iphone 13 pro condition:used under:450 discount:20\n"
                "/watch ps5 console auctions:on discount:25 — snipes auctions too\n"
                "/watch lego 75192 markets:GB,DE,US — cross-border arbitrage\n"
                "/watch airpods pro lots:on exclude:case — job lots, drops \"case\"\n"
                "\n"
                "Manage: /watches · /unwatch <id> · /quote <id> · /demand <id> · /refimage <id> <url>\n"
                "/quote <id> shows the working: fair value, confidence, the evidence "
                "behind it, and the price alerts fire at.\n"
                "Tip: in /watches, /list and /observing, tap 🗑 next to a row to "
                "remove it — no need to type the id or name.\n"
                "📷 Deals are matched against the item's stock photo to drop wrong items; "
                "/refimage <id> <url> pins that photo, /refimage <id> clear reverts to auto.\n"
                "Under a deal, tap to mute a variant, flag a wrong match, or rate 👍/👎 "
                "(👎 raises the discount bar, 👍 relaxes it).\n"
                "\n"
                "ADMIN: /invite @user · /uninvite @user · /invites"
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
                return "Usage: /add sellername [minprice]  e.g. /add lithosale [100]"
            parts = arg.split()
            seller = normalize_ebay_username(parts[0])
            if not is_valid_ebay_username(seller):
                return "That does not look like a valid eBay username."
            floor_ok, min_price = parse_price_floor(" ".join(parts[1:]))
            if not floor_ok:
                return "Price floor must be a number, e.g. /add lithosale [100]."
            floor = format_price_floor(min_price)
            floor_text = f" Alerting only on items {floor}." if floor else ""
            if self.store.has_seller(seller):
                # Re-adding with a floor just retunes the threshold (and keeps the
                # seen baseline) instead of rejecting the command outright.
                if len(parts) > 1:
                    self.store.set_seller_min_price(seller, min_price)
                    return (
                        f"Updated {seller}.{floor_text}"
                        if floor
                        else f"Updated {seller}: alerting on items at any price."
                    )
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
            added = self.store.add_seller(seller, min_price)
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
                    f"so the next check will verify listings.{baseline_text}{floor_text}"
                )
            return f"Added seller: {seller}.{baseline_text}{floor_text}"
        if command == "/remove":
            if not arg:
                return "Usage: /remove sellername"
            removed = self.store.remove_seller(arg.split()[0])
            return "Removed." if removed else "Seller was not in the watchlist."
        if command == "/list":
            return await self._send_table(chat_id, *self._sellers_view())
        if command == "/floor":
            return self._handle_floor(arg)
        if command == "/status":
            return self.status_text()
        if command == "/health":
            return self.health_text()
        if command == "/check":
            self.store.add_chat(chat_id, username)
            count = await self.check_once()
            return f"Check complete. Alerts sent: {count}"
        if command == "/observe":
            return await self._handle_observe(chat_id, username, arg)
        if command == "/unobserve":
            if not arg:
                return "Usage: /unobserve sellername"
            removed = self.store.remove_observed_seller(arg.split()[0])
            return "Stopped observing." if removed else "Seller was not on the observe list."
        if command == "/observing":
            return await self._send_table(chat_id, *self._observing_view())
        if command == "/interval":
            return self._handle_interval(arg)
        if command == "/watch":
            return await self._handle_watch(chat_id, username, arg)
        if command == "/unwatch":
            return self._handle_unwatch(arg)
        if command == "/demand":
            token = arg.split()[0] if arg.split() else ""
            if not token.isdigit():
                return "Usage: /demand <id>  (see ids with /watches)"
            watch = self.store.get_market_watch(int(token))
            if watch is None:
                return "No market watch with that id."
            _, detail = self._demand_summary(int(token))
            return f"Demand for “{watch['query']}”:\n{detail}"
        if command == "/quote":
            return await self._handle_quote(arg)
        if command == "/refimage":
            return self._handle_refimage(arg)
        if command == "/watches":
            return await self._send_table(chat_id, *self._watches_view())
        return "Unknown command. Try /help"

    # ----- tappable management lists ------------------------------------------
    # /watches, /list and /observing render their text plus a 🗑 button per row,
    # so anything can be removed with a tap instead of retyping its id/name. Each
    # button's callback_data carries the action and the row key; handle_callback
    # removes the row and edits the message in place so the list stays current.

    def _watches_view(self) -> tuple[str, list[dict]]:
        rows = self.store.list_market_watches()
        trends = {
            row["id"]: self._price_trend_text(row["id"], row["market_variant"] or "")
            for row in rows
        }
        text = format_market_rows(
            rows,
            self.config.market_interval_seconds,
            self.config.market_discount_percent,
            trends,
        )
        buttons = [
            self._remove_button(f"🗑 #{row['id']} {row['query']}", f"uw:{row['id']}")
            for row in rows
        ]
        return text, [button for button in buttons if button]

    def _sellers_view(self) -> tuple[str, list[dict]]:
        rows = self.store.list_seller_rows()
        text = format_seller_rows(rows)
        buttons = [
            self._remove_button(f"🗑 {row['username']}", f"rs:{row['username']}")
            for row in rows
        ]
        return text, [button for button in buttons if button]

    def _observing_view(self) -> tuple[str, list[dict]]:
        rows = self.store.list_observed_sellers()
        text = format_observed_rows(rows, self.config.observe_interval_seconds)
        buttons = [
            self._remove_button(f"🗑 {row['username']}", f"ro:{row['username']}")
            for row in rows
        ]
        return text, [button for button in buttons if button]

    @staticmethod
    def _remove_button(label: str, data: str) -> dict | None:
        """An inline button, or None when its callback_data won't fit Telegram's
        64-byte cap (only a pathologically long eBay username could overflow)."""
        if len(data.encode("utf-8")) > 64:
            return None
        return {"text": label[:60], "callback_data": data}

    @staticmethod
    def _table_markup(buttons: list[dict]) -> dict:
        """One button per row, capped. An empty keyboard clears the buttons on
        edit when the last row is removed."""
        return {"inline_keyboard": [[button] for button in buttons[:_MAX_TABLE_BUTTONS]]}

    async def _send_table(self, chat_id: str, text: str, buttons: list[dict]) -> str:
        """Send a managed list with its remove buttons; returns "" so the command
        loop doesn't also send the text as a plain (button-less) reply."""
        markup = self._table_markup(buttons) if buttons else None
        await self.telegram.send_message(
            chat_id, html.escape(text), disable_preview=True, reply_markup=markup
        )
        return ""

    async def _refresh_table(
        self, chat_id: str, message_id: int | None, view: tuple[str, list[dict]]
    ) -> None:
        """Re-render a managed list in place after a removal so the tapped row
        disappears. Best-effort: a failed edit just leaves the toast feedback."""
        if not message_id:
            return
        text, buttons = view
        try:
            await self.telegram.edit_message(
                chat_id,
                message_id,
                html.escape(text),
                reply_markup=self._table_markup(buttons),
                disable_preview=True,
            )
        except Exception:
            log.debug("Could not refresh managed list after removal", exc_info=True)

    async def handle_callback(
        self, chat_id: str, username: str | None, data: str, message_id: int | None = None
    ) -> str:
        """Handle an inline-button tap on a deal alert or a managed list."""
        if not self.is_authorized_chat(chat_id, username):
            return "Not authorized."
        action, _, rest = data.partition(":")
        # One-tap removals from /watches, /list, /observing tables.
        if action in ("uw", "rs", "ro"):
            return await self._callback_remove(chat_id, message_id, action, rest)
        watch_str, _, item_id = rest.partition(":")
        if not watch_str.isdigit() or not item_id:
            return ""
        watch_id = int(watch_str)
        row = self.store.get_market_watch(watch_id)
        if row is None:
            return "That watch no longer exists."
        if action == "bl":
            self.store.block_market_item(watch_id, item_id)
            learned = self._learn_exclude_from_item(watch_id, item_id, row["query"])
            return (
                f"Blocked — and now excluding “{learned}” from this watch."
                if learned
                else "Blocked — I won't alert this listing again."
            )
        if action == "mv":
            alert = self.store.get_deal_alert(watch_id, item_id)
            variant = (alert["variant"] if alert else None) or ""
            if not variant:
                self.store.block_market_item(watch_id, item_id)
                return "Muted this listing."
            self.store.mute_market_variant(watch_id, variant)
            return f"Muted the “{variant}” variant — no more alerts for it."
        if action in ("fu", "fd"):
            verdict = "good" if action == "fu" else "bad"
            self.store.record_feedback(watch_id, item_id, verdict)
            if not self.config.market_feedback_enabled:
                return "Thanks for the feedback."
            delta = (
                self.config.market_feedback_step
                if verdict == "bad"
                else -self.config.market_feedback_relax_step
            )
            self.store.bump_discount_nudge(
                watch_id, delta, -self.config.market_feedback_max_nudge,
                self.config.market_feedback_max_nudge,
            )
            base = (
                row["discount_percent"]
                if row["discount_percent"] is not None
                else self.config.market_discount_percent
            )
            effective = max(1, min(95, base + self.store.get_discount_nudge(watch_id)))
            return f"Thanks — required discount now ~{effective:.0f}% for this watch."
        return ""

    async def _callback_remove(
        self, chat_id: str, message_id: int | None, action: str, key: str
    ) -> str:
        """Remove the row a 🗑 button addresses, then refresh the list in place."""
        if action == "uw":
            if not key.isdigit():
                return ""
            removed = self.store.remove_market_watch(int(key))
            toast = "Stopped watching." if removed else "Already removed."
            view = self._watches_view()
        elif action == "rs":
            removed = self.store.remove_seller(key)
            toast = f"Removed {key}." if removed else "Already removed."
            view = self._sellers_view()
        else:  # "ro"
            removed = self.store.remove_observed_seller(key)
            toast = f"Stopped observing {key}." if removed else "Already removed."
            view = self._observing_view()
        await self._refresh_table(chat_id, message_id, view)
        return toast

    def _learn_exclude_from_item(self, watch_id: int, item_id: str, query: str) -> str | None:
        """Derive a distinctive exclude term from a wrongly-matched listing.

        When the user flags a listing as not the item, the token in its title that
        most likely set it apart (and is not part of the query or a variant
        attribute) becomes a new exclude term, so look-alikes are dropped too.
        """
        alert = self.store.get_deal_alert(watch_id, item_id)
        title = alert["title"] if alert else None
        if not title:
            return None
        q_tokens = set(tokenize(query))
        attrs = attributes(title)
        skip = (
            q_tokens
            | set(attrs["colours"])  # type: ignore[arg-type]
            | set(attrs["qualifiers"])  # type: ignore[arg-type]
            | set(attrs["capacities"])  # type: ignore[arg-type]
        )
        candidates = [t for t in content_tokens(title) if t not in skip and len(t) >= 3]
        if not candidates:
            return None
        term = max(candidates, key=len)
        return term if self.store.append_exclude_term(watch_id, term) else None

    async def _handle_watch(self, chat_id: str, username: str | None, arg: str) -> str:
        if not arg.strip():
            return (
                "Usage: /watch <search terms> [condition:new|used] [under:PRICE] "
                "[discount:%] [every:TIME] [exclude:word] [-word] [category:ID] "
                "[auctions:on] [markets:GB,DE,US]\n"
                "Tip: include a model number (e.g. hu02) for tight matching."
            )
        condition: str | None = None
        max_price: float | None = None
        discount: int | None = None
        interval: int | None = None
        category_id: str | None = None
        include_auctions: bool | None = None
        include_lots: bool | None = None
        markets: str | None = None
        excludes: list[str] = []
        query_words: list[str] = []
        for token in arg.split():
            if token.startswith("-") and len(token) > 1 and not token[1].isdigit():
                excludes.append(token[1:])
                continue
            key, sep, value = token.partition(":")
            key = key.lower()
            if sep and value:
                if key == "condition" and value.lower() in {"new", "used"}:
                    condition = value.lower()
                    continue
                if key in {"category", "cat"}:
                    if not value.isdigit():
                        return "Category must be a numeric eBay category id, e.g. category:178893"
                    category_id = value
                    continue
                if key in {"auctions", "auction"}:
                    include_auctions = value.lower() in {"on", "yes", "true", "1"}
                    continue
                if key in {"lots", "lot", "bundles"}:
                    include_lots = value.lower() in {"on", "yes", "true", "1"}
                    continue
                if key in {"markets", "market"}:
                    codes = [c.strip().upper() for c in value.split(",") if c.strip()]
                    normalized = [c if c.startswith("EBAY_") else f"EBAY_{c}" for c in codes]
                    markets = ",".join(normalized) or None
                    continue
                if key in {"under", "max", "maxprice"}:
                    parsed_price = self._parse_price(value)
                    if parsed_price is None:
                        return "Price must be a number, e.g. under:500"
                    max_price = parsed_price
                    continue
                if key in {"discount", "disc", "off"}:
                    try:
                        discount = max(1, min(95, int(value.rstrip("%"))))
                    except ValueError:
                        return "Discount must be a whole percent, e.g. discount:20"
                    continue
                if key == "every":
                    interval = parse_interval(value)
                    if interval is None:
                        return "Interval must look like 5m, 600s, or 1h."
                    interval = max(self.config.market_min_interval_seconds, interval)
                    continue
                if key in {"exclude", "not", "without"}:
                    excludes.append(value)
                    continue
            query_words.append(token)
        query = normalize_market_query(" ".join(query_words))
        if not query:
            return "Give me something to search for, e.g. /watch dyson airblade hu02"
        if self.store.has_market_watch(query, condition):
            return f"Already watching the market for: {query}"
        self.store.add_chat(chat_id, username)
        exclude_terms = ", ".join(dict.fromkeys(excludes)) or None
        watch_id = self.store.add_market_watch(
            query,
            condition=condition,
            discount_percent=discount,
            max_price=max_price,
            interval_seconds=interval,
            exclude_terms=exclude_terms,
            category_id=category_id,
            include_auctions=include_auctions,
            markets=markets,
            owner_chat_id=chat_id,
            include_lots=include_lots,
        )
        if watch_id is None:
            return f"Already watching the market for: {query}"
        used_discount = discount if discount is not None else self.config.market_discount_percent
        every = format_interval(interval or self.config.market_interval_seconds)
        bits = [f"Alerting on Buy It Now listings ≥{used_discount}% below market"]
        if condition:
            bits.append(f"condition: {condition}")
        if max_price is not None:
            bits.append(f"under {max_price:.0f}")
        if category_id:
            bits.append(f"category {category_id}")
        if self._watch_auctions_enabled(self.store.get_market_watch(watch_id)):
            bits.append("auctions on")
        if exclude_terms:
            bits.append(f"excluding: {exclude_terms}")
        bits.append(f"checked every {every}")
        header = f"Watching the market for “{query}” (#{watch_id}).\n" + ", ".join(bits) + "."
        return header + "\n" + await self._market_sample_summary(watch_id)

    async def _market_sample_summary(self, watch_id: int) -> str:
        """Price the watch immediately so the user sees how good the match is."""
        row = self.store.get_market_watch(watch_id)
        if row is None:
            return ""
        try:
            snap = await self._sample_market(row)
            await self._quote_market(row, snap)
        except Exception:
            log.warning("Could not sample market watch %s on add", watch_id, exc_info=True)
            return "I'll price it on the next check."
        quotes = snap.quotes
        if not quotes:
            return (
                f"Found only {len(snap.fixed)} comparable of {len(snap.items)} listings — "
                "too few to price reliably. Try broader terms, drop the model number, "
                "or relax filters."
            )
        label = max(quotes, key=lambda key: len(snap.clusters.get(key, ())))
        fair = quotes[label]
        headline = f"Market ≈ {fair.price:.2f}"
        if label:
            headline += f" for {label}"
        headline += f" (likely {fair.low:.0f}–{fair.high:.0f}, {fair.confidence}% confidence)"
        if snap.dimensions and len(quotes) > 1:
            others = ", ".join(
                f"{name or 'base'} ≈ {value.price:.0f}" for name, value in sorted(quotes.items())
            )
            headline += (
                f". Pricing variants separately by {'+'.join(snap.dimensions)} — {others}. "
                f"From {len(snap.fixed)} comparable of {len(snap.items)} sampled."
            )
        else:
            headline += (
                f" from {len(snap.fixed)} comparable of {len(snap.items)} listings sampled."
            )
        suggestion = ""
        if not row["category_id"]:
            cat_id, cat_name, share = self._dominant_category(snap.comparable)
            if cat_id and share >= 0.6:
                suggestion = f" Tip: most are in “{cat_name}” — add category:{cat_id} to cut noise."
        return headline + " I'll alert as deals appear. /quote " + f"{watch_id} shows the working." + suggestion

    async def _handle_quote(self, arg: str) -> str:
        """The full pricing breakdown for a watch: per variant, the fair value
        with its interval, the evidence behind it, and the prices that matter
        to someone trading it."""
        token = arg.split()[0] if arg.split() else ""
        if not token.isdigit():
            return "Usage: /quote <id>  (see ids with /watches)"
        row = self.store.get_market_watch(int(token))
        if row is None:
            return "No market watch with that id."
        try:
            snap = await self._sample_market(row)
            await self._quote_market(row, snap)
        except Exception as exc:
            log.warning("Could not quote market watch %s", row["id"], exc_info=True)
            return f"Couldn't price it just now ({str(exc) or exc.__class__.__name__})."
        currency = MARKETPLACE_CURRENCY.get(self._home_marketplace(), "USD")
        discount = self._watch_discount(row)

        def money(value: float) -> str:
            return format_money(value, currency)

        names = {"asks": "asks", "cleared": "ended", "sold": "sold"}
        lines = [
            f"Quote for “{row['query']}” (#{row['id']}) — "
            f"{len(snap.fixed)} comparable of {len(snap.items)} sampled"
            + (f", priced by {'+'.join(snap.dimensions)}" if snap.dimensions else "")
        ]
        for label in sorted(snap.quotes, key=lambda key: -len(snap.clusters.get(key, ()))):
            fair = snap.quotes[label]
            evidence = " · ".join(
                f"{names.get(part.source, part.source)} {money(part.price)} ×{part.n}"
                for part in fair.components
            )
            ceiling = deal_threshold(
                fair.price, discount, fair.predictive_sd, self.config.market_confidence_z
            )
            breakeven = pricing.net_proceeds(
                fair.price, self.config.market_resale_fee_percent,
                self.config.market_resale_fee_fixed, self.config.market_resale_shipping,
            )
            lines.append(
                f"\n• {label or 'all listings'}: fair {money(fair.price)} "
                f"({money(fair.low)}–{money(fair.high)}, {fair.confidence}% confidence)"
            )
            lines.append(f"   evidence: {evidence}")
            lines.append(
                f"   alerts at or below {money(ceiling)} · quick-sale "
                f"{money(pricing.quick_sale_price(fair.price, fair.sigma))} · "
                f"break-even buy {money(breakeven)}"
            )
        thin = [
            f"{label or 'all listings'} ({len(group)})"
            for label, group in snap.clusters.items()
            if label not in snap.quotes
        ]
        if thin:
            lines.append(
                f"\nToo little evidence to price yet (need {self.config.market_min_sample}): "
                + ", ".join(sorted(thin))
            )
        unresolved = len(snap.fixed) - sum(len(group) for group in snap.clusters.values())
        if unresolved > 0:
            lines.append(
                f"{unresolved} listing(s) don't state their {'/'.join(snap.dimensions)} "
                "and are left unpriced rather than guessed."
            )
        if not snap.quotes and not thin:
            lines.append("\nNo comparable listings found.")
        return "\n".join(lines)

    @staticmethod
    def _dominant_category(items: list[MarketItem]) -> tuple[str, str, float]:
        counts: dict[str, tuple[str, int]] = {}
        for item in items:
            category_id = getattr(item, "category_id", "")
            if not category_id:
                continue
            name, count = counts.get(category_id, (getattr(item, "category_name", ""), 0))
            counts[category_id] = (name, count + 1)
        if not counts:
            return "", "", 0.0
        cat_id, (name, count) = max(counts.items(), key=lambda kv: kv[1][1])
        return cat_id, name, count / max(1, len(items))

    def _handle_unwatch(self, arg: str) -> str:
        token = arg.split()[0] if arg.split() else ""
        if not token.isdigit():
            return "Usage: /unwatch <id>  (see ids with /watches)"
        removed = self.store.remove_market_watch(int(token))
        return "Stopped watching." if removed else "No market watch with that id."

    def _handle_refimage(self, arg: str) -> str:
        """Pin / show / clear a watch's reference 'stock' image — the photo of the
        actual item that listing photos are compared against."""
        parts = arg.split()
        if not parts or not parts[0].isdigit():
            return (
                "Usage:\n"
                "/refimage <id> <image-url> — pin the item's stock image\n"
                "/refimage <id> clear — back to auto\n"
                "/refimage <id> — show the current reference\n"
                "(ids from /watches)"
            )
        watch_id = int(parts[0])
        watch = self.store.get_market_watch(watch_id)
        if watch is None:
            return "No market watch with that id."
        query = watch["query"]
        if len(parts) == 1:
            current = watch["reference_image_url"]
            if current:
                return f"Reference image for “{query}”:\n{current}"
            return (
                f"No reference image pinned for “{query}” — auto-deriving one from "
                f"the market. Pin the exact stock image with /refimage {watch_id} <url>."
            )
        value = parts[1]
        if value.lower() in {"clear", "auto", "off", "none"} or value.lower().startswith(
            ("http://", "https://")
        ):
            self.store.clear_suppressed_deals(watch_id)
        if value.lower() in {"clear", "auto", "off", "none"}:
            self.store.set_market_reference_image(watch_id, None)
            return f"Cleared the reference image for “{query}” — back to auto."
        if not value.lower().startswith(("http://", "https://")):
            return "That doesn't look like an image URL (it must start with http:// or https://)."
        self.store.set_market_reference_image(watch_id, value)
        note = "" if self.config.market_vision else (
            "\n(Note: MARKET_VISION is off, so this isn't used yet — turn it on to compare photos.)"
        )
        return (
            f"📷 Pinned the reference image for “{query}”. "
            f"Listing photos will be compared against it.{note}"
        )

    @staticmethod
    def _parse_price(value: str) -> float | None:
        cleaned = value.strip().lstrip("£$€").replace(",", "")
        try:
            price = float(cleaned)
        except ValueError:
            return None
        return price if price > 0 else None

    async def _handle_observe(self, chat_id: str, username: str | None, arg: str) -> str:
        parts = arg.split()
        if not parts:
            return "Usage: /observe sellername [interval e.g. 3m] [minprice e.g. [100]]"
        seller = normalize_ebay_username(parts[0])
        if not is_valid_ebay_username(seller):
            return "That does not look like a valid eBay username."
        # The two optional args (interval, price floor) may arrive in either order;
        # a bracketed/currency-prefixed token is the floor, anything else an interval.
        interval: int | None = None
        min_price: float | None = None
        had_floor = False
        for token in parts[1:]:
            if looks_like_floor(token):
                floor_ok, min_price = parse_price_floor(token)
                if not floor_ok:
                    return "Price floor must be a number, e.g. /observe lithosale [100]."
                had_floor = True
            else:
                interval = parse_interval(token)
                if interval is None:
                    return "Interval must look like 90s, 3m, or 1h (price floor as [100])."
                interval = max(self.config.observe_min_interval_seconds, interval)
        floor = format_price_floor(min_price)
        floor_text = f" Alerting only on items {floor}." if floor else ""
        if self.store.has_observed_seller(seller):
            # Re-issuing with a floor retunes it without losing the seen baseline.
            if had_floor:
                self.store.set_observed_min_price(seller, min_price)
                return (
                    f"Updated {seller}.{floor_text}"
                    if floor
                    else f"Updated {seller}: alerting on items at any price."
                )
            return f"Already observing seller: {seller}"
        try:
            exists = await self.ebay.seller_exists(seller)
        except Exception:
            log.warning("Could not validate eBay seller %s for observe", seller)
            return "I could not verify that seller with eBay right now. Try again in a minute."
        self.store.add_chat(chat_id, username)
        if not self.store.add_observed_seller(seller, interval, min_price):
            return f"Already observing seller: {seller}"
        baseline_count = await self._seed_observe_baseline(seller)
        baseline_text = (
            f" Seeded {baseline_count} current listings as already seen."
            if baseline_count is not None
            else " I added it, but could not seed the current listings yet."
        )
        every = format_interval(interval or self.config.observe_interval_seconds)
        confirm = (
            ""
            if exists is not None
            else " eBay would not confirm the profile, but I will keep checking."
        )
        return f"Observing seller: {seller} every {every}.{baseline_text}{floor_text}{confirm}"

    def _handle_floor(self, arg: str) -> str:
        """Set or clear the price floor on a seller already on the watch/observe
        list, without dropping its seen baseline. ``none``/``any``/``0`` clears it."""
        parts = arg.split()
        if len(parts) < 2:
            return "Usage: /floor sellername [100]   (use /floor sellername none to clear)"
        seller = normalize_ebay_username(parts[0])
        floor_ok, min_price = parse_price_floor(" ".join(parts[1:]))
        if not floor_ok:
            return "Price floor must be a number, e.g. /floor lithosale [100]."
        on_watch = self.store.set_seller_min_price(seller, min_price)
        on_observe = self.store.set_observed_min_price(seller, min_price)
        if not on_watch and not on_observe:
            return f"{seller} is not on the watch or observe list. Add it first."
        lists = " and ".join(
            label for label, hit in (("watch", on_watch), ("observe", on_observe)) if hit
        )
        floor = format_price_floor(min_price)
        if floor:
            return f"{seller} ({lists}): alerting only on items {floor}."
        return f"{seller} ({lists}): alerting on items at any price."

    def _handle_interval(self, arg: str) -> str:
        parts = arg.split()
        if len(parts) < 2:
            return "Usage: /interval sellername 3m"
        seller = normalize_ebay_username(parts[0])
        interval = parse_interval(parts[1])
        if interval is None:
            return "Interval must look like 90s, 3m, or 1h."
        interval = max(self.config.observe_min_interval_seconds, interval)
        if not self.store.set_observed_interval(seller, interval):
            return f"{seller} is not on the observe list. Add it with /observe first."
        return f"Observing {seller} every {format_interval(interval)}."
