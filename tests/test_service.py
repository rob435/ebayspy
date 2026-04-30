import asyncio
from pathlib import Path
from types import SimpleNamespace

from ebayspy.models import Listing
from ebayspy.service import EbaySpyService
from ebayspy.storage import Store


class FakeEbay:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings

    async def seller_listings(self, seller: str) -> list[Listing]:
        return self.listings


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
