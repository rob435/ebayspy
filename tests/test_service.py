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
    ) -> None:
        self.deals.append((chat_id, item.item_id, item.total_price))


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
        market_min_dispersion=0.12,
        market_deal_scan=False,
        market_resale_fee_percent=12.8,
        market_resale_fee_fixed=0.30,
        market_auctions_default=False,
        market_snipe_window_seconds=600,
        market_turbo_interval_seconds=45,
        market_demand_grace_seconds=86400,
        market_demand_window_days=14,
        market_demand_min_events=3,
        market_arbitrage_threshold=20.0,
        market_arbitrage_interval_seconds=3600,
        market_health_threshold=5,
        fx_rates=(),
        ebay_global_id="EBAY-GB",
        market_aliases=(),
        market_watches=(),
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
        seller="seller",
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


def test_check_market_watch_caps_alerts_per_cycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    ebay = FakeEbay([])
    # Ten genuine deals plus enough at-market items to anchor the median at 100.
    title = "Acme Widget X1 Gadget"
    ebay.market_items = [_market_item(f"deal-{i}", 60, title) for i in range(10)]
    ebay.market_items += [_market_item(f"mkt-{i}", 100, title) for i in range(10)]
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


def _auction(item_id: str, bid_total: float, ends_in: int) -> MarketItem:
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
        end_date=end,
    )


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
        owned = store.add_market_watch("iphone 13", owner_chat_id="user-9")
        seeded = store.add_market_watch("ps5")  # no owner

        owned_row = store.get_market_watch(owned)
        seeded_row = store.get_market_watch(seeded)
        broadcast = ["a", "b"]

        # Owned watch -> owner + admins, not the whole broadcast list.
        assert service._chats_for_watch(owned_row, broadcast) == ["user-9", "admin-1"]
        # Ownerless watch -> broadcast to everyone.
        assert service._chats_for_watch(seeded_row, broadcast) == broadcast
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
        assert 360 <= row["market_price"] <= 366  # sold median, not the ~402 live asking
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
