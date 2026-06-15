from pathlib import Path

from ebayspy.models import Listing
from ebayspy.storage import (
    Store,
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

        assert [listing.item_id for listing, _ in ended] == ["123"]
        assert ended[0][0].listing_type == "Auction"
        assert ended[0][0].category == "Collectibles"
        assert ended[0][0].quantity_available == "1"
        assert ended[0][1] is not None  # last_seen_at fallback for the ended date

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


def test_observed_sellers_crud_and_interval(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        assert store.add_observed_seller("Seller_One", interval_seconds=120)
        assert not store.add_observed_seller("seller_one")  # case-insensitive duplicate
        assert store.has_observed_seller("SELLER_ONE")

        rows = store.list_observed_sellers()
        assert [row["username"] for row in rows] == ["Seller_One"]
        assert rows[0]["interval_seconds"] == 120
        assert not store.observed_seller_has_successful_check("seller_one")

        assert store.set_observed_interval("seller_one", 300)
        assert store.list_observed_sellers()[0]["interval_seconds"] == 300
        assert not store.set_observed_interval("missing", 300)

        store.record_observe_check("seller_one", new_count=2)
        row = store.list_observed_sellers()[0]
        assert row["last_new_count"] == 2
        assert row["last_error"] is None
        assert store.observed_seller_has_successful_check("seller_one")

        store.record_observe_check("seller_one", new_count=0, error="boom")
        assert store.list_observed_sellers()[0]["last_error"] == "boom"
        # A prior successful check is preserved after a later error.
        assert store.observed_seller_has_successful_check("seller_one")

        assert store.remove_observed_seller("seller_one")
        assert store.list_observed_sellers() == []
    finally:
        store.close()


def test_market_watch_crud_and_dedupe(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch(
            "  Dyson   Airblade HU02 ", condition="new", discount_percent=20, max_price=600
        )
        assert isinstance(watch_id, int)

        # Normalized + case-insensitive dedupe on query + condition.
        assert store.has_market_watch("dyson airblade hu02", condition="new")
        assert store.add_market_watch("Dyson Airblade HU02", condition="new") is None
        # A different condition is a distinct watch.
        other = store.add_market_watch("Dyson Airblade HU02", condition="used")
        assert isinstance(other, int) and other != watch_id

        row = store.get_market_watch(watch_id)
        assert row["query"] == "Dyson Airblade HU02"
        assert row["condition"] == "new"
        assert row["discount_percent"] == 20
        assert row["max_price"] == 600

        assert len(store.list_market_watches()) == 2
        assert store.remove_market_watch(watch_id) is True
        assert store.get_market_watch(watch_id) is None
        assert store.has_market_watch("Dyson Airblade HU02", condition="new") is False
    finally:
        store.close()


def test_market_price_and_check_recording(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("iphone 13")
        assert store.market_watch_has_successful_check(watch_id) is False

        store.update_market_price(watch_id, 412.5, sample_size=37, comparable_size=28)
        store.record_market_check(watch_id, deal_count=2)

        row = store.get_market_watch(watch_id)
        assert row["market_price"] == 412.5
        assert row["sample_size"] == 37
        assert row["comparable_size"] == 28
        assert row["last_deal_count"] == 2
        assert store.market_watch_has_successful_check(watch_id) is True

        # An errored check does not count as a successful one.
        store.record_market_check(watch_id, deal_count=0, error="boom")
        assert store.get_market_watch(watch_id)["last_error"] == "boom"
    finally:
        store.close()


def test_market_deal_alert_dedupe(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("iphone 13")
        assert store.deal_already_alerted(watch_id, "item-1") is False

        store.record_deal_alert(watch_id, "item-1", price=300.0)
        assert store.deal_already_alerted(watch_id, "item-1") is True

        # Removing the watch clears its recorded deal alerts.
        store.remove_market_watch(watch_id)
        assert store.deal_already_alerted(watch_id, "item-1") is False
    finally:
        store.close()


def test_market_deal_alert_stage_progression(tmp_path: Path) -> None:
    # An auction alerts as "deal", then again as "final" when it nears its end.
    # Recording "final" must not let an earlier "deal" re-fire, even though the
    # table keeps a single row per item (e.g. if the snipe window briefly lapses
    # due to clock skew or a relisted item id).
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("iphone 13")

        # First sighting: deal stage fires, final stage still open.
        assert store.deal_already_alerted(watch_id, "auc-1", "deal") is False
        store.record_deal_alert(watch_id, "auc-1", price=300.0, stage="deal")
        assert store.deal_already_alerted(watch_id, "auc-1", "deal") is True
        assert store.deal_already_alerted(watch_id, "auc-1", "final") is False

        # Snipe window: final fires once, then both stages are suppressed.
        store.record_deal_alert(watch_id, "auc-1", price=280.0, stage="final")
        assert store.deal_already_alerted(watch_id, "auc-1", "final") is True
        assert store.deal_already_alerted(watch_id, "auc-1", "deal") is True
    finally:
        store.close()


def test_market_health_alert_fires_once_then_recovers(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("obscure thing")
        for _ in range(3):
            store.record_market_check(watch_id, deal_count=0, error="boom")
        # Below threshold -> no alert yet.
        assert store.check_market_health(watch_id, threshold=5) is None
        for _ in range(2):
            store.record_market_check(watch_id, deal_count=0, error="boom")
        # Crossed threshold -> one alert.
        msg = store.check_market_health(watch_id, threshold=5)
        assert msg is not None and "errored" in msg
        # Does not re-alert while still failing.
        store.record_market_check(watch_id, deal_count=0, error="boom")
        assert store.check_market_health(watch_id, threshold=5) is None
        # Recovers -> counter resets, flag clears.
        store.record_market_check(watch_id, deal_count=1)
        assert store.check_market_health(watch_id, threshold=5) is None
        assert store.get_market_watch(watch_id)["consecutive_errors"] == 0
    finally:
        store.close()


def test_price_trend(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("iphone 13")
        # No history yet -> no trend.
        assert store.price_trend(watch_id, "256gb") is None

        # Backdate a baseline 10 days ago, then a recent lower sample.
        store.conn.execute(
            "insert into market_price_history(watch_id, variant, price, sampled_at) "
            "values (?, ?, ?, datetime('now', '-10 days'))",
            (watch_id, "256gb", 400.0),
        )
        store.record_price_sample(watch_id, "256gb", 360.0)

        pct = store.price_trend(watch_id, "256gb", window_days=7)
        assert pct is not None and round(pct, 1) == -10.0
    finally:
        store.close()


def test_market_lifecycle_and_demand_stats(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        watch_id = store.add_market_watch("iphone 13")
        # First sighting of three listings.
        store.record_market_sightings(
            watch_id,
            [
                ("a", "256gb", 400.0, "GBP", None),
                ("b", "256gb", 410.0, "GBP", None),
                ("c", "256gb", 420.0, "GBP", None),
            ],
        )
        # Next cycle: 'b' cheaper (a price drop), 'c' stays, 'a' not re-seen.
        store.record_market_sightings(
            watch_id,
            [("b", "256gb", 390.0, "GBP", None), ("c", "256gb", 420.0, "GBP", None)],
        )
        # Backdate 'a' so it is older than the grace window, then sweep.
        store.conn.execute(
            "update market_listings set last_seen_at = datetime('now', '-2 days') "
            "where watch_id=? and item_id='a'",
            (watch_id,),
        )
        ended = store.mark_disappeared_listings(watch_id, {"b", "c"}, grace_seconds=86400)
        assert ended == 1  # 'a' disappeared

        stats = store.market_demand_stats(watch_id, window_days=14)
        assert stats["active_count"] == 2  # b, c
        assert stats["ended_in_window"] == 1  # a
        # 'b' had a price cut recorded.
        row = store.conn.execute(
            "select price_drops from market_listings where watch_id=? and item_id='b'", (watch_id,)
        ).fetchone()
        assert row["price_drops"] == 1
    finally:
        store.close()


def test_normalize_market_query() -> None:
    assert normalize_market_query("  Dyson   Airblade\tHU02 ") == "Dyson Airblade HU02"
    assert normalize_market_query(None) == ""


def test_format_market_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        assert "No market watches yet" in format_market_rows([], 600, 15)

        watch_id = store.add_market_watch("iphone 13", condition="used")
        store.update_market_price(watch_id, 412.5, sample_size=40, comparable_size=25)
        text = format_market_rows(store.list_market_watches(), 600, 15)

        assert f"#{watch_id} iphone 13" in text
        assert "used" in text
        assert "-15%" in text  # falls back to the default discount
        assert "market ≈ 412.50 (25 comparable/40)" in text
    finally:
        store.close()


def test_parse_and_format_interval() -> None:
    assert parse_interval("90") == 90
    assert parse_interval("90s") == 90
    assert parse_interval("3m") == 180
    assert parse_interval("1h") == 3600
    assert parse_interval("  2 min ") == 120
    assert parse_interval("") is None
    assert parse_interval("soon") is None

    assert format_interval(45) == "45s"
    assert format_interval(180) == "3m"
    assert format_interval(3600) == "1h"


def test_format_observed_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.sqlite3")
    try:
        assert "No observed sellers yet" in format_observed_rows([], 180)

        store.add_observed_seller("seller_one")  # uses default interval
        store.add_observed_seller("seller_two", interval_seconds=60)
        store.record_observe_check("seller_one", new_count=1)
        text = format_observed_rows(store.list_observed_sellers(), 180)

        assert "seller_one: every 3m, 1 new last check" in text
        assert "seller_two: every 1m" in text
    finally:
        store.close()
