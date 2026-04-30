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


def test_parse_seller_search_html_maps_cards_to_listings() -> None:
    client = EbayClient(
        app_id=None,
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )
    html = """
    <ul>
      <li class="s-item">
        <div class="s-item__title">Example Item</div>
        <a class="s-item__link" href="https://www.ebay.com/itm/Example/123456789012"></a>
        <span class="s-item__price">$10.00</span>
        <span class="s-item__purchase-options">Buy It Now</span>
        <div>3 available</div>
        <div class="s-item__image"><img src="https://example.test/image.jpg"></div>
      </li>
      <li class="s-item">
        <div class="s-item__title">Shop on eBay</div>
        <a class="s-item__link" href="https://www.ebay.com/"></a>
      </li>
    </ul>
    """
    try:
        listings = client._parse_seller_search_html("seller_one", html)

        assert listings == [
            Listing(
                item_id="123456789012",
                seller="seller_one",
                title="Example Item",
                price="$10.00",
                url="https://www.ebay.com/itm/Example/123456789012",
                image_url="https://example.test/image.jpg",
                listing_type="Buy It Now",
                quantity_available="3",
            )
        ]
    finally:
        asyncio.run(client.close())


def test_parse_seller_search_html_reports_block_page() -> None:
    client = EbayClient(
        app_id=None,
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )
    try:
        try:
            client._parse_seller_search_html("seller_one", "<h1>Access Denied</h1>")
        except RuntimeError as exc:
            assert "blocked" in str(exc)
        else:
            raise AssertionError("expected block page to raise")
    finally:
        asyncio.run(client.close())


def test_seller_listings_uses_browser_scraper_only() -> None:
    client = EbayClient(
        app_id="appid",
        client_secret="secret",
        global_id="EBAY-US",
        timeout_seconds=1,
        max_items=20,
    )
    browser_listing = Listing(
        item_id="123456789012",
        seller="seller_one",
        title="Example",
        price="$10.00",
        url="https://example.test/itm/123456789012",
    )

    async def browser_scrape(seller: str) -> list[Listing]:
        return [browser_listing]

    async def api_error(seller: str) -> list[Listing]:
        raise AssertionError("API should not be called")

    client._seller_listings_browser = browser_scrape
    client._seller_listings_browse = api_error
    client._seller_listings_api = api_error
    client._seller_listings_scrape = api_error
    try:
        assert asyncio.run(client.seller_listings("seller_one")) == [browser_listing]
    finally:
        asyncio.run(client.close())
