import asyncio
from pathlib import Path
from types import SimpleNamespace

from ebayspy.models import Listing
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

    async def seller_listings(self, seller: str, *, hydrate: bool = True) -> list[Listing]:
        return self.listings

    async def hydrate_listings(self, listings: list[Listing]) -> list[Listing]:
        self.hydrated.extend(listing.item_id for listing in listings)
        return listings

    async def seller_exists(self, seller: str) -> bool | None:
        return self.seller_exists_response

    async def item_active(self, item_id: str) -> bool | None:
        return self.item_active_response


class FakeTelegram:
    def __init__(self) -> None:
        self.quantity_increases: list[tuple[str, str, int, int]] = []
        self.listings: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str]] = []

    async def notify_listing(self, chat_id: str, listing: Listing) -> None:
        self.listings.append((chat_id, listing.item_id))

    async def notify_ended_listing(self, chat_id: str, listing: Listing) -> None:
        self.ended.append((chat_id, listing.item_id))

    async def notify_quantity_increase(
        self, chat_id: str, listing: Listing, previous_quantity: int, current_quantity: int
    ) -> None:
        self.quantity_increases.append(
            (chat_id, listing.item_id, previous_quantity, current_quantity)
        )


def _service_config() -> SimpleNamespace:
    return SimpleNamespace(
        seed_sellers=(),
        telegram_chat_id="chat-1",
        telegram_allowed_chat_ids=(),
        telegram_allowed_usernames=(),
        notify_existing_on_first_run=False,
        seller_check_delay_seconds=0,
        max_items_per_seller=20,
        observe_interval_seconds=180,
        observe_min_interval_seconds=30,
        observe_sellers=(),
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
