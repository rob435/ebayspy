from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import replace

import httpx

from .models import Listing

log = logging.getLogger(__name__)

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_ITEM_BY_LEGACY_ID_URL = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"


class EbayClient:
    def __init__(
        self,
        app_id: str | None,
        client_secret: str | None,
        global_id: str,
        timeout_seconds: int,
        max_items: int,
        detail_concurrency: int = 5,
    ) -> None:
        self.app_id = app_id
        self.client_secret = client_secret
        self.global_id = global_id
        self.max_items = max_items
        self.detail_concurrency = max(1, detail_concurrency)
        self._oauth_token: str | None = None
        self._oauth_token_expires_at = 0.0
        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "User-Agent": "ebayspy/0.1 (+https://github.com/rob435/ebayspy)",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def seller_listings(self, seller: str) -> list[Listing]:
        listings = await self._search_seller_listings(seller)
        return await self._hydrate_listings(listings)

    async def seller_exists(self, seller: str) -> bool | None:
        try:
            await self._search_seller_listings(seller)
        except Exception as exc:
            log.warning(
                "Could not validate eBay seller %s with the Browse API: %s",
                seller,
                exc.__class__.__name__,
            )
            return None
        return True

    async def item_seller(self, item_id: str) -> str | None:
        try:
            item = await self._get_item_by_legacy_id(item_id)
        except Exception:
            log.debug("Could not fetch eBay item %s", item_id, exc_info=True)
            return None
        username = (item.get("seller") or {}).get("username")
        return str(username) if username else None

    async def _oauth_access_token(self) -> str:
        if self._oauth_token and time.time() < self._oauth_token_expires_at:
            return self._oauth_token
        if not self.app_id or not self.client_secret:
            raise RuntimeError("EBAY_APP_ID and EBAY_CLIENT_SECRET are required for the eBay API")

        credentials = base64.b64encode(f"{self.app_id}:{self.client_secret}".encode()).decode()
        response = await self.client.post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay OAuth request failed ({response.status_code}): {self._error_text(response)}"
            )
        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 7200))
        self._oauth_token = token
        self._oauth_token_expires_at = time.time() + max(60, expires_in - 60)
        return token

    async def _authorized_headers(self) -> dict[str, str]:
        token = await self._oauth_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id(),
        }

    async def _search_seller_listings(self, seller: str) -> list[Listing]:
        headers = await self._authorized_headers()
        # The Browse API rejects a search with no query criterion, so category_ids=0
        # (the root category) is paired with the seller filter to list everything a
        # seller has. buyingOptions keeps auction listings, which are otherwise dropped.
        response = await self.client.get(
            BROWSE_SEARCH_URL,
            headers=headers,
            params={
                "category_ids": "0",
                "filter": f"sellers:{{{seller}}},buyingOptions:{{FIXED_PRICE|AUCTION}}",
                "sort": "newlyListed",
                "limit": min(max(self.max_items, 1), 200),
            },
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay Browse API search failed ({response.status_code}): "
                f"{self._error_text(response)}"
            )
        payload = response.json()
        listings = [
            self._listing_from_browse_item(seller, item)
            for item in payload.get("itemSummaries") or []
        ]
        return [listing for listing in listings if listing.item_id and listing.url]

    async def _hydrate_listings(self, listings: list[Listing]) -> list[Listing]:
        """Fetch per-item detail so quantity (and a fuller description) are populated."""
        if not listings:
            return listings
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def hydrate(listing: Listing) -> Listing:
            async with semaphore:
                try:
                    item = await self._get_item_by_legacy_id(listing.item_id)
                except Exception:
                    log.debug(
                        "Could not fetch eBay item detail for %s", listing.item_id, exc_info=True
                    )
                    return listing
            description = listing.description or self._clean_description(
                str(item.get("shortDescription") or "")
            )
            return replace(
                listing,
                description=description,
                quantity_available=self._availability_quantity(item),
            )

        results = await asyncio.gather(
            *(hydrate(listing) for listing in listings), return_exceptions=True
        )
        return [
            result if isinstance(result, Listing) else original
            for original, result in zip(listings, results)
        ]

    async def _get_item_by_legacy_id(self, item_id: str) -> dict:
        headers = await self._authorized_headers()
        response = await self.client.get(
            BROWSE_ITEM_BY_LEGACY_ID_URL,
            headers=headers,
            params={"legacy_item_id": item_id},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay Browse API item lookup failed ({response.status_code}): "
                f"{self._error_text(response)}"
            )
        return response.json()

    def _listing_from_browse_item(self, seller: str, item: dict) -> Listing:
        price = item.get("price") or {}
        categories = item.get("categories") or []
        seller_info = item.get("seller") or {}
        item_id = (
            str(item.get("legacyItemId") or "")
            or self._extract_item_id(str(item.get("itemWebUrl") or ""))
            or self._legacy_id_from_browse_id(str(item.get("itemId") or ""))
        )
        price_value = str(price.get("value") or "")
        price_currency = str(price.get("currency") or "")
        return Listing(
            item_id=item_id,
            seller=str(seller_info.get("username") or seller),
            title=str(item.get("title") or "Untitled"),
            price=f"{price_value} {price_currency}".strip(),
            url=str(item.get("itemWebUrl") or ""),
            description=self._clean_description(str(item.get("shortDescription") or "")),
            listed_at=item.get("itemCreationDate"),
            image_url=(item.get("image") or {}).get("imageUrl"),
            listing_type=", ".join(item.get("buyingOptions") or []),
            category=str(categories[0].get("categoryName") or "") if categories else "",
            quantity_available=self._availability_quantity(item),
        )

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            errors = response.json().get("errors") or []
        except Exception:
            return response.text[:200]
        messages = [
            str(error.get("message") or error.get("longMessage") or error.get("errorId"))
            for error in errors
        ]
        return "; ".join(message for message in messages if message) or response.text[:200]

    @staticmethod
    def _extract_item_id(url: str) -> str:
        match = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", url)
        if match:
            return match.group(1)
        match = re.search(r"[?&]item=(\d{9,})", url)
        return match.group(1) if match else ""

    @staticmethod
    def _clean_description(description: str) -> str:
        return " ".join(description.split())

    @staticmethod
    def _availability_quantity(item: dict) -> str:
        for availability in item.get("estimatedAvailabilities") or []:
            quantity = availability.get("estimatedAvailableQuantity")
            if quantity is not None:
                return str(quantity)
            threshold = availability.get("availabilityThreshold")
            threshold_type = availability.get("availabilityThresholdType")
            if threshold is not None and threshold_type:
                return f"{str(threshold_type).replace('_', ' ').title()} {threshold}"
        return ""

    @staticmethod
    def _legacy_id_from_browse_id(item_id: str) -> str:
        match = re.search(r"\|(\d{9,})\|", item_id)
        return match.group(1) if match else ""

    def _marketplace_id(self) -> str:
        return self.global_id.upper().replace("-", "_")
