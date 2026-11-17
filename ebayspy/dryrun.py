"""Dry run: price one market and show the working — nothing sent, nothing saved.

It drives the production pipeline end to end — the same response parser,
matcher, variant clustering, fair-value engine, risk screen and deal selection
the live service runs — against either a live Browse API search or a recorded
one, with a throwaway in-memory store and a Telegram that only records. What it
reports as a deal is exactly what the service would have alerted.

A live run can save the raw search (``record_to``) so it can be replayed later
(``recording``): the same real listings, before and after a matching or pricing
change, with no API calls.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import pricing
from .config import Config
from .ebay import MARKETPLACE_CURRENCY, EbayClient, MarketSample
from .market import deal_threshold, variant_key
from .models import MarketItem
from .service import EbaySpyService
from .storage import Store
from .telegram import format_money

_EVIDENCE_NAMES = {"asks": "asks", "cleared": "ended", "sold": "sold"}


class RecordingTelegram:
    """Stands in for the bot: remembers every deal alert instead of sending it."""

    def __init__(self) -> None:
        self.deals: list[tuple[MarketItem, dict]] = []

    async def notify_deal(self, chat_id: str, item: MarketItem, **details) -> None:
        self.deals.append((item, details))

    async def send_message(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None


class ReplayEbay:
    """Serves a recorded search through the real response parser, so a replay
    exercises the same code path a live search does from the payload onward."""

    def __init__(self, recording: dict, parser: EbayClient) -> None:
        currency = MARKETPLACE_CURRENCY.get(recording.get("marketplace", ""), "USD")
        self.items = [
            item
            for summary in recording.get("itemSummaries") or []
            if (item := parser._market_item_from_summary(summary, currency)) is not None
        ]
        self.total = recording.get("total")
        self.details: dict[str, dict] = recording.get("details") or {}

    async def search_market(
        self, query: str, *, include_auctions: bool = False, sort: str | None = None, **_
    ) -> MarketSample:
        items = [item for item in self.items if include_auctions or not item.is_auction]
        if sort == "price":
            items.sort(key=lambda item: item.total_price)
        sample = MarketSample(items)
        sample.total = self.total
        return sample

    async def hydrate_market_items(self, items: list[MarketItem], limit: int) -> list[MarketItem]:
        return [
            replace(item, **self.details[item.item_id]) if item.item_id in self.details else item
            for item in items
        ]

    async def item_active(self, item_id: str) -> bool | None:
        return None

    async def search_item_sales(self, *args, **kwargs) -> list:
        return []

    async def close(self) -> None:
        return None


def _marketplace(config: Config) -> str:
    return config.ebay_global_id.upper().replace("-", "_")


async def dry_run(
    config: Config,
    query: str,
    *,
    condition: str | None = None,
    discount: int | None = None,
    max_price: float | None = None,
    category: str | None = None,
    exclude: tuple[str, ...] = (),
    auctions: bool = False,
    lots: bool = False,
    recording: dict | None = None,
    record_to: Path | None = None,
    client: EbayClient | None = None,
) -> dict:
    """Run one full market check for ``query`` and return a JSON-able report.

    ``recording`` replays a saved search instead of calling eBay; ``record_to``
    saves a live one. ``client`` substitutes the eBay client (tests).
    """
    # Show every deal the screens let through, not just the first few per cycle.
    config = replace(config, market_max_deals_per_cycle=10**6)
    parser = client or EbayClient(
        config.ebay_app_id,
        config.ebay_client_secret,
        config.ebay_global_id,
        config.http_timeout_seconds,
        config.max_items_per_seller,
        config.detail_concurrency,
    )
    captured: list[dict] = []
    if recording is not None:
        marketplace = recording.get("marketplace") or _marketplace(config)
        ebay = ReplayEbay(recording, parser)
    else:
        if client is None:
            config.require_ebay()
        marketplace = _marketplace(config)
        parser.on_search = lambda _query, payload: captured.append(payload)
        ebay = parser
    telegram = RecordingTelegram()
    service = EbaySpyService(config, store=Store(Path(":memory:")), ebay=ebay, telegram=telegram)
    try:
        watch_id = service.store.add_market_watch(
            query,
            condition=condition,
            discount_percent=discount,
            max_price=max_price,
            category_id=category,
            exclude_terms=", ".join(exclude) or None,
            include_auctions=auctions or None,
            include_lots=lots or None,
        )
        row = service.store.get_market_watch(watch_id)
        snap = await service._sample_market(row, explain=True)
        await service._quote_market(row, snap)
        await service._alert_deals(row, ["dry-run"], snap)
        report = _build_report(service, row, snap, telegram, marketplace)
        report["source"] = "replay" if recording is not None else "live"
        if record_to is not None and recording is None:
            _save_recording(record_to, query, marketplace, captured, parser)
            report["recorded_to"] = str(record_to)
        return report
    finally:
        service.store.close()
        await parser.close()


def _save_recording(
    path: Path, query: str, marketplace: str, payloads: list[dict], client: EbayClient
) -> None:
    summaries: dict[str, dict] = {}
    for payload in payloads:
        for summary in payload.get("itemSummaries") or []:
            summaries.setdefault(str(summary.get("itemId") or len(summaries)), summary)
    recording = {
        "query": query,
        "marketplace": marketplace,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": (payloads[0].get("total") if payloads else None),
        "itemSummaries": list(summaries.values()),
        "details": {
            item_id: fields for item_id, (_, fields) in client._detail_cache.items() if fields
        },
    }
    path.write_text(json.dumps(recording, indent=1))


def _build_report(service, row, snap, telegram: RecordingTelegram, marketplace: str) -> dict:
    config = service.config
    discount = service._watch_discount(row)
    z = config.market_confidence_z
    variants = []
    for label in sorted(snap.quotes, key=lambda key: -len(snap.clusters.get(key, ()))):
        fair = snap.quotes[label]
        variants.append(
            {
                "variant": label,
                "listings": len(snap.clusters.get(label, ())),
                "fair": fair.price,
                "low": fair.low,
                "high": fair.high,
                "confidence": fair.confidence,
                "spread": fair.sigma,
                "alert_at": deal_threshold(fair.price, discount, fair.predictive_sd, z),
                "quick_sale": pricing.quick_sale_price(fair.price, fair.sigma),
                "breakeven": pricing.net_proceeds(
                    fair.price, config.market_resale_fee_percent,
                    config.market_resale_fee_fixed, config.market_resale_shipping,
                ),
                "evidence": [
                    {"source": _EVIDENCE_NAMES.get(part.source, part.source),
                     "price": part.price, "n": part.n}
                    for part in fair.components
                ],
            }
        )
    deals = []
    for rank, (item, details) in enumerate(telegram.deals, start=1):
        fair = details.get("quote")
        paid = details.get("offer_estimate") or item.total_price
        stage = (
            "lot" if details.get("lot_quantity")
            else "offer" if details.get("offer_estimate") is not None
            else "auction" if item.is_auction
            else "deal"
        )
        deals.append(
            {
                "rank": rank,
                "item_id": item.item_id,
                "title": item.title,
                "url": item.url,
                "total": item.total_price,
                "stage": stage,
                "variant": details.get("variant") or "",
                "fair": details["market_price"],
                "discount_percent": details["discount_percent"],
                "edge_z": pricing.edge_z(
                    paid, details["market_price"],
                    fair.predictive_sd if fair is not None else pricing.MIN_SIGMA,
                ),
                "profit": details.get("profit"),
                "quick_profit": details.get("quick_profit"),
                "risk": details.get("risk") or "",
                "condition": item.condition,
                "seller": item.seller,
            }
        )
    comparable_ids = {item.item_id for item in snap.fixed}
    auction_ids = {item.item_id for item in snap.auctions}
    variant_of = {
        item.item_id: variant_key(item, snap.dimensions) for item in snap.comparable
    }
    listings = []
    for item in snap.items:
        if item.item_id in auction_ids:
            status, reason = "auction", ""
        elif item.item_id in comparable_ids:
            unresolved = variant_of.get(item.item_id) is None
            status = "unresolved" if unresolved else "comparable"
            reason = f"does not state its {'/'.join(snap.dimensions)}" if unresolved else ""
        else:
            status, reason = "rejected", snap.rejected.get(item.item_id, "not comparable")
        listings.append(
            {
                "item_id": item.item_id,
                "title": item.title,
                "total": item.total_price,
                "condition": item.condition,
                "seller": item.seller,
                "status": status,
                "variant": variant_of.get(item.item_id) or "",
                "reason": reason,
            }
        )
    return {
        "query": row["query"],
        "marketplace": marketplace,
        "currency": MARKETPLACE_CURRENCY.get(marketplace, "USD"),
        "discount_percent": discount,
        "confidence_z": z,
        "sampled": len(snap.items),
        "total_matches": getattr(snap.items, "total", None),
        "truncated": snap.truncated,
        "comparable": len(snap.fixed),
        "auctions": len(snap.auctions),
        "dimensions": list(snap.dimensions),
        "variants": variants,
        "unpriced": [
            {"variant": label, "listings": len(group)}
            for label, group in snap.clusters.items()
            if label not in snap.quotes
        ],
        "deals": deals,
        "listings": listings,
    }


def format_report(report: dict, *, show: int = 15) -> str:
    """Human-readable rendering of a dry-run report."""
    currency = report["currency"]

    def money(value: float) -> str:
        return format_money(value, currency)

    counts = {"comparable": 0, "rejected": 0, "unresolved": 0, "auction": 0}
    for listing in report["listings"]:
        counts[listing["status"]] += 1
    total = report.get("total_matches")
    lines = [
        f"DRY RUN  “{report['query']}”  ·  {report['marketplace']}  ·  {report['source']}"
        "  ·  nothing sent, nothing saved",
        f"Sampled {report['sampled']}"
        + (f" of {total} matches" if total is not None and total != report["sampled"] else "")
        + f"  →  {report['comparable']} comparable, {counts['rejected']} rejected"
        + (f", {counts['auction']} auctions" if counts["auction"] else "")
        + (f"  ·  split by {' + '.join(report['dimensions'])}" if report["dimensions"] else ""),
        "",
        "FAIR VALUE",
    ]
    if not report["variants"]:
        lines.append("  not enough independent evidence to price this market")
    for variant in report["variants"]:
        evidence = " · ".join(
            f"{part['source']} {money(part['price'])} ×{part['n']}" for part in variant["evidence"]
        )
        lines.append(
            f"  {variant['variant'] or 'all listings':<24} {money(variant['fair']):>10}"
            f"  ({money(variant['low'])}–{money(variant['high'])}, {variant['confidence']}% conf)"
            f"  spread ±{variant['spread'] * 100:.0f}%  [{evidence}]"
        )
        lines.append(
            f"  {'':<24} alerts ≤ {money(variant['alert_at'])} · quick-sale "
            f"{money(variant['quick_sale'])} · break-even buy {money(variant['breakeven'])}"
        )
    if report["unpriced"]:
        lines.append(
            "  too little evidence: "
            + ", ".join(f"{u['variant'] or 'all'} ({u['listings']})" for u in report["unpriced"])
        )
    if counts["unresolved"]:
        lines.append(
            f"  {counts['unresolved']} listing(s) don't state their "
            f"{'/'.join(report['dimensions'])} — left unpriced rather than guessed"
        )
    lines += ["", f"DEALS that would alert ({len(report['deals'])}), strongest first"]
    if not report["deals"]:
        lines.append("  none")
    for deal in report["deals"][:show]:
        flip = (
            f"  flip {money(deal['profit'])}"
            + (f"/{money(deal['quick_profit'])}" if deal["quick_profit"] is not None else "")
            if deal["profit"] is not None
            else ""
        )
        lines.append(
            f"  {deal['rank']:>2}. {money(deal['total']):>10}  -{deal['discount_percent']:.0f}% vs "
            f"{money(deal['fair'])}"
            + (f" [{deal['variant']}]" if deal["variant"] else "")
            + f"  {deal['edge_z']:.1f}σ  {deal['stage']}{flip}"
        )
        lines.append(f"      {deal['title'][:88]}")
        if deal["risk"]:
            lines.append(f"      {deal['risk']}")
    if len(report["deals"]) > show:
        lines.append(f"  … {len(report['deals']) - show} more (use --json for all)")

    reasons: dict[str, list[dict]] = {}
    for listing in report["listings"]:
        if listing["status"] == "rejected":
            reasons.setdefault(listing["reason"].split(" ", 1)[0], []).append(listing)
    lines += ["", f"REJECTED ({counts['rejected']}) by reason"]
    for reason, group in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        example = min(group, key=lambda listing: len(listing["title"]))
        lines.append(
            f"  {len(group):>4} × {reason:<28} e.g. {money(example['total'])}  "
            f"{example['title'][:60]}"
        )
    accepted = [listing for listing in report["listings"] if listing["status"] == "comparable"]
    lines += ["", f"COMPARABLE ({len(accepted)}), cheapest first"]
    for listing in sorted(accepted, key=lambda listing: listing["total"])[:show]:
        lines.append(
            f"  {money(listing['total']):>10}  "
            + (f"[{listing['variant']}] " if listing["variant"] else "")
            + listing["title"][:80]
        )
    if len(accepted) > show:
        lines.append(f"  … {len(accepted) - show} more (use --json for all)")
    return "\n".join(lines)
