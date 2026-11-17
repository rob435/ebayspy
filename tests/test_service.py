import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ebayspy.models import Listing, MarketItem
from ebayspy.service import EbaySpyService
from ebayspy.storage import Store


class FakeEbay:
    def __init__(
        self,
        listings: list[Listing],
        item_active_response: bool | None = False,
        seller_exists_response: bool | None = True,
    ) -> None:
        self.listings = listings
        self.item_active_response = item_active_response
        self.seller_exists_response = seller_exists_response
        self.hydrated: list[str] = []
        self.market_items: list = []

    async def seller_listings(self, seller: str, *, hydrate: bool = True) -> list[Listing]:
        return self.listings

    async def hydrate_listings(self, listings: list[Listing]) -> list[Listing]:
        self.hydrated.extend(listing.item_id for listing in listings)
        return listings

    async def seller_exists(self, seller: str) -> bool | None:
        return self.seller_exists_response

    async def item_active(self, item_id: str) -> bool | None:
        return self.item_active_response

    async def item_ended_state(self, item_id: str) -> tuple[bool | None, str | None]:
        return self.item_active_response, None

    async def search_market(self, query: str, **kwargs) -> list:
        return list(self.market_items)

    async def hydrate_market_items(self, items: list, limit: int) -> list:
        return items

    async def search_item_sales(self, query: str, **kwargs) -> list:
        return list(getattr(self, "sold_items", []))


class FakeTelegram:
    def __init__(self) -> None:
        self.quantity_increases: list[tuple[str, str, int, int]] = []
        self.listings: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str]] = []
        self.deals: list[tuple[str, str, float]] = []
        self.deal_details: list[dict] = []
        self.price_drops: list[tuple[str, str, float, float]] = []
        self.messages: list[tuple[str, str, dict | None]] = []
        self.edits: list[tuple[str, int, str, dict | None]] = []

    async def send_message(
        self, chat_id: str, text: str, disable_preview: bool = False, reply_markup=None
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))

    async def edit_message(
        self, chat_id: str, message_id: int, text: str, reply_markup=None,
        disable_preview: bool = True,
    ) -> None:
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def notify_listing(self, chat_id: str, listing: Listing) -> None:
        self.listings.append((chat_id, listing.item_id))

    async def notify_ended_listing(
        self, chat_id: str, listing: Listing, ended_at: str | None = None
    ) -> None:
        self.ended.append((chat_id, listing.item_id))

    async def notify_quantity_increase(
        self, chat_id: str, listing: Listing, previous_quantity: int, current_quantity: int
    ) -> None:
        self.quantity_increases.append(
            (chat_id, listing.item_id, previous_quantity, current_quantity)
        )

    async def notify_price_drop(
        self, chat_id: str, listing: Listing, old_price: float, new_price: float, pct: float
    ) -> None:
        self.price_drops.append((chat_id, listing.item_id, old_price, new_price))

    async def notify_deal(
        self,
        chat_id: str,
        item,
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
        distribution=None,
        comps=None,
        low_competition: bool = False,
        risk: str = "",
        lot_quantity: int | None = None,
        vision: str = "",
        offer_estimate: float | None = None,
        quote=None,
        quick_profit: float | None = None,
    ) -> None:
        self.deals.append((chat_id, item.item_id, item.total_price))
        self.deal_details.append(
            {"item_id": item.item_id, "market_price": market_price, "quote": quote,
             "profit": profit, "quick_profit": quick_profit, "variant": variant,
             "offer_estimate": offer_estimate}
        )


class FlakyTelegram(FakeTelegram):
    """Telegram double whose sends raise while ``fail`` is set, then record.

    Used to prove that a failed delivery is retried on the next poll rather than
    being silently marked as alerted/seen/ended.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def notify_listing(self, chat_id: str, listing: Listing) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        await super().notify_listing(chat_id, listing)

    async def notify_ended_listing(
        self, chat_id: str, listing: Listing, ended_at: str | None = None
    ) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        await super().notify_ended_listing(chat_id, listing, ended_at)

    async def notify_price_drop(
        self, chat_id: str, listing: Listing, old_price: float, new_price: float, pct: float
    ) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        await super().notify_price_drop(chat_id, listing, old_price, new_price, pct)

    async def notify_deal(self, chat_id: str, item, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        await super().notify_deal(chat_id, item, **kwargs)


def _service_config() -> SimpleNamespace:
    return SimpleNamespace(
        seed_sellers=(),
        telegram_chat_id="chat-1",
        telegram_allowed_chat_ids=(),
        telegram_allowed_usernames=(),
        telegram_send_photos=True,
        notify_existing_on_first_run=False,
        seller_check_delay_seconds=0,
        max_items_per_seller=20,
        observe_interval_seconds=180,
        observe_min_interval_seconds=30,
        observe_sellers=(),
        market_interval_seconds=600,
        market_min_interval_seconds=120,
        market_discount_percent=15,
        market_sample_size=200,
        market_min_deal_ratio=0.4,
        market_max_deals_per_cycle=5,
        market_min_sample=5,
        market_match_coverage=0.6,
        market_fuzzy_threshold=0.88,
        market_semantic=False,
        market_semantic_threshold=0.6,
        market_hydrate=True,
        market_hydrate_limit=20,
        market_price_source="listings",
        market_vision=False,
        market_vision_match_threshold=0.18,
        market_min_dispersion=0.12,
        market_deal_scan=False,
        market_resale_fee_percent=12.8,
        market_resale_fee_fixed=0.30,
        market_auctions_default=False,
        market_snipe_window_seconds=600,
        market_turbo_interval_seconds=45,
        market_low_bid_count=1,
        market_nobid_discount_percent=5,
        market_demand_grace_seconds=86400,
        market_demand_window_days=14,
        market_demand_min_events=3,
        market_arbitrage_threshold=20.0,
        market_arbitrage_interval_seconds=3600,
        market_health_threshold=5,
        market_risk_warn=40,
        market_risk_max=100,
        market_confidence_z=1.0,
        market_min_confidence=10,
        market_cleared_window_days=30,
        market_verify_ended_limit=3,
        market_category_consensus=0.7,
        market_resale_shipping=0.0,
        fx_rates=(),
        ebay_global_id="EBAY-GB",
        market_aliases=(),
        market_watches=(),
        seller_price_drop_percent=5,
        market_offer_aware=True,
        market_expected_offer_discount=10.0,
        market_vision_stock_threshold=0.55,
        market_vision_damage_threshold=0.55,
        market_vision_count_hint=False,
        market_vision_count_threshold=0.55,
        market_vision_preload=True,
        market_vision_reference=True,
        market_vision_ref_threshold=0.65,
        market_vision_auto_reference=True,
        market_vision_reference_sample=8,
        market_feedback_enabled=True,
        market_feedback_step=2.0,
        market_feedback_relax_step=1.0,
        market_feedback_max_nudge=10.0,
        heartbeat_enabled=False,
        heartbeat_interval_seconds=86400,
        backup_dir=Path("backups"),
        backup_keep=7,
        backup_interval_seconds=86400,
    )


def test_seed_seller_baseline_records_empty_successful_check(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.store = store
    service.ebay = FakeEbay([])
    try:
        store.add_seller("empty_seller")

        count = asyncio.run(service._seed_seller_baseline("empty_seller"))

        assert count == 0
        assert store.seller_has_successful_check("empty_seller")
        assert not store.seller_has_seen_items("empty_seller")
    finally:
        store.close()


def test_check_once_alerts_only_quantity_increases(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        previous_listings = [
            Listing(
                item_id="123456789012",
                seller="seller_one",
                title="Restocked",
                price="$10.00",
                url="https://example.test/itm/123456789012",
                quantity_available="2",
            ),
            Listing(
                item_id="234567890123",
                seller="seller_one",
                title="Lower stock",
                price="$12.00",
                url="https://example.test/itm/234567890123",
                quantity_available="6",
            ),
        ]
        for listing in previous_listings:
            store.mark_seen(listing, notified=False)
        store.upsert_active_listings(previous_listings)
        store.record_check("seller_one", listing_count=2, new_count=0)
        service.ebay = FakeEbay(
            [
                Listing(
                    item_id="123456789012",
                    seller="seller_one",
                    title="Restocked",
                    price="$10.00",
                    url="https://example.test/itm/123456789012",
                    quantity_available="5",
                ),
                Listing(
                    item_id="234567890123",
                    seller="seller_one",
                    title="Lower stock",
                    price="$12.00",
                    url="https://example.test/itm/234567890123",
                    quantity_available="3",
                ),
            ]
        )

        count = asyncio.run(service.check_once())

        assert count == 1
        assert service.telegram.quantity_increases == [("chat-1", "123456789012", 2, 5)]
        assert service.telegram.ended == []
    finally:
        store.close()


def test_seed_seller_baseline_marks_current_items_seen(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.store = store
    service.ebay = FakeEbay(
        [
            Listing(
                item_id="123456789012",
                seller="seller_one",
                title="Example",
                price="$10.00",
                url="https://example.test/itm/123456789012",
            )
        ]
    )
    try:
        store.add_seller("seller_one")

        count = asyncio.run(service._seed_seller_baseline("seller_one"))

        assert count == 1
        assert store.is_seen("123456789012")
        assert store.seller_has_successful_check("seller_one")
    finally:
        store.close()


def test_check_once_alerts_new_listing(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        existing = Listing(
            item_id="111111111111",
            seller="seller_one",
            title="Existing",
            price="GBP 5.00",
            url="https://example.test/itm/111111111111",
        )
        store.mark_seen(existing, notified=False)
        store.upsert_active_listings([existing])
        store.record_check("seller_one", listing_count=1, new_count=0)

        fresh = Listing(
            item_id="222222222222",
            seller="seller_one",
            title="Brand new",
            price="GBP 9.00",
            url="https://example.test/itm/222222222222",
        )
        service.ebay = FakeEbay([fresh, existing])

        count = asyncio.run(service.check_once())

        assert count == 1
        assert service.telegram.listings == [("chat-1", "222222222222")]
        assert service.telegram.ended == []
    finally:
        store.close()


def test_no_chat_configured_seeds_silently_without_flooding(tmp_path: Path) -> None:
    # With no chat configured yet, new listings must be seeded as seen (not held
    # unseen forever) so that adding a chat later doesn't flood the whole backlog.
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    service.configured_chats = lambda: []  # no chat yet
    try:
        store.add_seller("seller_one")
        store.record_check("seller_one", listing_count=0, new_count=0)  # not a first scan

        backlog = Listing(
            item_id="222222222222", seller="seller_one", title="Pre-existing",
            price="GBP 9.00", url="https://example.test/itm/222222222222",
        )
        service.ebay = FakeEbay([backlog])
        count = asyncio.run(service.check_once())
        assert count == 0  # nothing alerted with no chat
        assert service.telegram.listings == []
        assert store.is_seen("222222222222")  # but seeded so it won't re-fire

        # A chat appears and a genuinely new listing arrives — only it alerts.
        service.configured_chats = lambda: ["chat-1"]
        fresh = Listing(
            item_id="333333333333", seller="seller_one", title="Brand new",
            price="GBP 12.00", url="https://example.test/itm/333333333333",
        )
        service.ebay = FakeEbay([fresh, backlog])
        count = asyncio.run(service.check_once())
        assert count == 1
        assert service.telegram.listings == [("chat-1", "333333333333")]
    finally:
        store.close()


def test_check_once_skips_ended_detection_when_results_truncated(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.max_items_per_seller = 3
    service.config = config
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        previous = [
            Listing(
                item_id=item_id,
                seller="seller_one",
                title=f"Item {item_id}",
                price="GBP 1.00",
                url=f"https://example.test/itm/{item_id}",
            )
            for item_id in ("100000000001", "100000000002", "100000000003", "100000000004")
        ]
        for listing in previous:
            store.mark_seen(listing, notified=False)
        store.upsert_active_listings(previous)
        store.record_check("seller_one", listing_count=4, new_count=0)

        # A full page (len == max_items) means the result is truncated: items 3 and 4
        # are absent only because they fell off the page, not because they ended.
        current = previous[:2] + [
            Listing(
                item_id="100000000005",
                seller="seller_one",
                title="Newest",
                price="GBP 1.00",
                url="https://example.test/itm/100000000005",
            )
        ]
        service.ebay = FakeEbay(current)

        asyncio.run(service.check_once())

        assert service.telegram.ended == []
        assert service.telegram.listings == [("chat-1", "100000000005")]
    finally:
        store.close()


def test_check_once_alerts_ended_when_results_not_truncated(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.max_items_per_seller = 10
    service.config = config
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        still_active = Listing(
            item_id="100000000001",
            seller="seller_one",
            title="Still active",
            price="GBP 1.00",
            url="https://example.test/itm/100000000001",
        )
        gone = Listing(
            item_id="100000000002",
            seller="seller_one",
            title="Now gone",
            price="GBP 1.00",
            url="https://example.test/itm/100000000002",
        )
        for listing in (still_active, gone):
            store.mark_seen(listing, notified=False)
        store.upsert_active_listings([still_active, gone])
        store.record_check("seller_one", listing_count=2, new_count=0)

        # Only 1 listing returned, well under max_items=10, so the result is complete:
        # the absent listing genuinely ended.
        service.ebay = FakeEbay([still_active])

        count = asyncio.run(service.check_once())

        assert count == 1
        assert service.telegram.ended == [("chat-1", "100000000002")]
    finally:
        store.close()


def test_observed_changed_seller_detects_a_single_rename() -> None:
    service = object.__new__(EbaySpyService)

    def _listing(item_id: str, seller: str) -> Listing:
        return Listing(
            item_id=item_id,
            seller=seller,
            title="t",
            price="GBP 1.00",
            url=f"https://example.test/itm/{item_id}",
        )

    renamed = [_listing("100000000001", "new_name"), _listing("100000000002", "new_name")]
    assert service._observed_changed_seller("old_name", renamed) == "new_name"

    unchanged = [_listing("100000000001", "old_name")]
    assert service._observed_changed_seller("old_name", unchanged) is None

    ambiguous = [_listing("100000000001", "new_name"), _listing("100000000002", "other_name")]
    assert service._observed_changed_seller("old_name", ambiguous) is None

    assert service._observed_changed_seller("old_name", []) is None


def test_check_once_suppresses_ended_alert_when_item_still_active(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.max_items_per_seller = 10
    service.config = config
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        still_listed = Listing(
            item_id="100000000001",
            seller="seller_one",
            title="Still listed",
            price="GBP 1.00",
            url="https://example.test/itm/100000000001",
        )
        missing_from_search = Listing(
            item_id="100000000002",
            seller="seller_one",
            title="Missing from this poll",
            price="GBP 1.00",
            url="https://example.test/itm/100000000002",
        )
        for listing in (still_listed, missing_from_search):
            store.mark_seen(listing, notified=False)
        store.upsert_active_listings([still_listed, missing_from_search])
        store.record_check("seller_one", listing_count=2, new_count=0)

        # The search omits item 100000000002, but item_active reports it is
        # still listed -> the bot must suppress the false 'ended' alert.
        service.ebay = FakeEbay([still_listed], item_active_response=True)

        count = asyncio.run(service.check_once())

        assert count == 0
        assert service.telegram.ended == []
    finally:
        store.close()


def _observe_service(store: Store, ebay: FakeEbay) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.ebay = ebay
    service.stop_event = asyncio.Event()
    return service


def test_observe_seller_seeds_silently_on_first_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    fresh = Listing(
        item_id="222222222222",
        seller="seller_one",
        title="Brand new",
        price="GBP 9.00",
        url="https://example.test/itm/222222222222",
    )
    service = _observe_service(store, FakeEbay([fresh]))
    try:
        store.add_observed_seller("seller_one")

        count = asyncio.run(service._observe_seller("seller_one", ["chat-1"]))

        assert count == 0
        assert service.telegram.listings == []
        assert service.ebay.hydrated == []
        assert store.is_seen("222222222222")
        assert store.observed_seller_has_successful_check("seller_one")
    finally:
        store.close()


def test_observe_seller_alerts_and_hydrates_only_new_items(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    existing = Listing(
        item_id="111111111111",
        seller="seller_one",
        title="Already seen",
        price="GBP 5.00",
        url="https://example.test/itm/111111111111",
    )
    fresh = Listing(
        item_id="222222222222",
        seller="seller_one",
        title="Brand new",
        price="GBP 9.00",
        url="https://example.test/itm/222222222222",
    )
    service = _observe_service(store, FakeEbay([fresh, existing]))
    try:
        store.add_observed_seller("seller_one")
        store.mark_seen(existing, notified=False)
        store.record_observe_check("seller_one", new_count=0)  # marks a prior successful check

        count = asyncio.run(service._observe_seller("seller_one", ["chat-1"]))

        assert count == 1
        assert service.telegram.listings == [("chat-1", "222222222222")]
        assert service.ebay.hydrated == ["222222222222"]
        assert store.is_seen("222222222222")
    finally:
        store.close()


def test_observe_seller_records_error_without_raising(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")

    class FailingEbay(FakeEbay):
        async def seller_listings(self, seller: str, *, hydrate: bool = True) -> list[Listing]:
            raise RuntimeError("eBay search failed")

    service = _observe_service(store, FailingEbay([]))
    try:
        store.add_observed_seller("seller_one")

        count = asyncio.run(service._observe_seller("seller_one", ["chat-1"]))

        assert count == 0
        rows = store.list_observed_sellers()
        assert rows[0]["last_error"] == "eBay search failed"
        assert not store.observed_seller_has_successful_check("seller_one")
    finally:
        store.close()


def test_observe_seller_suppresses_items_below_price_floor(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    cheap = Listing(
        item_id="111111111111",
        seller="seller_one",
        title="Cheap junk",
        price="GBP 9.00",
        url="https://example.test/itm/111111111111",
    )
    pricey = Listing(
        item_id="222222222222",
        seller="seller_one",
        title="Worth knowing about",
        price="GBP 150.00",
        url="https://example.test/itm/222222222222",
    )
    service = _observe_service(store, FakeEbay([pricey, cheap]))
    try:
        store.add_observed_seller("seller_one", None, min_price=100.0)
        store.record_observe_check("seller_one", new_count=0)  # prior successful check

        count = asyncio.run(service._observe_seller("seller_one", ["chat-1"]))

        assert count == 1
        assert service.telegram.listings == [("chat-1", "222222222222")]
        # The sub-floor item is never hydrated (saves an API call) but is marked
        # seen so it isn't reconsidered every poll.
        assert service.ebay.hydrated == ["222222222222"]
        assert store.is_seen("111111111111")
    finally:
        store.close()


def test_check_once_suppresses_new_listing_below_price_floor(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one", min_price=100.0)
        existing = Listing(
            item_id="111111111111",
            seller="seller_one",
            title="Existing",
            price="GBP 200.00",
            url="https://example.test/itm/111111111111",
        )
        store.mark_seen(existing, notified=False)
        store.upsert_active_listings([existing])
        store.record_check("seller_one", listing_count=1, new_count=0)

        cheap = Listing(
            item_id="333333333333",
            seller="seller_one",
            title="Cheap junk",
            price="GBP 9.00",
            url="https://example.test/itm/333333333333",
        )
        pricey = Listing(
            item_id="222222222222",
            seller="seller_one",
            title="Worth it",
            price="GBP 150.00",
            url="https://example.test/itm/222222222222",
        )
        service.ebay = FakeEbay([pricey, cheap, existing])

        count = asyncio.run(service.check_once())

        assert count == 1
        assert service.telegram.listings == [("chat-1", "222222222222")]
        # Sub-floor item is still recorded so it won't be re-evaluated each poll.
        assert store.is_seen("333333333333")
    finally:
        store.close()


def test_observe_interval_for_applies_default_and_floor() -> None:
    service = object.__new__(EbaySpyService)
    service.config = _service_config()

    assert service._observe_interval_for({"interval_seconds": None}) == 180
    assert service._observe_interval_for({"interval_seconds": 600}) == 600
    assert service._observe_interval_for({"interval_seconds": 5}) == 30


def test_observe_sleep_seconds_caps_and_floors() -> None:
    assert EbaySpyService._observe_sleep_seconds({}, 100.0) == 10.0
    assert EbaySpyService._observe_sleep_seconds({"a": 103.0}, 100.0) == 3.0
    assert EbaySpyService._observe_sleep_seconds({"a": 500.0}, 100.0) == 10.0
    assert EbaySpyService._observe_sleep_seconds({"a": 90.0}, 100.0) == 1.0


def _market_item(item_id: str, total: float, title: str) -> MarketItem:
    return MarketItem(
        item_id=item_id,
        title=title,
        url=f"https://example.test/itm/{item_id}",
        seller=f"seller-{item_id}",
        currency="GBP",
        item_price=total,
        total_price=total,
    )


def _market_service(store: Store, ebay: FakeEbay) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.ebay = ebay
    service.stop_event = asyncio.Event()
    service._auction_turbo = {}
    return service


def test_check_market_watch_alerts_below_market(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Market clusters around 200; "great" is a real deal, "scam" is below the
    # 40% floor, the rest sit at/above market. All titles are comparable.
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [
        _market_item("scam", 50, title),
        _market_item("great", 150, title),
        _market_item("mkt-a", 190, title),
        _market_item("mkt-b", 200, title),
        _market_item("mkt-c", 210, title),
        _market_item("mkt-d", 250, title),
    ]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")

        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        assert sent == 1
        assert [item_id for _, item_id, _ in service.telegram.deals] == ["great"]
        row = store.get_market_watch(watch_id)
        assert row["market_price"] == 200  # trimmed median (the 50 outlier dropped)
        assert row["sample_size"] == 6
        # The same deal is not re-alerted on the next check.
        again = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))
        assert again == 0
    finally:
        store.close()


def test_market_watcher_survives_one_watch_failing(tmp_path: Path) -> None:
    # A failure while checking one watch must not kill the whole market-watcher
    # task (it is created once and never restarted) — otherwise a single bad deal
    # would silently end ALL deal alerts. It should be logged and the loop go on.
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        store.add_market_watch("dyson airblade hu02")
        service.configured_chats = lambda: ["chat-1"]

        async def boom(row, chats):
            service.stop_event.set()  # let the loop exit cleanly after this tick
            raise RuntimeError("kaboom")

        service._check_market_watch = boom
        # Must return normally (exception swallowed), not propagate out of the task.
        asyncio.run(service.run_market_watchers())
    finally:
        store.close()


def test_check_market_watch_caps_alerts_per_cycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Ten genuine deals among a market that clearly trades at 100. (Deals must
    # be the minority: if half of all listings sat at 60, that would be a second
    # price level, not ten bargains.)
    title = "Acme Widget X1 Gadget"
    ebay.market_items = [_market_item(f"deal-{i}", 60, title) for i in range(10)]
    ebay.market_items += [_market_item(f"mkt-{i}", 100, title) for i in range(30)]
    service = _market_service(store, ebay)
    service.config.market_max_deals_per_cycle = 3
    try:
        watch_id = store.add_market_watch("acme widget x1")

        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        assert sent == 3  # capped, not all ten in one burst
        assert len(service.telegram.deals) == 3
    finally:
        store.close()


def test_check_market_watch_prices_each_variant_separately(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Two variants: 128GB clusters ~300, 256GB ~400. A genuine deal sits in each.
    # The 256GB deal (330) is BELOW its variant's market (400) but ABOVE the
    # blended median (~305) — so only per-variant pricing catches it.
    items = [_market_item(f"128-{i}", 300 + i, "Apple iPhone 13 128GB") for i in range(6)]
    items += [_market_item(f"256-{i}", 400 + i, "Apple iPhone 13 256GB") for i in range(6)]
    items.append(_market_item("deal128", 240, "Apple iPhone 13 128GB"))  # ~20% under 128
    items.append(_market_item("deal256", 330, "Apple iPhone 13 256GB"))  # ~18% under 256
    items.append(_market_item("notdeal", 295, "Apple iPhone 13 128GB"))  # only ~2% under 128
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13")

        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        alerted = {item_id for _, item_id, _ in service.telegram.deals}
        assert alerted == {"deal128", "deal256"}
        assert sent == 2
        # Headline price is the larger/most-populated variant, tagged with it.
        row = store.get_market_watch(watch_id)
        assert row["market_variant"] in {"128gb", "256gb"}
    finally:
        store.close()


def _auction(item_id: str, bid_total: float, ends_in: int, bid_count: int | None = None) -> MarketItem:
    end = (datetime.now(timezone.utc) + timedelta(seconds=ends_in)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    return MarketItem(
        item_id=item_id,
        title="Apple iPhone 13",
        url=f"https://example.test/itm/{item_id}",
        seller="s",
        currency="GBP",
        item_price=bid_total,
        total_price=bid_total,
        buying_options=("AUCTION",),
        current_bid=bid_total,
        bid_count=bid_count,
        end_date=end,
    )


def test_no_bid_auction_alerts_at_relaxed_threshold(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Market ~400. A no-bid auction at 380 (only 5% below) would NOT clear the
    # normal 15% threshold, but qualifies under the relaxed no-competition rule.
    items = [_market_item(f"f{i}", 400 + i, "Apple iPhone 13") for i in range(6)]
    items.append(_auction("nobid", 380, ends_in=300, bid_count=0))
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_auctions=True)
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        assert sent == 1
        assert {item_id for _, item_id, _ in service.telegram.deals} == {"nobid"}
    finally:
        store.close()


def test_auction_sniping_two_stage_and_turbo(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Fixed-price market ~400 anchors the price; auctions priced off live bid.
    items = [_market_item(f"fix-{i}", 400 + i, "Apple iPhone 13") for i in range(6)]
    items.append(_auction("soon", 300, ends_in=300))  # below market, ends in 5 min
    items.append(_auction("later", 300, ends_in=7200))  # below market, ends in 2h
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_auctions=True)

        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        alerted = {item_id for _, item_id, _ in service.telegram.deals}
        assert alerted == {"soon", "later"}  # both auctions, no fixed deals at 400
        assert sent == 2
        # The imminent auction flags the watch for turbo polling.
        assert service._auction_turbo.get(watch_id) == 45
        # Stages recorded distinctly: the imminent one is a final-call.
        assert store.get_deal_alert(watch_id, "soon")["stage"] == "final"
        assert store.get_deal_alert(watch_id, "later")["stage"] == "deal"
    finally:
        store.close()


def test_chats_for_watch_owner_vs_broadcast(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    service.config.telegram_allowed_chat_ids = ("admin-1",)
    try:
        store.add_allowed_username("niner")
        store.add_chat("user-9", "niner")
        owned = store.add_market_watch("iphone 13", owner_chat_id="user-9")
        seeded = store.add_market_watch("ps5")  # no owner

        owned_row = store.get_market_watch(owned)
        seeded_row = store.get_market_watch(seeded)
        broadcast = ["a", "b"]

        # Owned watch -> owner + admins, not the whole broadcast list.
        assert service._chats_for_watch(owned_row, broadcast) == ["user-9", "admin-1"]
        # Ownerless watch -> broadcast to everyone.
        assert service._chats_for_watch(seeded_row, broadcast) == broadcast
        # An owner who is later uninvited stops receiving their watch's alerts.
        store.remove_allowed_username("niner")
        assert service._chats_for_watch(owned_row, broadcast) == ["admin-1"]
    finally:
        store.close()


def test_insights_overrides_price_with_sold_median(tmp_path: Path) -> None:
    from ebayspy.models import SoldItem

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Apple iPhone 13"
    # Live asking ~402; real sold median ~362. "marginal" (320) looks like a deal
    # vs live asking (15% off 402 = 342) but NOT vs real sold prices (15% off 362
    # = 308). "real" (280) is a genuine deal either way.
    ebay.market_items = [_market_item(f"f{i}", 400 + i, title) for i in range(6)]
    ebay.market_items.append(_market_item("marginal", 320, title))
    ebay.market_items.append(_market_item("real", 280, title))
    ebay.sold_items = [
        SoldItem(item_id=str(i), title=title, total_price=360 + i, currency="GBP")
        for i in range(6)
    ]
    service = _market_service(store, ebay)
    service.config.market_price_source = "insights"
    try:
        watch_id = store.add_market_watch("apple iphone 13")
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        # Sold pricing suppresses the false deal and keeps the genuine one.
        assert {item_id for _, item_id, _ in service.telegram.deals} == {"real"}
        assert sent == 1
        row = store.get_market_watch(watch_id)
        # Led by the ~362 sold median, not the ~402 live asking. (Six sales are
        # thin evidence, so the asks still pull the estimate up a couple of percent.)
        assert 360 <= row["market_price"] <= 375
    finally:
        store.close()


def test_lot_arbitrage_values_per_unit(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Single-unit market ~400. A job lot of 5 at 1400 = £280/unit -> 30% under,
    # worth ~£2000 as units. Normal matching would exclude the lot entirely.
    items = [_market_item(f"u{i}", 400 + i, "Apple iPhone 13") for i in range(6)]
    items.append(_market_item("lot", 1400, "Job Lot x5 Apple iPhone 13 Bundle"))
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_lots=True)
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        assert sent == 1
        assert {item_id for _, item_id, _ in service.telegram.deals} == {"lot"}
    finally:
        store.close()


def test_check_market_watch_no_comparables(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    ebay.market_items = []
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("nonexistent thing")

        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))

        assert sent == 0
        assert service.telegram.deals == []
        assert store.get_market_watch(watch_id)["market_price"] is None
    finally:
        store.close()


def test_callback_block_learns_exclude_term(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    service.config.telegram_allowed_chat_ids = ()
    service.config.telegram_allowed_usernames = ()
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        # A deal was alerted for a tap that wrongly matched.
        store.record_deal_alert(
            watch_id, "999", 200.0, variant=None, title="Dyson Airblade HU02 Tap AB14 White"
        )

        reply = asyncio.run(service.handle_callback("chat-1", "user", f"bl:{watch_id}:999"))

        assert "Blocked" in reply
        assert "999" in store.blocked_item_ids(watch_id)
        # A distinctive token from the title was learned as an exclude.
        assert store.get_market_watch(watch_id)["exclude_terms"]
        assert "ab14" in store.get_market_watch(watch_id)["exclude_terms"]
    finally:
        store.close()


def test_callback_mute_variant(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    service.config.telegram_allowed_chat_ids = ()
    service.config.telegram_allowed_usernames = ()
    try:
        watch_id = store.add_market_watch("iphone 13")
        store.record_deal_alert(watch_id, "777", 400.0, variant="pro · 256gb", title="iPhone 13 Pro")

        reply = asyncio.run(service.handle_callback("chat-1", "user", f"mv:{watch_id}:777"))

        assert "pro · 256gb" in reply
        assert "pro · 256gb" in store.muted_variants(watch_id)
    finally:
        store.close()


def test_handle_watch_parses_options(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        reply = asyncio.run(
            service._handle_watch(
                "chat-1",
                "user",
                "dyson airblade hu02 condition:used under:£500 discount:25 every:10m "
                "exclude:refurbished -bracket",
            )
        )

        assert "Watching the market" in reply
        rows = store.list_market_watches()
        assert len(rows) == 1
        row = rows[0]
        assert row["query"] == "dyson airblade hu02"
        assert row["condition"] == "used"
        assert row["max_price"] == 500
        assert row["discount_percent"] == 25
        assert row["interval_seconds"] == 600  # 10m
        assert row["exclude_terms"] == "refurbished, bracket"

        # /unwatch by id removes it.
        removed = service._handle_unwatch(str(row["id"]))
        assert "Stopped watching" in removed
        assert store.list_market_watches() == []
    finally:
        store.close()


def test_new_listing_alert_retried_after_send_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    telegram = FlakyTelegram()
    service.telegram = telegram
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        existing = Listing(
            item_id="111111111111",
            seller="seller_one",
            title="Existing",
            price="GBP 5.00",
            url="https://example.test/itm/111111111111",
        )
        store.mark_seen(existing, notified=False)
        store.upsert_active_listings([existing])
        store.record_check("seller_one", listing_count=1, new_count=0)

        fresh = Listing(
            item_id="222222222222",
            seller="seller_one",
            title="Brand new",
            price="GBP 9.00",
            url="https://example.test/itm/222222222222",
        )
        service.ebay = FakeEbay([fresh, existing])

        # Telegram is down: the alert fails and the item must stay unseen so it
        # is retried, not silently lost.
        count = asyncio.run(service.check_once())
        assert count == 0
        assert telegram.listings == []
        assert not store.is_seen("222222222222")

        # Telegram recovers: the next poll re-sends and only now marks it seen.
        telegram.fail = False
        count = asyncio.run(service.check_once())
        assert count == 1
        assert telegram.listings == [("chat-1", "222222222222")]
        assert store.is_seen("222222222222")
    finally:
        store.close()


def test_ended_alert_retried_after_send_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.max_items_per_seller = 10
    service.config = config
    service.store = store
    telegram = FlakyTelegram()
    service.telegram = telegram
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        still_active = Listing(
            item_id="100000000001",
            seller="seller_one",
            title="Still active",
            price="GBP 1.00",
            url="https://example.test/itm/100000000001",
        )
        gone = Listing(
            item_id="100000000002",
            seller="seller_one",
            title="Now gone",
            price="GBP 1.00",
            url="https://example.test/itm/100000000002",
        )
        for listing in (still_active, gone):
            store.mark_seen(listing, notified=False)
        store.upsert_active_listings([still_active, gone])
        store.record_check("seller_one", listing_count=2, new_count=0)

        # item_ended_state returns (active=False) -> genuinely ended.
        service.ebay = FakeEbay([still_active])

        # First poll: the ended alert send fails, so the item must remain a
        # candidate (not marked ended-notified) for the next poll.
        count = asyncio.run(service.check_once())
        assert count == 0
        assert telegram.ended == []

        # Telegram recovers: the ended alert fires exactly once on retry.
        telegram.fail = False
        count = asyncio.run(service.check_once())
        assert count == 1
        assert telegram.ended == [("chat-1", "100000000002")]
    finally:
        store.close()


def test_market_deal_alert_retried_after_send_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [
        _market_item("great", 150, title),
        _market_item("mkt-a", 190, title),
        _market_item("mkt-b", 200, title),
        _market_item("mkt-c", 210, title),
        _market_item("mkt-d", 250, title),
    ]
    service = _market_service(store, ebay)
    telegram = FlakyTelegram()
    service.telegram = telegram
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")

        # Telegram is down: the deal is not recorded as alerted, so it stays
        # actionable instead of being permanently suppressed.
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))
        assert sent == 0
        assert telegram.deals == []
        assert store.deal_already_alerted(watch_id, "great") is False

        # Telegram recovers: the deal alerts on the next check.
        telegram.fail = False
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))
        assert sent == 1
        assert [item_id for _, item_id, _ in telegram.deals] == ["great"]
        assert store.deal_already_alerted(watch_id, "great") is True
    finally:
        store.close()


def _bo_item(item_id: str, total: float, title: str) -> MarketItem:
    return MarketItem(
        item_id=item_id, title=title, url=f"https://example.test/itm/{item_id}",
        seller="seller", currency="GBP", item_price=total, total_price=total,
        buying_options=("FIXED_PRICE", "BEST_OFFER"),
    )


def test_check_once_alerts_price_drop(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        item = Listing(item_id="111111111111", seller="seller_one", title="Widget",
                       price="GBP 100.00", url="https://example.test/itm/111111111111")
        store.mark_seen(item, notified=False)
        store.upsert_active_listings([item])
        store.record_check("seller_one", listing_count=1, new_count=0)

        cheaper = Listing(item_id="111111111111", seller="seller_one", title="Widget",
                          price="GBP 80.00", url="https://example.test/itm/111111111111")
        service.ebay = FakeEbay([cheaper])
        assert asyncio.run(service.check_once()) == 1
        assert service.telegram.price_drops == [("chat-1", "111111111111", 100.0, 80.0)]

        # Same low price next poll -> deduped, no re-alert.
        service.ebay = FakeEbay([cheaper])
        assert asyncio.run(service.check_once()) == 0
        assert len(service.telegram.price_drops) == 1
    finally:
        store.close()


def test_check_once_records_poll_health(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        service.ebay = FakeEbay([])
        count = asyncio.run(service.check_once())
        assert store.get_meta("last_poll_ok_at") is not None
        assert store.get_meta("last_poll_alert_count") == str(count)
        assert store.health_snapshot()["seller_count"] == 1
    finally:
        store.close()


def test_offer_candidate_alerted_at_offer_stage(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    # Market median ~200; offer-eligible item at 190 is above the 15% deal threshold
    # (170) so not a list deal, but a ~15% offer (est 170) would clear it.
    ebay.market_items = [
        _bo_item("offer", 190, title),
        _market_item("a", 195, title), _market_item("b", 200, title),
        _market_item("c", 205, title), _market_item("d", 210, title),
        _market_item("e", 200, title),
    ]
    service = _market_service(store, ebay)
    service.config.market_offer_aware = True
    service.config.market_expected_offer_discount = 15.0
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        sent = asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))
        assert sent == 1
        assert store.get_deal_alert(watch_id, "offer")["stage"] == "offer"
        assert [item_id for _, item_id, _ in service.telegram.deals] == ["offer"]

        # Disabled -> the same item is not flagged.
        store2 = Store(tmp_path / "t2.sqlite3")
        svc2 = _market_service(store2, ebay)
        svc2.config.market_offer_aware = False
        wid2 = store2.add_market_watch("dyson airblade hu02")
        assert asyncio.run(svc2._check_market_watch(store2.get_market_watch(wid2), ["chat-1"])) == 0
        store2.close()
    finally:
        store.close()


def test_callback_feedback_tunes_required_discount(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        watch_id = store.add_market_watch("iphone 13")
        reply = asyncio.run(service.handle_callback("chat-1", "user", f"fd:{watch_id}:item-1"))
        assert store.feedback_counts(watch_id) == (0, 1)
        assert store.get_discount_nudge(watch_id) == service.config.market_feedback_step
        assert "discount" in reply.lower()

        asyncio.run(service.handle_callback("chat-1", "user", f"fu:{watch_id}:item-1"))
        assert store.feedback_counts(watch_id) == (1, 1)
        assert store.get_discount_nudge(watch_id) == (
            service.config.market_feedback_step - service.config.market_feedback_relax_step
        )
    finally:
        store.close()


def test_handle_command_health(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service._started_at = datetime.now(timezone.utc)
    try:
        store.add_seller("seller_one")
        reply = asyncio.run(service.handle_command("chat-1", "user", "/health", ""))
        assert "uptime" in reply and "sellers" in reply
    finally:
        store.close()


def _command_service(store: Store, ebay: FakeEbay) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.ebay = ebay
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    return service


def test_handle_command_add_with_price_floor(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _command_service(store, FakeEbay([]))
    try:
        reply = asyncio.run(service.handle_command("chat-1", "user", "/add", "lithosale [100]"))
        assert "≥100" in reply
        assert store.get_seller_min_price("lithosale") == 100.0

        # Re-adding with a new floor retunes it in place instead of being rejected.
        reply = asyncio.run(service.handle_command("chat-1", "user", "/add", "lithosale [250]"))
        assert "Updated" in reply and "≥250" in reply
        assert store.get_seller_min_price("lithosale") == 250.0

        # A bad floor token is rejected rather than silently dropped.
        reply = asyncio.run(service.handle_command("chat-1", "user", "/add", "other xyz"))
        assert "number" in reply.lower()
        assert not store.has_seller("other")
    finally:
        store.close()


def test_handle_command_floor_sets_and_clears(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _command_service(store, FakeEbay([]))
    try:
        store.add_seller("lithosale")
        store.add_observed_seller("lithosale")

        reply = asyncio.run(service.handle_command("chat-1", "user", "/floor", "lithosale [100]"))
        assert "≥100" in reply and "watch and observe" in reply
        assert store.get_seller_min_price("lithosale") == 100.0
        assert store.get_observed_min_price("lithosale") == 100.0

        reply = asyncio.run(service.handle_command("chat-1", "user", "/floor", "lithosale none"))
        assert "any price" in reply
        assert store.get_seller_min_price("lithosale") is None
        assert store.get_observed_min_price("lithosale") is None

        reply = asyncio.run(service.handle_command("chat-1", "user", "/floor", "ghost 50"))
        assert "not on the watch or observe list" in reply
    finally:
        store.close()


def test_handle_command_observe_with_interval_and_floor(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _command_service(store, FakeEbay([]))
    try:
        reply = asyncio.run(
            service.handle_command("chat-1", "user", "/observe", "techbargains 90s [100]")
        )
        assert "every 90s" in reply and "≥100" in reply
        assert store.get_observed_min_price("techbargains") == 100.0
        rows = store.list_observed_sellers()
        assert rows[0]["interval_seconds"] == 90
    finally:
        store.close()


def _heartbeat_service(store: Store) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.heartbeat_enabled = True
    config.heartbeat_interval_seconds = 18000
    config.telegram_chat_id = "chat-1"
    service.config = config
    service.store = store
    service.telegram = FakeTelegram()
    service._started_at = datetime.now(timezone.utc)
    return service


def test_heartbeat_sends_when_quiet(tmp_path: Path) -> None:
    # With nothing heard within the interval, a heartbeat goes out so a silent
    # stall (or a sleeping Mac woken just to poll) still produces a 6-hourly ping.
    store = Store(tmp_path / "test.sqlite3")
    service = _heartbeat_service(store)
    try:
        asyncio.run(service._maybe_send_heartbeat())
        assert service.telegram.messages, "expected a heartbeat to be sent"
        assert "Heartbeat" in service.telegram.messages[0][1]
        assert store.get_meta("last_heartbeat_at") is not None
    finally:
        store.close()


def test_heartbeat_skips_when_recently_active(tmp_path: Path) -> None:
    # A real alert within the interval already proves liveness — no heartbeat.
    store = Store(tmp_path / "test.sqlite3")
    service = _heartbeat_service(store)
    try:
        store.set_meta("last_alert_at", datetime.now(timezone.utc).isoformat())
        asyncio.run(service._maybe_send_heartbeat())
        assert not service.telegram.messages
        assert store.get_meta("last_heartbeat_at") is None
    finally:
        store.close()


def test_heartbeat_disabled_is_silent(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _heartbeat_service(store)
    service.config.heartbeat_enabled = False
    try:
        asyncio.run(service._maybe_send_heartbeat())
        assert not service.telegram.messages
    finally:
        store.close()


def _management_service(store: Store) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    return service


def _callback_datas(markup: dict | None) -> set[str]:
    if not markup:
        return set()
    return {b["callback_data"] for row in markup["inline_keyboard"] for b in row}


def test_list_command_sends_one_remove_button_per_seller(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _management_service(store)
    try:
        store.add_seller("lithosale")
        store.add_seller("chfle-6052")
        reply = asyncio.run(service.handle_command("chat-1", "user", "/list", ""))
        # Sent directly with buttons, so the plain-text reply path stays empty.
        assert reply == ""
        assert len(service.telegram.messages) == 1
        _chat, _text, markup = service.telegram.messages[0]
        assert _callback_datas(markup) == {"rs:lithosale", "rs:chfle-6052"}
    finally:
        store.close()


def test_watches_and_observing_commands_send_remove_buttons(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _management_service(store)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        store.add_observed_seller("lithosale")

        asyncio.run(service.handle_command("chat-1", "user", "/watches", ""))
        asyncio.run(service.handle_command("chat-1", "user", "/observing", ""))

        watches_markup = service.telegram.messages[0][2]
        observing_markup = service.telegram.messages[1][2]
        assert _callback_datas(watches_markup) == {f"uw:{watch_id}"}
        assert _callback_datas(observing_markup) == {"ro:lithosale"}
    finally:
        store.close()


def test_remove_button_tap_removes_seller_and_refreshes_in_place(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _management_service(store)
    try:
        store.add_seller("lithosale")
        store.add_seller("chfle-6052")

        toast = asyncio.run(service.handle_callback("chat-1", "user", "rs:lithosale", 42))

        assert "Removed lithosale" in toast
        assert [r["username"] for r in store.list_seller_rows()] == ["chfle-6052"]
        # The message is edited in place to drop the removed row's button.
        assert service.telegram.edits, "expected the list to refresh after removal"
        _chat, message_id, _text, markup = service.telegram.edits[-1]
        assert message_id == 42
        assert _callback_datas(markup) == {"rs:chfle-6052"}
    finally:
        store.close()


def test_last_removal_clears_the_keyboard(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _management_service(store)
    try:
        store.add_seller("lithosale")
        asyncio.run(service.handle_callback("chat-1", "user", "rs:lithosale", 1))
        assert store.list_seller_rows() == []
        _chat, _mid, _text, markup = service.telegram.edits[-1]
        assert markup == {"inline_keyboard": []}  # buttons cleared when empty
    finally:
        store.close()


def test_unwatch_and_unobserve_button_taps_remove_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _management_service(store)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        store.add_observed_seller("lithosale")

        watch_toast = asyncio.run(
            service.handle_callback("chat-1", "user", f"uw:{watch_id}", 7)
        )
        observe_toast = asyncio.run(
            service.handle_callback("chat-1", "user", "ro:lithosale", 8)
        )

        assert "Stopped watching" in watch_toast
        assert "Stopped observing lithosale" in observe_toast
        assert store.list_market_watches() == []
        assert store.list_observed_sellers() == []
    finally:
        store.close()


def test_remove_button_callback_data_skipped_when_too_long() -> None:
    # An eBay username long enough to push callback_data past 64 bytes gets no
    # button (it would make Telegram reject the whole message); text still lists it.
    assert EbaySpyService._remove_button("x", "rs:" + "a" * 62) is None
    assert EbaySpyService._remove_button("x", "rs:lithosale") is not None


def _vision_service(**overrides) -> EbaySpyService:
    service = object.__new__(EbaySpyService)
    config = _service_config()
    config.market_vision = True
    for key, value in overrides.items():
        setattr(config, key, value)
    service.config = config
    return service


def _vision_item() -> MarketItem:
    return MarketItem(
        item_id="1", title="iPhone 13", url="https://ebay/1", seller="s",
        currency="GBP", item_price=400.0, total_price=400.0,
        image_url="https://img.test/listing.jpg",
    )


def test_vision_check_drops_when_photo_differs_from_reference(monkeypatch) -> None:
    from ebayspy import service as service_mod

    service = _vision_service(market_vision_ref_threshold=0.65)
    monkeypatch.setattr(service_mod.vision, "available", lambda: True)
    monkeypatch.setattr(service_mod.vision, "match_score", lambda url, q: 0.5)
    monkeypatch.setattr(service_mod.vision, "vision_flags", lambda url, cond: None)
    # Listing photo looks nothing like the reference stock image -> dropped.
    monkeypatch.setattr(service_mod.vision, "image_match", lambda ref, url: 0.40)
    drop, note = asyncio.run(
        service._vision_check(_vision_item(), "iphone 13", "https://img.test/stock.jpg")
    )
    assert drop is True and note == ""


def test_vision_check_keeps_and_notes_a_matching_reference(monkeypatch) -> None:
    from ebayspy import service as service_mod

    service = _vision_service(market_vision_ref_threshold=0.65)
    monkeypatch.setattr(service_mod.vision, "available", lambda: True)
    monkeypatch.setattr(service_mod.vision, "match_score", lambda url, q: 0.5)
    monkeypatch.setattr(service_mod.vision, "vision_flags", lambda url, cond: None)
    monkeypatch.setattr(service_mod.vision, "image_match", lambda ref, url: 0.88)
    drop, note = asyncio.run(
        service._vision_check(_vision_item(), "iphone 13", "https://img.test/stock.jpg")
    )
    assert drop is False and "Matches the item" in note and "88%" in note


def test_vision_check_without_reference_skips_image_match(monkeypatch) -> None:
    from ebayspy import service as service_mod

    service = _vision_service()
    monkeypatch.setattr(service_mod.vision, "available", lambda: True)
    monkeypatch.setattr(service_mod.vision, "match_score", lambda url, q: 0.5)
    monkeypatch.setattr(service_mod.vision, "vision_flags", lambda url, cond: None)

    def _boom(ref, url):  # must not be called without a reference
        raise AssertionError("image_match should not run without a reference")

    monkeypatch.setattr(service_mod.vision, "image_match", _boom)
    drop, note = asyncio.run(service._vision_check(_vision_item(), "iphone 13", None))
    assert drop is False and "Matches the item" not in note


def test_handle_refimage_set_show_clear(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _vision_service()
    service.store = store
    try:
        watch_id = store.add_market_watch("iPhone 13 Pro", condition="used")
        # Show before pinning -> auto message.
        assert "auto-deriving" in service._handle_refimage(str(watch_id))
        # Pin a URL.
        reply = service._handle_refimage(f"{watch_id} https://img.test/stock.jpg")
        assert "Pinned" in reply
        assert store.get_market_watch(watch_id)["reference_image_url"] == (
            "https://img.test/stock.jpg"
        )
        # Show after pinning -> echoes the URL.
        assert "https://img.test/stock.jpg" in service._handle_refimage(str(watch_id))
        # Clear -> back to auto.
        assert "back to auto" in service._handle_refimage(f"{watch_id} clear")
        assert store.get_market_watch(watch_id)["reference_image_url"] is None
        # Bad inputs.
        assert "Usage" in service._handle_refimage("")
        assert "No market watch" in service._handle_refimage("9999 https://img.test/x.jpg")
        assert "image URL" in service._handle_refimage(f"{watch_id} not-a-url")
    finally:
        store.close()


# --- fair-value engine in the watch flow --------------------------------------


def _check(service: EbaySpyService, store: Store, watch_id: int) -> int:
    return asyncio.run(service._check_market_watch(store.get_market_watch(watch_id), ["chat-1"]))


def _alerted(service: EbaySpyService) -> list[str]:
    return [item_id for _, item_id, _ in service.telegram.deals]


def test_scam_listings_do_not_block_real_deals(tmp_path: Path) -> None:
    # Five all-red-flag listings rank above a genuine deal. They are suppressed
    # without being recorded, so if the per-cycle cap were taken BEFORE the risk
    # screen they would fill every slot on every check and the real deal would
    # never be sent.
    from dataclasses import replace

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 100 + i % 3, title) for i in range(12)]
    ebay.market_items += [
        replace(
            _market_item(f"scam{i}", 45, title),
            seller_feedback_score=2, seller_feedback_percent=80.0, item_location="CN",
        )
        for i in range(5)
    ]
    ebay.market_items.append(_market_item("real", 80, title))
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        assert _check(service, store, watch_id) == 1
        assert _alerted(service) == ["real"]
    finally:
        store.close()


def test_deal_alert_carries_the_quote_and_both_exit_prices(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 195 + i, title) for i in range(10)]
    ebay.market_items.append(_market_item("great", 150, title))
    service = _market_service(store, ebay)
    service.config.market_resale_shipping = 5.0
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        assert _check(service, store, watch_id) == 1
        detail = service.telegram.deal_details[0]
        quote = detail["quote"]
        assert quote.low < quote.price < quote.high
        assert [part.source for part in quote.components] == ["asks"]
        # Profit is net of fees AND the postage you pay; a fast sale earns less.
        assert detail["quick_profit"] < detail["profit"]
        expected = quote.price * (1 - 12.8 / 100) - 0.30 - 5.0 - 150
        assert abs(detail["profit"] - expected) < 1e-6
        row = store.get_market_watch(watch_id)
        assert row["market_low"] < row["market_price"] < row["market_high"]
        assert 0 < row["market_confidence"] <= 50  # asking prices alone
    finally:
        store.close()


def test_ended_listings_feed_the_fair_value(tmp_path: Path) -> None:
    import json

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Sony WH-1000XM5 Headphones"
    # Sellers are asking ~244; eight comparable listings ended last week at ~226.
    ebay.market_items = [_market_item(f"a{i}", 240 + i, title) for i in range(8)]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("sony wh-1000xm5")
        store.record_market_sightings(
            watch_id,
            [
                (f"gone{i}", "", 222 + i, "GBP", None, title, f"old-{i}",
                 json.dumps({"qualifier": "base"}))
                for i in range(8)
            ],
        )
        store.conn.execute(
            "update market_listings set ended_at = datetime('now', '-3 days'), "
            "last_seen_at = datetime('now', '-3 days'), ended_confirmed = 1 "
            "where item_id like 'gone%'"
        )
        store.conn.commit()
        _check(service, store, watch_id)
        row = store.get_market_watch(watch_id)
        # Fair value sits nearer what cleared than what is still unsold, and
        # having transaction proxies lifts confidence above asks-only (≤ 50).
        assert 226 < row["market_price"] < 236
        assert row["market_confidence"] > 50
        reply = asyncio.run(service.handle_command("chat-1", None, "/quote", str(watch_id)))
        assert "ended" in reply and "asks" in reply and "alerts at or below" in reply
    finally:
        store.close()


def test_ended_listing_blocked_by_user_stops_counting(tmp_path: Path) -> None:
    import json

    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        watch_id = store.add_market_watch("sony wh-1000xm5")
        store.record_market_sightings(
            watch_id,
            [("gone", "", 200, "GBP", None, "Sony WH-1000XM5", "s", json.dumps({"qualifier": "base"})),
             ("case", "", 15, "GBP", None, "Sony WH-1000XM5 Carry Case", "s2",
              json.dumps({"qualifier": "base"}))],
        )
        store.conn.execute(
            "update market_listings set ended_at = datetime('now', '-1 days'), ended_confirmed = 1"
        )
        store.conn.commit()
        row = store.get_market_watch(watch_id)
        # The accessory is re-screened by today's matcher, not trusted on file.
        assert [o.price for o in service._cleared_observations(row, [])[""]] == [200]
        store.block_market_item(watch_id, "gone")
        assert service._cleared_observations(row, []) == {}
    finally:
        store.close()


def test_for_parts_condition_is_never_a_comparable(tmp_path: Path) -> None:
    from dataclasses import replace

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 198 + i, title) for i in range(8)]
    # A clean title, 40% under market — but eBay's condition says it is for parts.
    ebay.market_items.append(
        replace(_market_item("parts", 120, title),
                condition="For parts or not working", condition_id="7000")
    )
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        assert _check(service, store, watch_id) == 0
        assert store.get_market_watch(watch_id)["comparable_size"] == 8
    finally:
        store.close()


def test_listing_with_unknown_variant_is_not_priced_as_one(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    items = [_market_item(f"a{i}", 300 + i, "Apple iPhone 13 128GB") for i in range(6)]
    items += [_market_item(f"b{i}", 400 + i, "Apple iPhone 13 256GB") for i in range(6)]
    # No capacity in the title: these could be either. Their own mixed bucket
    # would have a median of 400 and make every 300 one look 25% under market.
    for i, price in enumerate([300, 300, 300, 300, 400, 400, 400, 400, 500, 500, 500, 500]):
        items.append(_market_item(f"x{i}", price, "Apple iPhone 13 Unlocked"))
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13")
        assert _check(service, store, watch_id) == 0
        assert store.market_dimensions(watch_id) == ["capacity"]
    finally:
        store.close()


def test_category_consensus_drops_listings_filed_elsewhere(tmp_path: Path) -> None:
    from dataclasses import replace

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Sony A7 III Camera"
    ebay.market_items = [
        replace(_market_item(f"c{i}", 1000 + i, title), category_id="31388",
                category_name="Digital Cameras")
        for i in range(10)
    ]
    # Same words, 30% cheaper, but filed under camera accessories.
    ebay.market_items.append(
        replace(_market_item("stray", 700, title), category_id="999", category_name="Accessories")
    )
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("sony a7 iii")
        assert _check(service, store, watch_id) == 0
        service.config.market_category_consensus = 0.0  # off: the stray is a "deal"
        assert _check(service, store, watch_id) == 1
        assert _alerted(service) == ["stray"]
    finally:
        store.close()


def test_truncated_sample_confirms_an_ending_before_counting_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 200 + i, title) for i in range(6)]
    service = _market_service(store, ebay)
    service.config.market_sample_size = 6  # a full page: a window, not the market
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")

        def absent_listing():
            store.record_market_sightings(watch_id, [("old", "", 190, "GBP", None)])
            store.conn.execute(
                "update market_listings set last_seen_at = datetime('now', '-2 days'), "
                "ended_at = null where item_id = 'old'"
            )
            store.conn.commit()

        def ended_at():
            return store.conn.execute(
                "select ended_at from market_listings where item_id = 'old'"
            ).fetchone()[0]

        # Slid out of the window but still live on eBay: NOT an ending.
        absent_listing()
        ebay.item_active_response = True
        _check(service, store, watch_id)
        assert ended_at() is None
        # eBay cannot say right now: leave it undecided rather than guess.
        absent_listing()
        ebay.item_active_response = None
        _check(service, store, watch_id)
        assert ended_at() is None
        # Confirmed gone: now it is an ending.
        ebay.item_active_response = False
        _check(service, store, watch_id)
        assert ended_at() is not None
    finally:
        store.close()


def test_auction_keeps_turbo_polling_after_its_heads_up(tmp_path: Path) -> None:
    # The heads-up fires ~19 minutes out. On the next (turbo) check the item is
    # already alerted — turbo must stay armed or the final call is never seen.
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    items = [_market_item(f"fix-{i}", 400 + i, "Apple iPhone 13") for i in range(6)]
    items.append(_auction("snipe", 300, ends_in=1150))
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_auctions=True)
        assert _check(service, store, watch_id) == 1
        assert service._auction_turbo.get(watch_id) == 45
        assert _check(service, store, watch_id) == 0  # no repeat alert...
        assert service._auction_turbo.get(watch_id) == 45  # ...but still on turbo
    finally:
        store.close()


def test_few_bids_only_relax_the_bar_near_the_close(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    items = [_market_item(f"f{i}", 400 + i, "Apple iPhone 13") for i in range(6)]
    # 5% under market with no bids — but two days from ending, when "no bids"
    # just means nobody has bothered yet.
    items.append(_auction("early", 380, ends_in=172800, bid_count=0))
    ebay.market_items = items
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_auctions=True)
        assert _check(service, store, watch_id) == 0
    finally:
        store.close()


def test_observe_alert_retried_after_send_failure(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    fresh = Listing(
        item_id="222222222222", seller="seller_one", title="Brand new",
        price="GBP 9.00", url="https://example.test/itm/222222222222",
    )
    service = _observe_service(store, FakeEbay([fresh]))
    service.telegram = FlakyTelegram()
    try:
        store.add_observed_seller("seller_one")
        store.record_observe_check("seller_one", new_count=0)  # past the silent seed
        assert asyncio.run(service._observe_seller("seller_one", ["chat-1"])) == 0
        assert not store.is_seen("222222222222")  # not lost: still pending
        service.telegram.fail = False
        assert asyncio.run(service._observe_seller("seller_one", ["chat-1"])) == 1
        assert service.telegram.listings == [("chat-1", "222222222222")]
    finally:
        store.close()


def test_health_notice_is_html_escaped(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        asyncio.run(service._notify_text(["chat-1"], "watch “m&s <jumper>” failed: <html>502"))
        assert service.telegram.messages[0][1] == (
            "watch “m&amp;s &lt;jumper&gt;” failed: &lt;html&gt;502"
        )
    finally:
        store.close()


def test_quote_command_usage_and_unknown_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        assert "Usage" in asyncio.run(service.handle_command("chat-1", None, "/quote", ""))
        assert "No market watch" in asyncio.run(
            service.handle_command("chat-1", None, "/quote", "99")
        )
    finally:
        store.close()


def test_price_drop_alert_retried_after_send_failure(tmp_path: Path) -> None:
    # A drop whose alert could not be delivered must still look like a drop on
    # the next poll; overwriting the stored price would silently lose it.
    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FlakyTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        item = Listing(item_id="111111111111", seller="seller_one", title="Widget",
                       price="GBP 100.00", url="https://example.test/itm/111111111111")
        store.mark_seen(item, notified=False)
        store.upsert_active_listings([item])
        store.record_check("seller_one", listing_count=1, new_count=0)
        cheaper = Listing(item_id="111111111111", seller="seller_one", title="Widget",
                          price="GBP 80.00", url="https://example.test/itm/111111111111")
        service.ebay = FakeEbay([cheaper])

        assert asyncio.run(service.check_once()) == 0  # Telegram down
        service.telegram.fail = False
        assert asyncio.run(service.check_once()) == 1  # retried, not lost
        assert service.telegram.price_drops == [("chat-1", "111111111111", 100.0, 80.0)]
        assert asyncio.run(service.check_once()) == 0  # and then deduped as usual
    finally:
        store.close()


def test_poll_where_every_seller_fails_is_not_recorded_as_healthy(tmp_path: Path) -> None:
    import httpx

    class TimingOut(FakeEbay):
        async def seller_listings(self, seller: str, *, hydrate: bool = True) -> list[Listing]:
            raise httpx.ReadTimeout("")  # str() of this is empty

    store = Store(tmp_path / "test.sqlite3")
    service = object.__new__(EbaySpyService)
    service.config = _service_config()
    service.store = store
    service.telegram = FakeTelegram()
    service.stop_event = asyncio.Event()
    service._check_lock = asyncio.Lock()
    try:
        store.add_seller("seller_one")
        service.ebay = TimingOut([])
        asyncio.run(service.check_once())
        assert store.get_meta("last_poll_ok_at") is None  # /health must not say "healthy"
        assert store.status_rows()[0]["last_error"] == "ReadTimeout"  # nor /status "ok"
        service.ebay = FakeEbay([])
        asyncio.run(service.check_once())
        assert store.get_meta("last_poll_ok_at")
    finally:
        store.close()


def test_rename_probe_runs_at_most_daily(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        assert service._rename_probe_due("Seller_One") is True
        assert service._rename_probe_due("seller_one") is False  # same seller, same day
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        store.set_meta("seller_rename_probe_at:seller_one", stale)
        assert service._rename_probe_due("seller_one") is True
    finally:
        store.close()


# --- regressions found by independent review of the fair-value commit ----------


def test_only_confirmed_endings_count_as_price_evidence(tmp_path: Path) -> None:
    import json

    class CountingEbay(FakeEbay):
        asked: list[str] = []

        async def item_active(self, item_id: str) -> bool | None:
            self.asked.append(item_id)
            return self.item_active_response

    store = Store(tmp_path / "test.sqlite3")
    ebay = CountingEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 97 + i, title) for i in range(8)]
    service = _market_service(store, ebay)
    service.config.market_verify_ended_limit = 10
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        # Eight dearer listings vanish from the sample — e.g. the user narrowed
        # the watch to a category. They are still live on eBay.
        store.record_market_sightings(
            watch_id,
            [(f"gone{i}", "", 135 + i, "GBP", None, title, f"o{i}",
              json.dumps({"qualifier": "base"})) for i in range(8)],
        )
        store.conn.execute(
            "update market_listings set last_seen_at = datetime('now', '-2 days')"
        )
        store.conn.commit()
        row = store.get_market_watch(watch_id)

        ebay.item_active_response = None  # eBay cannot say: absence alone proves nothing
        _check(service, store, watch_id)
        assert service._cleared_observations(row, []) == {}
        assert abs(store.get_market_watch(watch_id)["market_price"] - 100.5) < 1

        ebay.item_active_response = True  # confirmed still live: never evidence...
        _check(service, store, watch_id)
        assert service._cleared_observations(row, []) == {}
        asked = len(ebay.asked)
        _check(service, store, watch_id)
        assert len(ebay.asked) == asked  # ...and not asked about again
        stats = store.market_demand_stats(watch_id, 14)
        assert stats["ended_in_window"] == 0  # nor counted as sold by the demand read
    finally:
        store.close()


def test_confirmed_ending_becomes_price_evidence(tmp_path: Path) -> None:
    import json

    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 97 + i, title) for i in range(8)]
    service = _market_service(store, ebay)
    service.config.market_verify_ended_limit = 10
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        store.record_market_sightings(
            watch_id,
            [(f"gone{i}", "", 88 + i, "GBP", None, title, f"o{i}",
              json.dumps({"qualifier": "base"})) for i in range(4)],
        )
        store.conn.execute(
            "update market_listings set last_seen_at = datetime('now', '-2 days')"
        )
        store.conn.commit()
        ebay.item_active_response = False  # getItem says they ended
        _check(service, store, watch_id)
        row = store.get_market_watch(watch_id)
        assert len(service._cleared_observations(row, [])[""]) == 4
        _check(service, store, watch_id)  # the next check prices with them
        assert store.get_market_watch(watch_id)["market_price"] < 100
    finally:
        store.close()


def test_stuck_dimension_is_released_when_listings_stop_stating_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    ebay.market_items = [
        _market_item(f"x{i}", 300 + i, "Apple iPhone 13 Unlocked") for i in range(12)
    ]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("apple iphone 13")
        store.set_market_dimensions(watch_id, ["capacity"])  # chosen on an earlier sample
        _check(service, store, watch_id)
        # Holding on to "capacity" would leave all twelve unpriced, forever.
        assert store.market_dimensions(watch_id) == []
        assert store.get_market_watch(watch_id)["market_price"] is not None
    finally:
        store.close()


def test_mute_survives_the_label_gaining_a_dimension() -> None:
    muted = {"128gb"}
    assert EbaySpyService._is_muted("128gb", muted)
    assert EbaySpyService._is_muted("128gb · used", muted)  # market later split by condition
    assert EbaySpyService._is_muted("pro · 128gb · used", muted)
    assert not EbaySpyService._is_muted("256gb · used", muted)
    assert not EbaySpyService._is_muted("", muted)
    assert EbaySpyService._is_muted("", {""})


def test_quote_shows_the_alert_price_the_alerts_actually_use(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Dyson Airblade HU02 Hand Dryer"
    ebay.market_items = [_market_item(f"m{i}", 200 + i % 3, title) for i in range(30)]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("dyson airblade hu02")
        plain = asyncio.run(service.handle_command("chat-1", None, "/quote", str(watch_id)))
        store.bump_discount_nudge(watch_id, 8.0, -10.0, 10.0)  # 👎 feedback: 15% -> 23%
        nudged = asyncio.run(service.handle_command("chat-1", None, "/quote", str(watch_id)))
        assert "alerts at or below £170.85" in plain
        assert "alerts at or below £154.77" in nudged
    finally:
        store.close()


def test_turbo_polling_is_cleared_by_a_failed_check(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        watch_id = store.add_market_watch("apple iphone 13", include_auctions=True)
        service._auction_turbo[watch_id] = 45  # armed by an earlier check

        async def boom(*args, **kwargs):
            raise RuntimeError("search failed")

        service.ebay.search_market = boom
        _check(service, store, watch_id)
        assert watch_id not in service._auction_turbo
    finally:
        store.close()


def test_media_word_is_judged_by_price_level(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    service = _market_service(store, ebay)
    try:
        # A watch for a GAME: half the listings say "Game", and cost the same as
        # the half that don't. They are the product.
        ebay.market_items = [
            _market_item(f"a{i}", 34 + i, "Mario Kart 8 Deluxe (Nintendo Switch, 2017)")
            for i in range(5)
        ] + [
            _market_item(f"b{i}", 35 + i, "Mario Kart 8 Deluxe - Nintendo Switch Game - Sealed")
            for i in range(5)
        ]
        game = store.add_market_watch("mario kart 8 deluxe")
        _check(service, store, game)
        assert store.get_market_watch(game)["comparable_size"] == 10

        # A watch for a CONSOLE: the games cost a fraction of it. They stay out.
        ebay.market_items = [
            _market_item(f"c{i}", 250 + i, "Nintendo Switch Console 32GB Neon") for i in range(8)
        ] + [
            _market_item(f"g{i}", 35 + i, "Zelda Breath of the Wild Nintendo Switch Game")
            for i in range(6)
        ]
        console = store.add_market_watch("nintendo switch")
        _check(service, store, console)
        assert store.get_market_watch(console)["comparable_size"] == 8

        # A watch where EVERY listing says "Book": the product is a book.
        ebay.market_items = [
            _market_item(f"k{i}", 9 + i * 0.2, "Atomic Habits by James Clear Paperback Book")
            for i in range(6)
        ]
        book = store.add_market_watch("atomic habits")
        _check(service, store, book)
        assert store.get_market_watch(book)["comparable_size"] == 6
    finally:
        store.close()


def test_no_alerts_against_a_quote_below_the_confidence_floor(tmp_path: Path) -> None:
    # Six listings scattered over a 4x range: something is priced, but with so
    # little confidence that "40% under" means nothing. It is quoted, not traded.
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    title = "Garmin Fenix GPS Watch"
    prices = [150, 240, 330, 450, 520, 610]
    ebay.market_items = [_market_item(f"m{i}", p, title) for i, p in enumerate(prices)]
    service = _market_service(store, ebay)
    service.config.market_confidence_z = 0.0  # isolate the confidence floor
    service.config.market_min_confidence = 15
    try:
        watch_id = store.add_market_watch("garmin fenix")
        assert _check(service, store, watch_id) == 0
        row = store.get_market_watch(watch_id)
        assert row["market_price"] is not None and row["market_confidence"] < 15
        service.config.market_min_confidence = 0  # floor off: the cheap one alerts
        assert _check(service, store, watch_id) >= 1
    finally:
        store.close()


# --- regressions from the adversarial review on categories outside electronics ---


def test_unpinned_sizes_are_priced_separately(tmp_path: Path) -> None:
    # A watch that leaves the size open accepts every size. Priced as one market,
    # every 60ml bottle looks 40% "under" the 100ml ones.
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    ebay.market_items = [
        _market_item(f"a{i}", 78 + i, "Dior Sauvage EDP 100ml Spray") for i in range(7)
    ] + [_market_item(f"b{i}", 52 + i * 0.5, "Dior Sauvage EDP 60ml Spray") for i in range(6)]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("dior sauvage edp")
        assert _check(service, store, watch_id) == 0
        assert store.market_dimensions(watch_id) == ["size"]
    finally:
        store.close()


def test_part_watch_is_not_priced_off_the_whole_product(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    ebay.market_items = [
        _market_item(f"c{i}", 48 + i, "Sony PS5 DualSense Wireless Controller White")
        for i in range(6)
    ] + [
        _market_item(f"k{i}", 380 + i, "Sony PS5 Console Disc Edition with 2 Controllers")
        for i in range(6)
    ]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("ps5 controller")
        _check(service, store, watch_id)
        row = store.get_market_watch(watch_id)
        assert row["comparable_size"] == 6 and 45 < row["market_price"] < 60
    finally:
        store.close()


def test_category_consensus_does_not_purge_the_product_for_its_parts(tmp_path: Path) -> None:
    from dataclasses import replace

    store = Store(tmp_path / "test.sqlite3")
    service = _market_service(store, FakeEbay([]))
    try:
        parts = [
            replace(_market_item(f"p{i}", 60 + i, "Fender Stratocaster Neck"),
                    category_id="P", category_name="Guitar Parts")
            for i in range(10)
        ]
        guitars = [
            replace(_market_item(f"g{i}", 700 + i, "Fender Stratocaster"),
                    category_id="G", category_name="Electric Guitars")
            for i in range(4)
        ]
        # Parts are the majority category, but at a tenth of the price they are
        # not the product: nothing is purged (the price clip deals with them).
        assert len(service._category_consensus(parts + guitars)) == 14
        # A watch that itself names a part IS the cheap category.
        assert len(service._category_consensus(parts + guitars, part_watch=True)) == 10
        # And a dominant category at the product's own price level still purges.
        strays = [replace(g, total_price=65.0, item_price=65.0) for g in guitars]
        assert len(service._category_consensus(parts + strays)) == 10
    finally:
        store.close()


def test_games_never_become_the_market_of_a_console_watch(tmp_path: Path) -> None:
    # A price cap hid every console, so only games came back. Nine *different*
    # games are not "the product"; nothing is priced and nothing alerts.
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    games = ["Minecraft", "Super Smash Bros Ultimate", "Zelda Breath of the Wild",
             "Animal Crossing New Horizons", "Splatoon 3", "Pokemon Scarlet",
             "Metroid Dread", "Luigis Mansion 3", "Kirby Forgotten Land"]
    ebay.market_items = [
        _market_item(f"g{i}", 18 + i * 3, f"{name} Nintendo Switch Game")
        for i, name in enumerate(games)
    ]
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("nintendo switch", max_price=100)
        assert _check(service, store, watch_id) == 0
        assert store.get_market_watch(watch_id)["market_price"] is None
    finally:
        store.close()


def test_search_uses_the_users_own_words(tmp_path: Path) -> None:
    # The matcher's internal normal form ("24-70mm" -> "2470mm", "4/102" ->
    # "4 102") is not something to type into eBay's search box.
    class Recording(FakeEbay):
        queries: list[str] = []

        async def search_market(self, query: str, **kwargs) -> list:
            self.queries.append(query)
            return []

    store = Store(tmp_path / "test.sqlite3")
    ebay = Recording([])
    service = _market_service(store, ebay)
    try:
        watch_id = store.add_market_watch("sony fe 24-70mm f2.8 gm")
        _check(service, store, watch_id)
        assert ebay.queries == ["sony fe 24-70mm f2.8 gm"]
    finally:
        store.close()
