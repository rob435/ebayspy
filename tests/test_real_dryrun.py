"""End-to-end dry runs over real eBay listings.

Each product search in the labelled real-listing files is replayed through the
production pipeline (matcher, variant split, fair value, deal selection), and
the listings it would alert are checked against the human labels: an alert for
something that is not the product is the failure a user actually sees.

The files carry title, price and new/used only (see tests/data/README.md), so
this exercises matching and price stratification, not sellers, categories or
lifecycle evidence.
"""

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from ebayspy.config import Config
from ebayspy.dryrun import dry_run

DATA = Path(__file__).parent / "data"


def _config() -> Config:
    return replace(
        Config.load(),
        ebay_global_id="EBAY-US",
        market_semantic=False,
        market_vision=False,
        market_hydrate=False,
        market_deal_scan=False,
        market_price_source="listings",
        market_discount_percent=15,
        market_min_sample=5,
        market_min_deal_ratio=0.4,
        market_min_dispersion=0.12,
        market_confidence_z=1.0,
        market_offer_aware=False,
        market_category_consensus=0.7,
        market_risk_max=100,
        market_aliases=(),
    )


def _replay(name: str) -> tuple[Counter, Counter, list[str]]:
    by_query: dict[str, list[dict]] = defaultdict(list)
    for row in json.loads((DATA / name).read_text()):
        by_query[row["query"]].append(row)
    config = _config()
    deals: Counter = Counter()
    matching: Counter = Counter()
    wrong: list[str] = []
    for query, rows in by_query.items():
        label = {row["item_id"]: row["match"] for row in rows}
        used = lambda row: row["condition"] == "Used"  # noqa: E731
        recording = {
            "query": query,
            "marketplace": "EBAY_US",
            "total": len(rows),
            "itemSummaries": [
                {
                    "legacyItemId": row["item_id"],
                    "title": row["title"],
                    "itemWebUrl": f"https://www.ebay.com/itm/{row['item_id']}",
                    "price": {"value": f"{row['price_usd']:.2f}", "currency": "USD"},
                    "buyingOptions": ["FIXED_PRICE"],
                    "condition": row["condition"],
                    "conditionId": "3000" if used(row) else "1000",
                }
                for row in rows
            ],
        }
        report = asyncio.run(dry_run(config, query, recording=recording))
        for listing in report["listings"]:
            truth = label[listing["item_id"]]
            if truth in (True, False):
                kept = listing["status"] != "rejected"
                matching[("t" if kept == truth else "f") + ("p" if kept else "n")] += 1
        for deal in report["deals"]:
            truth = label[deal["item_id"]]
            deals[str(truth)] += 1
            if truth is False:
                wrong.append(f"{query}: ${deal['total']:.2f} {deal['title']}")
    return deals, matching, wrong


@pytest.mark.parametrize(
    ("name", "min_deal_precision", "min_match_precision", "min_match_recall"),
    [
        # measured: 162 genuine / 5 wrong alerts (0.970); matching P 0.966 R 0.994
        ("real_listings.json", 0.95, 0.95, 0.97),
        # measured (held out): 92 genuine / 4 wrong alerts (0.958); matching P 0.963 R 0.972
        ("real_listings_holdout.json", 0.93, 0.94, 0.95),
    ],
)
def test_alerts_on_real_searches_are_the_product(
    name: str, min_deal_precision: float, min_match_precision: float, min_match_recall: float
) -> None:
    deals, matching, wrong = _replay(name)
    genuine, not_product = deals["True"], deals["False"]
    deal_precision = genuine / max(1, genuine + not_product)
    precision = matching["tp"] / max(1, matching["tp"] + matching["fp"])
    recall = matching["tp"] / max(1, matching["tp"] + matching["fn"])
    summary = (
        f"{name}: {genuine} genuine and {not_product} wrong alerts "
        f"(precision {deal_precision:.3f}); pipeline matching P={precision:.3f} "
        f"R={recall:.3f}\nwrong alerts:\n" + "\n".join(wrong)
    )
    assert genuine >= 50, summary  # a pipeline that alerts nothing is not "precise"
    assert deal_precision >= min_deal_precision, summary
    assert precision >= min_match_precision and recall >= min_match_recall, summary
