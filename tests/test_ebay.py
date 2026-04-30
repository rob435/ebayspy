import asyncio

from ebayspy.ebay import EbayClient
from ebayspy.models import Listing


def test_extract_item_id_from_listing_path() -> None:
    url = "https://www.ebay.com/itm/Test-Item/123456789012?hash=item"

    assert EbayClient._extract_item_id(url) == "123456789012"


def test_extract_item_id_from_query_param() -> None:
    url = "https://www.ebay.com/p/whatever?item=987654321098&foo=bar"

    assert EbayClient._extract_item_id(url) == "987654321098"


def test_clean_description_collapses_whitespace() -> None:
    assert EbayClient._clean_description("A   nice\n\nitem\tlisted today") == "A nice item listed today"


def test_browse_item_maps_to_listing() -> None:
    client = EbayClient(
        app_id="appid",
        client_secret="secret",
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )
    item = {
        "legacyItemId": "123456789012",
        "seller": {"username": "seller_one"},
        "title": "Example",
        "price": {"value": "10.00", "currency": "USD"},
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "itemCreationDate": "2026-04-30T12:00:00.000Z",
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
        assert listing.quantity_available == "7"
        assert listing.listing_type == "FIXED_PRICE"
        assert listing.category == "Video Games"
    finally:
        asyncio.run(client.close())


def test_legacy_id_from_browse_id() -> None:
    assert EbayClient._legacy_id_from_browse_id("v1|123456789012|0") == "123456789012"


def test_seller_listings_raises_when_api_fails_and_fallback_is_empty() -> None:
    client = EbayClient(
        app_id="appid",
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )

    async def api_error(seller: str) -> list[Listing]:
        raise RuntimeError("rate limited")

    async def empty_fallback(seller: str) -> list[Listing]:
        return []

    client._seller_listings_api = api_error
    client._seller_listings_scrape = empty_fallback
    try:
        try:
            asyncio.run(client.seller_listings("seller_one"))
        except RuntimeError as exc:
            assert "fallback returned no listings" in str(exc)
        else:
            raise AssertionError("expected seller_listings to raise")
    finally:
        asyncio.run(client.close())


def test_seller_listings_uses_non_empty_fallback_after_api_error() -> None:
    client = EbayClient(
        app_id="appid",
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )
    fallback_listing = Listing(
        item_id="123456789012",
        seller="seller_one",
        title="Example",
        price="$10.00",
        url="https://example.test/itm/123456789012",
    )

    async def api_error(seller: str) -> list[Listing]:
        raise RuntimeError("rate limited")

    async def non_empty_fallback(seller: str) -> list[Listing]:
        return [fallback_listing]

    client._seller_listings_api = api_error
    client._seller_listings_scrape = non_empty_fallback
    try:
        assert asyncio.run(client.seller_listings("seller_one")) == [fallback_listing]
    finally:
        asyncio.run(client.close())
