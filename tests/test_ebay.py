import asyncio

from ebayspy.ebay import EbayClient
from ebayspy.models import Listing


def _client() -> EbayClient:
    return EbayClient(
        app_id="appid",
        client_secret="secret",
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )


def test_extract_item_id_from_listing_path() -> None:
    url = "https://www.ebay.com/itm/Test-Item/123456789012?hash=item"

    assert EbayClient._extract_item_id(url) == "123456789012"


def test_extract_item_id_from_query_param() -> None:
    url = "https://www.ebay.com/p/whatever?item=987654321098&foo=bar"

    assert EbayClient._extract_item_id(url) == "987654321098"


def test_clean_description_collapses_whitespace() -> None:
    assert EbayClient._clean_description("A   nice\n\nitem\tlisted today") == "A nice item listed today"


def test_legacy_id_from_browse_id() -> None:
    assert EbayClient._legacy_id_from_browse_id("v1|123456789012|0") == "123456789012"


def test_marketplace_id_from_global_id() -> None:
    assert _client()._marketplace_id() == "EBAY_US"


def test_browse_item_maps_to_listing() -> None:
    client = _client()
    item = {
        "legacyItemId": "123456789012",
        "seller": {"username": "seller_one"},
        "title": "Example",
        "price": {"value": "10.00", "currency": "USD"},
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "itemCreationDate": "2026-04-30T12:00:00.000Z",
        "shortDescription": "A   tidy   summary",
        "image": {"imageUrl": "https://example.test/image.jpg"},
        "buyingOptions": ["FIXED_PRICE"],
        "categories": [{"categoryName": "Video Games"}],
        "estimatedAvailabilities": [{"estimatedAvailableQuantity": 7}],
    }
    try:
        listing = client._listing_from_browse_item("seller_one", item)

        assert listing.item_id == "123456789012"
        assert listing.seller == "seller_one"
        assert listing.price == "10.00 USD"
        assert listing.description == "A tidy summary"
        assert listing.quantity_available == "7"
        assert listing.listing_type == "FIXED_PRICE"
        assert listing.category == "Video Games"
    finally:
        asyncio.run(client.close())


def test_browse_item_falls_back_to_search_seller() -> None:
    client = _client()
    item = {
        "legacyItemId": "123456789012",
        "title": "Example",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
    }
    try:
        listing = client._listing_from_browse_item("watched_seller", item)

        assert listing.seller == "watched_seller"
        assert listing.description == ""
        assert listing.quantity_available == ""
    finally:
        asyncio.run(client.close())


def test_seller_listings_searches_then_hydrates() -> None:
    client = _client()
    found = Listing(
        item_id="123456789012",
        seller="seller_one",
        title="Example",
        price="$10.00",
        url="https://example.test/itm/123456789012",
    )

    async def fake_search(seller: str) -> list[Listing]:
        return [found]

    async def fake_detail(item_id: str) -> dict:
        return {
            "shortDescription": "Hydrated   summary",
            "estimatedAvailabilities": [{"estimatedAvailableQuantity": 9}],
        }

    client._search_seller_listings = fake_search
    client._get_item_by_legacy_id = fake_detail
    try:
        listings = asyncio.run(client.seller_listings("seller_one"))

        assert len(listings) == 1
        assert listings[0].quantity_available == "9"
        assert listings[0].description == "Hydrated summary"
    finally:
        asyncio.run(client.close())


def test_market_item_includes_shipping_in_total() -> None:
    client = _client()
    summary = {
        "legacyItemId": "123456789012",
        "title": "Dyson Airblade",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "price": {"value": "100.00", "currency": "GBP"},
        "seller": {"username": "seller_one"},
        "condition": "New",
        "buyingOptions": ["FIXED_PRICE"],
        "shippingOptions": [
            {"shippingCost": {"value": "9.99", "currency": "GBP"}},
            {"shippingCost": {"value": "4.99", "currency": "GBP"}},
        ],
    }
    try:
        item = client._market_item_from_summary(summary, "GBP")

        assert item is not None
        assert item.item_price == 100.00
        assert item.shipping_cost == 4.99  # lowest shipping option
        assert item.total_price == 104.99
        assert item.condition == "New"
    finally:
        asyncio.run(client.close())


def test_market_item_unknown_shipping_is_zero() -> None:
    client = _client()
    summary = {
        "legacyItemId": "123456789012",
        "title": "No shipping data",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "price": {"value": "50.00", "currency": "GBP"},
    }
    try:
        item = client._market_item_from_summary(summary, "GBP")

        assert item is not None
        assert item.shipping_cost is None
        assert item.total_price == 50.00
    finally:
        asyncio.run(client.close())


def test_market_item_skipped_without_price() -> None:
    client = _client()
    summary = {
        "legacyItemId": "123456789012",
        "title": "Broken",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
    }
    try:
        assert client._market_item_from_summary(summary, "GBP") is None
    finally:
        asyncio.run(client.close())


def test_marketplace_currency_maps_gb_to_gbp() -> None:
    client = EbayClient(
        app_id="appid",
        client_secret="secret",
        global_id="EBAY-GB",
        timeout_seconds=1,
        max_items=20,
    )
    try:
        assert client._marketplace_currency() == "GBP"
    finally:
        asyncio.run(client.close())


def test_item_active_returns_true_for_open_listing() -> None:
    client = _client()

    async def fake_detail(item_id: str) -> dict:
        return {"title": "Still listed"}

    client._get_item_by_legacy_id = fake_detail
    try:
        assert asyncio.run(client.item_active("123456789012")) is True
    finally:
        asyncio.run(client.close())


def test_item_active_returns_false_on_not_found() -> None:
    client = _client()

    async def fake_detail(item_id: str) -> dict:
        raise RuntimeError("eBay Browse API item lookup failed (404): not found")

    client._get_item_by_legacy_id = fake_detail
    try:
        assert asyncio.run(client.item_active("123456789012")) is False
    finally:
        asyncio.run(client.close())


def test_item_ended_state_handles_naive_end_date() -> None:
    # A timezone-naive itemEndDate must not raise TypeError (which would crash the
    # poll loop) when compared against the aware "now".
    client = _client()

    async def fake_detail(item_id: str) -> dict:
        return {"title": "x", "itemEndDate": "2020-01-01T00:00:00"}  # past, no Z

    client._get_item_by_legacy_id = fake_detail
    try:
        active, end_date = asyncio.run(client.item_ended_state("123456789012"))
        assert active is False  # the naive past date is read as ended, not a crash
        assert end_date == "2020-01-01T00:00:00"
    finally:
        asyncio.run(client.close())


def test_hydration_failure_keeps_search_listing() -> None:
    client = _client()
    found = Listing(
        item_id="123456789012",
        seller="seller_one",
        title="Example",
        price="$10.00",
        url="https://example.test/itm/123456789012",
    )

    async def fake_search(seller: str) -> list[Listing]:
        return [found]

    async def failing_detail(item_id: str) -> dict:
        raise RuntimeError("eBay item lookup failed")

    client._search_seller_listings = fake_search
    client._get_item_by_legacy_id = failing_detail
    try:
        assert asyncio.run(client.seller_listings("seller_one")) == [found]
    finally:
        asyncio.run(client.close())


# --- hydration cache and transport resilience ---------------------------------

import httpx  # noqa: E402

from ebayspy.ebay import EbayRateLimited  # noqa: E402
from ebayspy.models import MarketItem  # noqa: E402


def _market(item_id: str) -> MarketItem:
    return MarketItem(
        item_id=item_id, title="Thing", url=f"https://example.test/itm/{item_id}",
        seller="s", currency="USD", item_price=10, total_price=10,
    )


def test_market_hydration_is_cached_and_rolls_through_the_sample() -> None:
    client = _client()
    calls: list[str] = []

    async def fake_detail(item_id: str) -> dict:
        calls.append(item_id)
        return {"epid": "E1", "localizedAspects": [{"name": "Storage Capacity", "value": "256 GB"}]}

    client._get_item_by_legacy_id = fake_detail
    items = [_market(str(i)) for i in range(5)]
    try:
        first = asyncio.run(client.hydrate_market_items(items, limit=2))
        assert calls == ["0", "1"]  # only the per-check budget
        assert [bool(i.aspects) for i in first] == [True, True, False, False, False]
        # Next check: the first two are free, so the budget moves on to new ones.
        second = asyncio.run(client.hydrate_market_items(items, limit=2))
        assert calls == ["0", "1", "2", "3"]
        assert [bool(i.aspects) for i in second] == [True, True, True, True, False]
        asyncio.run(client.hydrate_market_items(items, limit=2))
        asyncio.run(client.hydrate_market_items(items, limit=2))
        assert calls == ["0", "1", "2", "3", "4"]  # fully covered: no further calls
        assert second[0].aspects == {"Storage Capacity": "256 GB"} and second[0].epid == "E1"
    finally:
        asyncio.run(client.close())


def test_failed_hydration_is_not_retried_every_check() -> None:
    client = _client()
    calls: list[str] = []

    async def broken_detail(item_id: str) -> dict:
        calls.append(item_id)
        raise RuntimeError("eBay Browse API item lookup failed (400)")

    client._get_item_by_legacy_id = broken_detail
    try:
        for _ in range(3):
            out = asyncio.run(client.hydrate_market_items([_market("1")], limit=5))
        assert calls == ["1"] and out[0].aspects == {}
    finally:
        asyncio.run(client.close())


def _transport_client(handler) -> EbayClient:
    client = _client()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_revoked_token_is_refreshed_once_on_401() -> None:
    tokens = iter(["stale", "fresh"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 7200})
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"errors": [{"message": "Invalid access token"}]})
        return httpx.Response(200, json={"itemSummaries": []})

    client = _transport_client(handler)
    try:
        assert asyncio.run(client.search_market("thing")) == []
        assert seen == ["Bearer stale", "Bearer fresh"]
    finally:
        asyncio.run(client.close())


def test_rate_limit_trips_a_breaker_instead_of_spending_more_calls() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        hits += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    client = _transport_client(handler)
    try:
        for _ in range(3):
            try:
                asyncio.run(client.search_market("thing"))
            except EbayRateLimited:
                pass
            else:
                raise AssertionError("expected EbayRateLimited")
        assert hits == 1  # the second and third calls never left the process
        # A rate-limited hydration is not remembered as the listing's failure.
        asyncio.run(client.hydrate_market_items([_market("1")], limit=5))
        assert client._detail_cache == {}
        # ...and "unknown", not "ended", is the verdict on an item we could not ask about.
        assert asyncio.run(client.item_ended_state("1")) == (None, None)
    finally:
        asyncio.run(client.close())


def test_transient_5xx_is_retried_once() -> None:
    statuses = iter([502, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        status = next(statuses)
        return httpx.Response(status, json={"itemSummaries": []} if status == 200 else {})

    client = _transport_client(handler)
    try:
        assert asyncio.run(client.search_market("thing")) == []
    finally:
        asyncio.run(client.close())


def test_market_item_carries_condition_id() -> None:
    client = _client()
    summary = {
        "legacyItemId": "123456789012",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "price": {"value": "10.00", "currency": "USD"},
        "condition": "For parts or not working",
        "conditionId": "7000",
    }
    try:
        assert client._market_item_from_summary(summary, "USD").condition_id == "7000"
    finally:
        asyncio.run(client.close())
