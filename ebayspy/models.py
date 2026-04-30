from __future__ import annotations

from dataclasses import dataclass


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
