import asyncio

from ebayspy.ebay import EbayClient
from ebayspy.insights import summarize_sold
from ebayspy.models import SoldItem


def _client() -> EbayClient:
    return EbayClient(
        app_id="appid", client_secret="secret", global_id="EBAY-GB",
        timeout_seconds=1, max_items=20,
    )


def test_sold_item_parsing() -> None:
    client = _client()
    summary = {
        "itemId": "v1|123|0",
        "title": "Apple iPhone 13 256GB",
        "lastSoldPrice": {"value": "402.00", "currency": "GBP"},
        "lastSoldDate": "2026-06-01T10:00:00.000Z",
        "totalSoldQuantity": 3,
    }
    try:
        item = client._sold_item_from_summary(summary, "GBP")
        assert item is not None
        assert item.total_price == 402.00
        assert item.currency == "GBP"
        assert item.quantity == 3
    finally:
        asyncio.run(client.close())


def test_summarize_sold_velocity() -> None:
    items = [
        SoldItem(item_id=str(i), title="t", total_price=400, currency="GBP", quantity=1)
        for i in range(120)  # ~120 sales in 90d -> ~9/wk -> Hot
    ]
    tag, detail = summarize_sold(items)
    assert "🔥" in tag and "sold/wk" in tag
    assert "Marketplace Insights" in detail


def test_summarize_sold_empty() -> None:
    tag, _ = summarize_sold([])
    assert "no recorded sales" in tag
