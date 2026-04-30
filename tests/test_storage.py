from pathlib import Path

from ebayspy.models import Listing
from ebayspy.storage import (
    Store,
    format_status_rows,
    is_valid_ebay_username,
    is_valid_telegram_username,
    normalize_ebay_username,
    normalize_telegram_username,
)


def test_store_sellers_seen_items_and_status(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.add_seller("seller_one")
        assert not store.add_seller("SELLER_ONE")

        assert store.list_sellers() == ["seller_one"]
        assert not store.is_seen("123")
        assert not store.seller_has_successful_check("seller_one")

        store.mark_seen(
            Listing(
                item_id="123",
                seller="seller_one",
                title="Example",
                price="$10.00",
                url="https://example.test/item/123",
                listing_type="FixedPrice",
                category="Video Games",
                quantity_available="2",
            ),
            notified=True,
        )
        assert store.is_seen("123")
        assert store.seller_has_seen_items("seller_one")

        store.record_check("seller_one", listing_count=3, new_count=1)
        assert store.seller_has_successful_check("seller_one")
        status = format_status_rows(store.status_rows())

        assert "seller_one: ok, 3 active, 1 new, 0 ended" in status
    finally:
        store.close()


def test_successful_check_tracks_empty_seller_baseline(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.add_seller("empty_seller")

        assert not store.seller_has_seen_items("empty_seller")
        assert not store.seller_has_successful_check("empty_seller")

        store.record_check("empty_seller", listing_count=0, new_count=0)

        assert not store.seller_has_seen_items("empty_seller")
        assert store.seller_has_successful_check("empty_seller")
    finally:
        store.close()


def test_remove_seller_is_case_insensitive(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.add_seller("Seller_One")

        assert store.remove_seller("seller_one")
        assert store.list_sellers() == []
    finally:
        store.close()


def test_store_tracks_ended_candidates(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.add_seller("seller_one")
        first = Listing(
            item_id="123",
            seller="seller_one",
            title="Example",
            price="$10.00",
            url="https://example.test/item/123",
            listing_type="Auction",
            category="Collectibles",
            quantity_available="1",
        )
        second = Listing(
            item_id="456",
            seller="seller_one",
            title="Still active",
            price="$12.00",
            url="https://example.test/item/456",
        )

        store.upsert_active_listings([first, second])
        ended = store.ended_candidates("seller_one", {"456"})

        assert [listing.item_id for listing in ended] == ["123"]
        assert ended[0].listing_type == "Auction"
        assert ended[0].category == "Collectibles"
        assert ended[0].quantity_available == "1"

        store.mark_ended_notified("123")

        assert store.ended_candidates("seller_one", {"456"}) == []
    finally:
        store.close()


def test_store_tracks_quantity_increase_candidates(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.upsert_active_listings(
            [
                Listing(
                    item_id="123",
                    seller="seller_one",
                    title="Restocked",
                    price="$10.00",
                    url="https://example.test/item/123",
                    quantity_available="2",
                ),
                Listing(
                    item_id="456",
                    seller="seller_one",
                    title="Lower stock",
                    price="$12.00",
                    url="https://example.test/item/456",
                    quantity_available="8",
                ),
                Listing(
                    item_id="789",
                    seller="seller_one",
                    title="Unknown stock",
                    price="$14.00",
                    url="https://example.test/item/789",
                    quantity_available="More than 10",
                ),
            ]
        )

        increases = store.quantity_increase_candidates(
            [
                Listing(
                    item_id="123",
                    seller="seller_one",
                    title="Restocked",
                    price="$10.00",
                    url="https://example.test/item/123",
                    quantity_available="5",
                ),
                Listing(
                    item_id="456",
                    seller="seller_one",
                    title="Lower stock",
                    price="$12.00",
                    url="https://example.test/item/456",
                    quantity_available="3",
                ),
                Listing(
                    item_id="789",
                    seller="seller_one",
                    title="Unknown stock",
                    price="$14.00",
                    url="https://example.test/item/789",
                    quantity_available="11",
                ),
                Listing(
                    item_id="999",
                    seller="seller_one",
                    title="New item",
                    price="$16.00",
                    url="https://example.test/item/999",
                    quantity_available="4",
                ),
            ]
        )

        assert [(listing.item_id, old, new) for listing, old, new in increases] == [
            ("123", 2, 5)
        ]
    finally:
        store.close()


def test_store_tracks_allowed_usernames(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        assert store.add_allowed_username("@Alice")
        assert not store.add_allowed_username("alice")
        assert store.add_allowed_username("bobby")
        assert store.list_allowed_usernames() == ["alice", "bobby"]

        assert store.remove_allowed_username("@ALICE")
        assert not store.remove_allowed_username("missing")
        assert store.list_allowed_usernames() == ["bobby"]
        assert normalize_telegram_username("@MixedCase") == "mixedcase"
        assert is_valid_telegram_username("valid_user")
        assert not is_valid_telegram_username("bad-name")
    finally:
        store.close()


def test_ebay_username_normalization_and_validation() -> None:
    assert normalize_ebay_username("@seller_one") == "seller_one"
    assert normalize_ebay_username("https://www.ebay.com/usr/Seller-One") == "Seller-One"
    assert normalize_ebay_username("https://www.ebay.com/sch/i.html?_ssn=seller_two") == "seller_two"
    assert is_valid_ebay_username("seller.two-1")
    assert not is_valid_ebay_username("seller two")
    assert not is_valid_ebay_username("https://www.ebay.com/usr/seller")


def test_rename_seller_updates_watchlist_and_items(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        store.add_seller("old_name")
        store.mark_seen(
            Listing(
                item_id="123",
                seller="old_name",
                title="Example",
                price="$10.00",
                url="https://example.test/item/123",
            ),
            notified=True,
        )
        store.upsert_active_listings(
            [
                Listing(
                    item_id="123",
                    seller="old_name",
                    title="Example",
                    price="$10.00",
                    url="https://example.test/item/123",
                )
            ]
        )

        assert store.rename_seller("old_name", "new_name")
        assert store.list_sellers() == ["new_name"]
        assert store.recent_active_item_ids("new_name") == ["123"]
        assert not store.rename_seller("new_name", "new_name")
    finally:
        store.close()
