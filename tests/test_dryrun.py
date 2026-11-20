import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from ebayspy.config import Config
from ebayspy.dryrun import dry_run, format_report
from ebayspy.ebay import EbayClient


def _config() -> Config:
    # Pin everything a developer's own .env could change under the test.
    return replace(
        Config.load(),
        ebay_global_id="EBAY-GB",
        market_semantic=False,
        market_vision=False,
        market_hydrate=False,
        market_deal_scan=False,
        market_price_source="listings",
        market_discount_percent=15,
        market_min_sample=5,
        market_confidence_z=1.0,
        market_offer_aware=False,
        market_category_consensus=0.7,
        market_resale_shipping=0.0,
        market_risk_max=100,
        market_aliases=(),
    )


def _summary(item_id: str, title: str, price: float, **extra) -> dict:
    return {
        "itemId": f"v1|{item_id}|0",
        "legacyItemId": item_id,
        "title": title,
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
        "price": {"value": f"{price:.2f}", "currency": "GBP"},
        "buyingOptions": ["FIXED_PRICE"],
        "condition": "Used",
        "conditionId": "3000",
        "seller": {"username": f"seller{item_id[-3:]}", "feedbackPercentage": "99.5",
                   "feedbackScore": 500},
        **extra,
    }


def _summaries() -> list[dict]:
    title = "Dyson Airblade HU02 Hand Dryer"
    rows = [_summary(f"1000000000{i:02d}", title, 198 + i) for i in range(10)]
    rows.append(_summary("100000000090", title, 150))  # ~25% under: a deal
    rows.append(_summary("100000000091", "Dyson Airblade HU02 Wall Bracket", 20))
    rows.append(_summary("100000000092", title, 120, condition="For parts or not working",
                         conditionId="7000"))
    return rows


def test_replayed_dry_run_reports_what_would_have_alerted() -> None:
    recording = {"query": "dyson airblade hu02", "marketplace": "EBAY_GB",
                 "total": 13, "itemSummaries": _summaries()}
    report = asyncio.run(dry_run(_config(), "dyson airblade hu02", recording=recording))

    assert report["source"] == "replay" and report["sampled"] == 13
    assert report["comparable"] == 11 and not report["truncated"]
    assert [deal["item_id"] for deal in report["deals"]] == ["100000000090"]
    deal = report["deals"][0]
    assert deal["stage"] == "deal" and 20 < deal["discount_percent"] < 30 and deal["edge_z"] > 1
    by_id = {listing["item_id"]: listing for listing in report["listings"]}
    assert by_id["100000000091"]["status"] == "rejected"
    assert by_id["100000000092"]["reason"].startswith("condition:")  # eBay says for parts
    (variant,) = report["variants"]
    assert variant["low"] < variant["fair"] < variant["high"]
    assert variant["alert_at"] <= variant["fair"] * 0.85
    assert variant["evidence"] == [{"source": "asks", "price": variant["fair"], "n": 10}]

    text = format_report(report)
    assert "nothing sent, nothing saved" in text
    assert "DEALS that would alert (1)" in text and "REJECTED (2)" in text
    json.dumps(report)  # the --json form must serialise


def test_live_run_can_be_recorded_and_replayed_identically(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        return httpx.Response(200, json={"total": 13, "itemSummaries": _summaries()})

    config = _config()
    client = EbayClient("app", "secret", config.ebay_global_id, 1, 20)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    saved = tmp_path / "search.json"
    live = asyncio.run(dry_run(config, "dyson airblade hu02", record_to=saved, client=client))
    assert live["source"] == "live" and live["recorded_to"] == str(saved)

    replayed = asyncio.run(
        dry_run(config, "dyson airblade hu02", recording=json.loads(saved.read_text()))
    )
    for key in ("sampled", "comparable", "variants", "deals", "listings", "dimensions"):
        assert replayed[key] == live[key], key


def test_dry_run_with_nothing_to_price_says_so() -> None:
    recording = {"query": "nonexistent thing", "marketplace": "EBAY_GB", "itemSummaries": []}
    report = asyncio.run(dry_run(_config(), "nonexistent thing", recording=recording))
    assert report["variants"] == [] and report["deals"] == []
    assert "not enough independent evidence" in format_report(report)
