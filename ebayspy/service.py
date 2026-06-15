from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import NamedTuple

from . import demand as demand_metrics
from . import insights as insights_metrics
from . import risk as risk_metrics
from . import semantic
from . import vision
from .config import Config
from .ebay import MARKETPLACE_CURRENCY, EbayClient
from .fx import FxConverter
from .market import (
    choose_cluster_dimensions,
    cluster_by_variant,
    discount_percent_for,
    estimate_resale_profit,
    find_arbitrage,
    find_deals,
    market_price,
    offer_candidates,
    price_distribution,
    variant_label,
)
from .matching import (
    attributes,
    canonicalize,
    content_tokens,
    filter_comparable,
    lot_quantity,
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
    format_status_rows,
    is_valid_ebay_username,
    is_valid_telegram_username,
    normalize_ebay_username,
    normalize_market_query,
    normalize_telegram_username,
    parse_interval,
)
from .telegram import TelegramBot

log = logging.getLogger(__name__)


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
        self.telegram = TelegramBot(
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
                    if not sent:
                        # Every chat send failed (e.g. Telegram outage); leave the
                        # item unseen so the next poll retries instead of silently
                        # dropping a genuinely new listing.
                        continue
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

                # Price drops are read BEFORE upsert_active_listings overwrites the
                # stored price; the floor is recorded only on a successful send so a
                # failed alert retries next poll instead of being lost.
                for listing, old_price, new_price, pct in self.store.price_drop_candidates(
                    listings, self.config.seller_price_drop_percent
                ):
                    sent = await self._notify_price_drop_chats(
                        chats, listing, old_price, new_price, pct
                    )
                    if sent:
                        self.store.mark_price_drop_alerted(listing.item_id, new_price)
                        total_alert_count += 1

                if not listings_truncated:
                    for listing, last_seen_at in self.store.ended_candidates(
                        seller, active_item_ids
                    ):
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
            self.store.record_observe_check(seller, new_count=0, error=str(exc))
            return 0

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
            for listing in await self.ebay.hydrate_listings(new_listings):
                sent = await self._notify_chats(chats, listing)
                self.store.mark_seen(listing, notified=sent)
                if sent:
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
                            await self._check_market_watch(row, watch_chats)
                            await self._maybe_check_arbitrage(row, watch_chats, loop.time())
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
        chats = [owner]
        for admin in self.config.telegram_allowed_chat_ids:
            if admin not in chats:
                chats.append(admin)
        return chats

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
        quotes: list[tuple[str, float, str]] = []
        for marketplace in marketplaces:
            try:
                items = await self.ebay.search_market(
                    search_query,
                    condition=row["condition"],
                    min_price=row["min_price"],
                    max_price=row["max_price"],
                    limit=self.config.market_sample_size,
                    category_ids=row["category_id"] or None,
                    marketplace=marketplace,
                )
            except Exception:
                log.warning("Arbitrage search failed for %s on %s", row["query"], marketplace)
                continue
            fixed = [
                item
                for item in filter_comparable(
                    row["query"],
                    items,
                    extra_excludes=self._watch_exclude_terms(row),
                    coverage=self.config.market_match_coverage,
                    fuzzy_threshold=self.config.market_fuzzy_threshold,
                    semantic_threshold=self._semantic_threshold(),
                )
                if not item.is_auction
            ]
            price = market_price(item.total_price for item in fixed)
            if price is not None and len(fixed) >= self.config.market_min_sample:
                quotes.append((marketplace, price, MARKETPLACE_CURRENCY.get(marketplace, "USD")))
        if len(quotes) < 2:
            return
        result = find_arbitrage(
            quotes,
            self.fx.convert,
            self.config.market_arbitrage_threshold,
            MARKETPLACE_CURRENCY.get(home, "USD"),
        )
        if result:
            for chat_id in chats:
                try:
                    await self.telegram.notify_arbitrage(chat_id, row["query"], result)
                except Exception:
                    log.exception("Failed sending arbitrage alert to %s", chat_id)

    def _watch_exclude_terms(self, row) -> list[str]:
        raw = row["exclude_terms"] or ""
        return [term.strip() for term in raw.split(",") if term.strip()]

    async def _sample_market(self, row):
        """Search, narrow to comparable listings, and split them into priceable
        per-variant clusters.

        Returns (sampled, comparable, clusters, dimension). ``clusters`` maps a
        variant label to the listings of that variant; pricing and deal detection
        run per cluster so different colours/capacities never blend into one
        meaningless median. Persists the headline (largest variant) figure for
        /watches.
        """
        query = row["query"]
        category_ids = row["category_id"] or None
        include_auctions = self._watch_auctions_enabled(row)
        search_query = canonicalize(query)
        items = await self.ebay.search_market(
            search_query,
            condition=row["condition"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            limit=self.config.market_sample_size,
            category_ids=category_ids,
            include_auctions=include_auctions,
        )
        if self.config.market_deal_scan:
            # Optional supplementary cheapest-first pass to catch deep-page deals.
            extra = await self.ebay.search_market(
                search_query,
                condition=row["condition"],
                min_price=row["min_price"],
                max_price=row["max_price"],
                limit=self.config.market_sample_size,
                sort="price",
                category_ids=category_ids,
                include_auctions=include_auctions,
            )
            seen = {item.item_id for item in items}
            items.extend(item for item in extra if item.item_id not in seen)
        blocked = self.store.blocked_item_ids(row["id"])
        comparable = [
            item
            for item in filter_comparable(
                query,
                items,
                extra_excludes=self._watch_exclude_terms(row),
                coverage=self.config.market_match_coverage,
                fuzzy_threshold=self.config.market_fuzzy_threshold,
                semantic_threshold=self._semantic_threshold(),
            )
            if item.item_id not in blocked
        ]
        # Enrich the comparable set with eBay's structured catalog data (ePID,
        # GTIN, MPN, aspects) so variant clustering uses authoritative values
        # instead of guessing from titles — bounded so the extra calls stay cheap.
        if self.config.market_hydrate:
            comparable = await self.ebay.hydrate_market_items(
                comparable, self.config.market_hydrate_limit
            )
        # The market price is set by fixed-price listings only; an auction's live
        # bid is artificially low mid-auction and would drag the median down.
        fixed = [item for item in comparable if not item.is_auction]
        auctions = [item for item in comparable if item.is_auction]
        dimensions = choose_cluster_dimensions(
            fixed,
            specified_dimensions(query),
            min_sample=self.config.market_min_sample,
            min_dispersion=self.config.market_min_dispersion,
        )
        clusters = cluster_by_variant(fixed, dimensions)
        self._store_headline_price(row["id"], items, fixed, clusters)
        return items, comparable, clusters, dimensions, auctions

    def _watch_auctions_enabled(self, row) -> bool:
        flag = row["include_auctions"]
        return self.config.market_auctions_default if flag is None else bool(flag)

    def _semantic_threshold(self) -> float | None:
        return self.config.market_semantic_threshold if self.config.market_semantic else None

    async def _vision_check(self, item: MarketItem, query: str) -> tuple[bool, str]:
        """Optionally verify the listing photo. Returns (drop, note): drop=True when
        the image clearly isn't the product; note is a one-line image read (incl. a
        gem flag when the photo looks better than the listing's stated condition).
        Runs the CLIP work off the event loop; no-ops when vision is unavailable."""
        if not self.config.market_vision or not item.image_url or not vision.available():
            return False, ""
        match = await asyncio.to_thread(vision.match_score, item.image_url, query)
        if match is not None and match < self.config.market_vision_match_threshold:
            return True, ""  # photo doesn't depict the product — likely mistitled/wrong
        flags = await asyncio.to_thread(vision.vision_flags, item.image_url, item.condition)
        if flags is None:
            return False, ""
        note = vision.compose_note(
            flags,
            item.condition,
            stock_threshold=self.config.market_vision_stock_threshold,
            damage_threshold=self.config.market_vision_damage_threshold,
            count_hint=self.config.market_vision_count_hint,
            count_threshold=self.config.market_vision_count_threshold,
        )
        return False, note

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

    async def _fetch_sold_prices(self, row, dimensions, labels: set[str]):
        """Real sold-price medians, distribution, and recent comps per variant,
        plus a sales-velocity tag, from the Marketplace Insights API. Returns
        ({}, None, {}, {}) on any failure so the caller keeps the live estimate."""
        try:
            sold = await self.ebay.search_item_sales(
                canonicalize(row["query"]),
                condition=row["condition"],
                category_ids=row["category_id"] or None,
                limit=self.config.market_sample_size,
            )
        except Exception:
            log.warning("Insights sold-data lookup failed for %s; using listings", row["query"])
            return {}, None, {}, {}
        comparable = filter_comparable(
            row["query"],
            sold,
            key=lambda item: item.title,
            extra_excludes=self._watch_exclude_terms(row),
            coverage=self.config.market_match_coverage,
            fuzzy_threshold=self.config.market_fuzzy_threshold,
            semantic_threshold=self._semantic_threshold(),
        )
        clusters = cluster_by_variant(comparable, dimensions)
        price_map: dict[str, float] = {}
        dist_map: dict[str, tuple[float, float, float]] = {}
        comps_map: dict[str, list[float]] = {}
        for label, group in clusters.items():
            if label not in labels or len(group) < self.config.market_min_sample:
                continue
            price = market_price(item.total_price for item in group)
            if price is None:
                continue
            price_map[label] = price
            dist = price_distribution(item.total_price for item in group)
            if dist:
                dist_map[label] = dist
            # Most-recent sold prices as comps (newest first by sold date).
            recent = sorted(group, key=lambda s: s.sold_date or "", reverse=True)
            comps_map[label] = [s.total_price for s in recent[:3]]
        tag, _ = insights_metrics.summarize_sold(comparable)
        return price_map, tag, dist_map, comps_map

    def _demand_summary(self, watch_id: int) -> tuple[str, str]:
        stats = self.store.market_demand_stats(watch_id, self.config.market_demand_window_days)
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

    def _auction_candidates(
        self, row, auctions: list[MarketItem], dimensions, priced, muted, discount
    ) -> list[_Deal]:
        """Auction snipes below their own variant's fixed-price market.

        Sends a heads-up the first time a bid is below market, and a separate
        final-call when it enters the snipe window; flags the watch for turbo
        polling so the final-call lands in time. Auctions with no/low competition
        qualify at a gentler discount, since winning uncontested is itself worth a lot.
        """
        watch_id = row["id"]
        ratio = self.config.market_min_deal_ratio
        snipe = self.config.market_snipe_window_seconds
        out: list[_Deal] = []
        for auction in auctions:
            label = variant_label(auction, dimensions) if dimensions else ""
            price = priced.get(label)
            if price is None or label in muted:
                continue
            low_comp = (
                auction.bid_count is not None
                and auction.bid_count <= self.config.market_low_bid_count
            )
            effective_discount = (
                min(discount, self.config.market_nobid_discount_percent) if low_comp else discount
            )
            if not (price * ratio <= auction.total_price <= price * (1 - effective_discount / 100)):
                continue
            secs = self._seconds_until(auction.end_date)
            if secs is not None and secs <= 0:
                continue  # already ended
            ending_soon = secs is not None and secs <= snipe
            stage = "final" if ending_soon else "deal"
            if self.store.deal_already_alerted(watch_id, auction.item_id, stage):
                continue
            out.append(
                _Deal(auction, price, label or None, stage, ending_soon, low_comp)
            )
            if secs is not None and secs <= snipe * 2:
                self._auction_turbo[watch_id] = self.config.market_turbo_interval_seconds
        return out

    def _watch_lots_enabled(self, row) -> bool:
        return bool(row["include_lots"])

    def _lot_candidates(self, row, items, dimensions, priced, muted, discount) -> list[_Deal]:
        """Job-lot/bundle deals: value a lot of N by N × the single-unit market.

        Lots are normally excluded from matching; here we deliberately keep them,
        parse the quantity, and flag lots priced below their per-unit worth.
        """
        if not self._watch_lots_enabled(row):
            return []
        watch_id = row["id"]
        ratio = self.config.market_min_deal_ratio
        blocked = self.store.blocked_item_ids(watch_id)
        lot_comparable = [
            item
            for item in filter_comparable(
                row["query"],
                items,
                extra_excludes=self._watch_exclude_terms(row),
                coverage=self.config.market_match_coverage,
                fuzzy_threshold=self.config.market_fuzzy_threshold,
                semantic_threshold=self._semantic_threshold(),
                allow_lots=True,
            )
            if item.item_id not in blocked and not item.is_auction
        ]
        out: list[_Deal] = []
        for lot in lot_comparable:
            quantity = lot_quantity(lot.title)
            if not quantity:
                continue
            label = variant_label(lot, dimensions) if dimensions else ""
            unit_market = priced.get(label)
            if unit_market is None or label in muted:
                continue
            lot_value = unit_market * quantity
            if not (lot_value * ratio <= lot.total_price <= lot_value * (1 - discount / 100)):
                continue
            if self.store.deal_already_alerted(watch_id, lot.item_id, "lot"):
                continue
            out.append(_Deal(lot, lot_value, label or None, "lot", lot_quantity=quantity))
        return out

    def _store_headline_price(self, watch_id, items, comparable, clusters) -> float | None:
        """Persist the largest well-populated variant as the headline price."""
        priceable = {
            label: group
            for label, group in clusters.items()
            if len(group) >= self.config.market_min_sample
        }
        chosen = priceable or clusters
        price, variant = None, None
        if chosen:
            label, group = max(chosen.items(), key=lambda kv: len(kv[1]))
            price = market_price(item.total_price for item in group)
            variant = label or None
        self.store.update_market_price(watch_id, price, len(items), len(comparable), variant)
        return price

    async def _check_market_watch(self, row, chats: list[str]) -> int:
        watch_id = row["id"]
        query = row["query"]
        discount = (
            row["discount_percent"]
            if row["discount_percent"] is not None
            else self.config.market_discount_percent
        )
        if self.config.market_feedback_enabled:
            # 👍/👎 feedback nudges the required discount; flows through every deal
            # type since this single value is passed into find_deals/auctions/lots.
            discount = max(1, min(95, discount + self.store.get_discount_nudge(watch_id)))
        try:
            items, comparable, clusters, dimensions, auctions = await self._sample_market(row)
        except Exception as exc:
            log.exception("Failed pricing market watch %s (%s)", watch_id, query)
            await self._finish_market_check(row, chats, deal_count=0, error=str(exc))
            return 0

        # Price each variant against its own kind. A cluster must clear the
        # minimum-sample gate before its median is trusted; otherwise a too-narrow
        # or too-dispersed query would mis-price the market and fire false deals.
        priced = {
            label: market_price(item.total_price for item in group)
            for label, group in clusters.items()
            if len(group) >= self.config.market_min_sample
        }
        # Price distribution (P10/P50/P90) per variant — from live asking prices
        # by default, overridden by real sold prices under the insights source.
        dist_by_label = {
            label: price_distribution(item.total_price for item in clusters.get(label, []))
            for label in priced
        }
        comps_by_label: dict[str, list[float]] = {}

        # When real sold data is available, price each (live-buyable) variant
        # against its *sold* median instead of the live-asking median.
        sold_demand_tag = None
        if self.config.market_price_source == "insights" and priced:
            sold_map, sold_demand_tag, sold_dist, sold_comps = await self._fetch_sold_prices(
                row, dimensions, set(priced)
            )
            for label, sold in sold_map.items():
                priced[label] = sold
            dist_by_label.update(sold_dist)
            comps_by_label = sold_comps
            if sold_map:
                # Keep the /watches headline consistent with the sold pricing.
                dominant = max(priced, key=lambda label: len(clusters.get(label, [])))
                self.store.update_market_price(
                    watch_id, priced[dominant], len(items), len(comparable), dominant or None
                )
        if not priced:
            await self._finish_market_check(row, chats, deal_count=0, empty=True)
            log.info(
                "Market watch %s (%s): %s/%s comparable across %s variant(s), "
                "none with the %s needed to price — holding",
                watch_id,
                query,
                len(comparable),
                len(items),
                len(clusters),
                self.config.market_min_sample,
            )
            return 0

        for label, price in priced.items():
            if price is not None:
                self.store.record_price_sample(watch_id, label, price)

        # Collect lifecycle data for the demand read: record sightings of the
        # fixed-price comparables and mark ones that have vanished as ended.
        fixed = [item for item in comparable if not item.is_auction]
        self.store.record_market_sightings(
            watch_id,
            [
                (
                    item.item_id,
                    variant_label(item, dimensions) if dimensions else "",
                    item.total_price,
                    item.currency,
                    item.listed_at,
                )
                for item in fixed
            ],
        )
        self.store.mark_disappeared_listings(
            watch_id,
            {item.item_id for item in fixed},
            self.config.market_demand_grace_seconds,
        )
        demand_tag, _ = self._demand_summary(watch_id)
        if sold_demand_tag:
            demand_tag = sold_demand_tag

        muted = self.store.muted_variants(watch_id)
        ratio = self.config.market_min_deal_ratio
        candidates: list[_Deal] = []

        # Fixed-price deals across every priceable variant.
        for label, group in clusters.items():
            price = priced.get(label)
            if price is None or label in muted:
                continue
            for item in find_deals(group, price, discount, ratio):
                if not self.store.deal_already_alerted(watch_id, item.item_id, "deal"):
                    candidates.append(_Deal(item, price, label or None, "deal"))

        # Auction snipes, priced against their own variant's fixed-price market.
        self._auction_turbo.pop(watch_id, None)
        candidates.extend(self._auction_candidates(row, auctions, dimensions, priced, muted, discount))
        # Lot/bundle arbitrage (opt-in per watch).
        candidates.extend(self._lot_candidates(row, items, dimensions, priced, muted, discount))

        # Best-Offer candidates: list price isn't a deal but a plausible accepted
        # offer would be. Skip items already a list-price deal this cycle.
        if self.config.market_offer_aware:
            deal_ids = {c.item.item_id for c in candidates}
            for label, group in clusters.items():
                price = priced.get(label)
                if price is None or label in muted:
                    continue
                for item, estimate in offer_candidates(
                    group,
                    price,
                    discount,
                    self.config.market_expected_offer_discount,
                    ratio,
                    sold_comps=comps_by_label.get(label or ""),
                ):
                    if item.item_id in deal_ids:
                        continue
                    if not self.store.deal_already_alerted(watch_id, item.item_id, "offer"):
                        candidates.append(
                            _Deal(item, price, label or None, "offer", offer_estimate=estimate)
                        )

        candidates.sort(key=lambda c: c.item.total_price / c.price)
        first_check = not self.store.market_watch_has_successful_check(watch_id)
        sent_count = 0
        # Drain at most a handful of deals per tick so a backlog (common on the
        # very first check) never floods the chat in one burst.
        home_country = self._home_country()
        for deal in candidates[: self.config.market_max_deals_per_cycle]:
            item, price, variant = deal.item, deal.price, deal.variant
            stage, ending_soon, low_comp = deal.stage, deal.ending_soon, deal.low_competition
            lot_qty = deal.lot_quantity
            offer_estimate = deal.offer_estimate
            risk_score, risk_reasons = risk_metrics.assess(item, price, home_country)
            if risk_score > self.config.market_risk_max:
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
            drop, vision_note = await self._vision_check(item, query)
            if drop:
                log.info(
                    "Vision dropped deal %s on watch %s: image doesn't match the product",
                    item.item_id, watch_id,
                )
                continue
            # Offer candidates are judged on the estimated accepted-offer price,
            # not the list price.
            effective_price = offer_estimate if offer_estimate is not None else item.total_price
            actual_discount = discount_percent_for(effective_price, price)
            profit, roi = estimate_resale_profit(
                price,
                effective_price,
                self.config.market_resale_fee_percent,
                self.config.market_resale_fee_fixed * (lot_qty or 1),
            )
            sent = await self._notify_deal_chats(
                chats,
                item,
                market_price=price,
                discount_percent=actual_discount,
                query=query,
                variant=variant if dimensions else None,
                profit=profit,
                roi=roi,
                watch_id=watch_id,
                ending_soon=ending_soon,
                trend=self._price_trend_text(watch_id, variant),
                demand=demand_tag,
                distribution=dist_by_label.get(variant or ""),
                comps=comps_by_label.get(variant or ""),
                low_competition=low_comp,
                risk=risk_text,
                lot_quantity=lot_qty,
                vision=vision_note,
                offer_estimate=offer_estimate,
            )
            if sent:
                # Only mark the deal as alerted once it has actually been
                # delivered to at least one chat; otherwise a transient Telegram
                # failure would permanently suppress a real below-market deal.
                self.store.record_deal_alert(
                    watch_id, item.item_id, item.total_price, variant=variant,
                    title=item.title, stage=stage,
                )
                sent_count += 1
        await self._finish_market_check(row, chats, deal_count=sent_count)
        log.info(
            "Market watch %s (%s): %s/%s comparable across %s priced variant(s)%s, "
            "%s deals alerted%s",
            watch_id,
            query,
            len(comparable),
            len(items),
            len(priced),
            f" by {'+'.join(dimensions)}" if dimensions else "",
            sent_count,
            " (first check)" if first_check else "",
        )
        return sent_count

    async def _notify_deal_chats(
        self,
        chats: list[str],
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
        offer_estimate: float | None = None,
    ) -> bool:
        sent_any = False
        for chat_id in chats:
            try:
                await self.telegram.notify_deal(
                    chat_id,
                    item,
                    market_price=market_price,
                    discount_percent=discount_percent,
                    query=query,
                    variant=variant,
                    profit=profit,
                    roi=roi,
                    watch_id=watch_id,
                    ending_soon=ending_soon,
                    trend=trend,
                    demand=demand,
                    distribution=distribution,
                    comps=comps,
                    low_competition=low_competition,
                    risk=risk,
                    lot_quantity=lot_quantity,
                    vision=vision,
                    offer_estimate=offer_estimate,
                )
                sent_any = True
            except Exception:
                log.exception("Failed sending Telegram deal alert to chat %s", chat_id)
        return sent_any

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
                "WATCH a seller — alerts every poll: new + price drop + ended/sold + restock\n"
                "  /add <seller>          e.g. /add techbargains_uk\n"
                "  /remove <seller> · /list · /status · /check (poll now)\n"
                "  /health                liveness: uptime, last poll, counts, errors\n"
                "\n"
                "OBSERVE a seller — fast new-listing alerts only, on its own interval\n"
                "  /observe <seller> [interval]   e.g. /observe techbargains_uk 90s\n"
                "  /unobserve <seller> · /observing · /interval <seller> <time>\n"
                "\n"
                "MARKET DEALS — alert when something lists below the going rate\n"
                "  /watch <terms> [options]\n"
                "  options: condition:new|used  under:PRICE  discount:%  every:TIME\n"
                "           exclude:word  category:ID  auctions:on  lots:on  markets:GB,DE,US\n"
                "  examples:\n"
                "    /watch dyson airblade hu02\n"
                "    /watch iphone 13 pro condition:used under:450 discount:20\n"
                "    /watch ps5 console auctions:on discount:25      (also snipes auctions)\n"
                "    /watch lego 75192 markets:GB,DE,US              (cross-border arbitrage)\n"
                "    /watch airpods pro lots:on exclude:case         (job lots; drop \"case\")\n"
                "  /watches (list) · /unwatch <id> · /demand <id> (liquidity read)\n"
                "  Under each deal, tap to mute a variant, flag a wrong match, or rate it\n"
                "  👍/👎 — 👎 raises that watch's discount bar, 👍 relaxes it.\n"
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
            return format_observed_rows(
                self.store.list_observed_sellers(), self.config.observe_interval_seconds
            )
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
        if command == "/watches":
            rows = self.store.list_market_watches()
            trends = {
                row["id"]: self._price_trend_text(row["id"], row["market_variant"] or "")
                for row in rows
            }
            return format_market_rows(
                rows,
                self.config.market_interval_seconds,
                self.config.market_discount_percent,
                trends,
            )
        return "Unknown command. Try /help"

    async def handle_callback(self, chat_id: str, username: str | None, data: str) -> str:
        """Handle an inline-button tap on a deal alert."""
        if not self.is_authorized_chat(chat_id, username):
            return "Not authorized."
        action, _, rest = data.partition(":")
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
            items, comparable, clusters, dimensions, _auctions = await self._sample_market(row)
        except Exception:
            log.warning("Could not sample market watch %s on add", watch_id, exc_info=True)
            return "I'll price it on the next check."
        priced = {
            label: market_price(item.total_price for item in group)
            for label, group in clusters.items()
            if len(group) >= self.config.market_min_sample
        }
        if not priced:
            return (
                f"Found only {len(comparable)} comparable of {len(items)} listings — "
                "too few to price reliably. Try broader terms, drop the model number, "
                "or relax filters."
            )
        row = self.store.get_market_watch(watch_id)
        headline = f"Market ≈ {row['market_price']:.2f}"
        if row["market_variant"]:
            headline += f" for {row['market_variant']}"
        if dimensions and len(priced) > 1:
            others = ", ".join(
                f"{label or 'base'} ≈ {value:.0f}"
                for label, value in sorted(priced.items())
                if value
            )
            headline += (
                f". Pricing variants separately by {'+'.join(dimensions)} — {others}. "
                f"From {len(comparable)} comparable of {len(items)} sampled."
            )
        else:
            headline += (
                f" from {len(comparable)} comparable of {len(items)} listings sampled."
            )
        suggestion = ""
        if not row["category_id"]:
            cat_id, cat_name, share = self._dominant_category(comparable)
            if cat_id and share >= 0.6:
                suggestion = f" Tip: most are in “{cat_name}” — add category:{cat_id} to cut noise."
        return headline + " I'll alert as deals appear." + suggestion

    @staticmethod
    def _dominant_category(items: list[MarketItem]) -> tuple[str, str, float]:
        counts: dict[str, tuple[str, int]] = {}
        for item in items:
            if not item.category_id:
                continue
            name, count = counts.get(item.category_id, (item.category_name, 0))
            counts[item.category_id] = (name, count + 1)
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
            return "Usage: /observe sellername [interval e.g. 3m]"
        seller = normalize_ebay_username(parts[0])
        if not is_valid_ebay_username(seller):
            return "That does not look like a valid eBay username."
        interval: int | None = None
        if len(parts) > 1:
            interval = parse_interval(parts[1])
            if interval is None:
                return "Interval must look like 90s, 3m, or 1h."
            interval = max(self.config.observe_min_interval_seconds, interval)
        if self.store.has_observed_seller(seller):
            return f"Already observing seller: {seller}"
        try:
            exists = await self.ebay.seller_exists(seller)
        except Exception:
            log.warning("Could not validate eBay seller %s for observe", seller)
            return "I could not verify that seller with eBay right now. Try again in a minute."
        self.store.add_chat(chat_id, username)
        if not self.store.add_observed_seller(seller, interval):
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
        return f"Observing seller: {seller} every {every}.{baseline_text}{confirm}"

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
