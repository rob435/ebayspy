from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Listing:
    item_id: str
    seller: str
    title: str
    price: str
    url: str
    description: str = ""
    listed_at: str | None = None
    image_url: str | None = None
    listing_type: str = ""
    category: str = ""
    quantity_available: str = ""
    seller_feedback_percent: str = ""
    seller_feedback_score: str = ""


@dataclass(frozen=True)
class MarketItem:
    """A single live listing sampled while pricing a market watch."""

    item_id: str
    title: str
    url: str
    seller: str
    currency: str
    item_price: float
    total_price: float
    shipping_cost: float | None = None
    condition: str = ""
    buying_options: tuple[str, ...] = ()
    image_url: str | None = None
    listed_at: str | None = None
    seller_feedback_percent: float | None = None
    seller_feedback_score: int | None = None
    category_id: str = ""
    category_name: str = ""
    current_bid: float | None = None
    bid_count: int | None = None
    end_date: str | None = None
    item_location: str = ""
    # Populated only when an item is hydrated via getItem (structured catalog data).
    epid: str = ""
    gtin: str = ""
    mpn: str = ""
    aspects: dict[str, str] = field(default_factory=dict)

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    @property
    def accepts_offers(self) -> bool:
        return "BEST_OFFER" in self.buying_options


@dataclass(frozen=True)
class SoldItem:
    """A completed sale from the Marketplace Insights API (real sold price).

    Shares the ``title``/``total_price``/``aspects`` shape with MarketItem so the
    same variant-clustering and pricing helpers work on it unchanged.
    """

    item_id: str
    title: str
    total_price: float
    currency: str
    sold_date: str | None = None
    quantity: int = 1
    aspects: dict[str, str] = field(default_factory=dict)
    is_auction: bool = False
