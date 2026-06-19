from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from .models import Listing, MarketItem, SoldItem

log = logging.getLogger(__name__)

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_ITEM_BY_LEGACY_ID_URL = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
INSIGHTS_SALES_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)

BASE_SCOPE = "https://api.ebay.com/oauth/api_scope"
INSIGHTS_SCOPE = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"
)

MARKETPLACE_CURRENCY = {
    "EBAY_US": "USD",
    "EBAY_GB": "GBP",
    "EBAY_DE": "EUR",
    "EBAY_FR": "EUR",
    "EBAY_IT": "EUR",
    "EBAY_ES": "EUR",
    "EBAY_IE": "EUR",
    "EBAY_NL": "EUR",
    "EBAY_AT": "EUR",
    "EBAY_BE": "EUR",
    "EBAY_AU": "AUD",
    "EBAY_CA": "CAD",
    "EBAY_CH": "CHF",
    "EBAY_PL": "PLN",
}

# eBay condition IDs for the optional per-watch condition filter.
CONDITION_IDS = {"new": "1000", "used": "3000"}


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
        self._oauth_tokens: dict[str, tuple[str, float]] = {}
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

    async def seller_listings(self, seller: str, *, hydrate: bool = True) -> list[Listing]:
        listings = await self._search_seller_listings(seller)
        if not hydrate:
            return listings
        return await self._hydrate_listings(listings)

    async def hydrate_listings(self, listings: list[Listing]) -> list[Listing]:
        """Fetch per-item detail for a specific subset of listings (e.g. only new ones)."""
        return await self._hydrate_listings(listings)

    async def hydrate_market_items(
        self, items: list[MarketItem], limit: int
    ) -> list[MarketItem]:
        """Enrich up to ``limit`` items with structured catalog data (ePID, GTIN,
        MPN, and localizedAspects) via getItem — concurrent and bounded so the
        extra calls stay affordable. Items beyond the cap are returned unchanged.
        """
        if limit <= 0 or not items:
            return items
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def enrich(item: MarketItem) -> MarketItem:
            async with semaphore:
                try:
                    detail = await self._get_item_by_legacy_id(item.item_id)
                except Exception:
                    log.debug("Could not hydrate market item %s", item.item_id, exc_info=True)
                    return item
            aspects = {
                str(aspect.get("name")): str(aspect.get("value"))
                for aspect in detail.get("localizedAspects") or []
                if aspect.get("name") and aspect.get("value") is not None
            }
            return replace(
                item,
                epid=str(detail.get("epid") or ""),
                gtin=str(detail.get("gtin") or ""),
                mpn=str(detail.get("mpn") or ""),
                aspects=aspects,
            )

        head = await asyncio.gather(*(enrich(item) for item in items[:limit]))
        return [*head, *items[limit:]]

    async def search_market(
        self,
        query: str,
        *,
        condition: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        limit: int = 200,
        sort: str | None = None,
        include_auctions: bool = False,
        category_ids: str | None = None,
        marketplace: str | None = None,
    ) -> list[MarketItem]:
        """Sample live listings matching a keyword query.

        Defaults to eBay Best Match (``sort=None``), which ranks the actual
        product highly and returns a representative spread of comparable prices
        — the right sample for a median. (A cheapest-first ``sort='price'`` pass
        is biased low and, when accessories are cheaper than the item, can miss
        the product entirely; callers opt into it only as a supplementary
        deal-scan.) ``include_auctions`` adds auction listings (priced off their
        current bid); ``category_ids`` and ``marketplace`` narrow/redirect the
        search.
        """
        headers = await self._authorized_headers(marketplace)
        currency = self._marketplace_currency(marketplace)
        options = "FIXED_PRICE|AUCTION" if include_auctions else "FIXED_PRICE"
        filters = [f"buyingOptions:{{{options}}}"]
        condition_ids = CONDITION_IDS.get((condition or "").strip().lower())
        if condition_ids:
            filters.append(f"conditionIds:{{{condition_ids}}}")
        if min_price is not None or max_price is not None:
            low = min_price if min_price is not None else 0.0
            high = max_price if max_price is not None else 999999.0
            filters.append(f"price:[{low:.2f}..{high:.2f}]")
            filters.append(f"priceCurrency:{currency}")
        params = {
            "q": query,
            "filter": ",".join(filters),
            "limit": min(max(limit, 1), 200),
        }
        if category_ids:
            params["category_ids"] = category_ids
        if sort:
            params["sort"] = sort
        response = await self.client.get(BROWSE_SEARCH_URL, headers=headers, params=params)
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay Browse API market search failed ({response.status_code}): "
                f"{self._error_text(response)}"
            )
        payload = response.json()
        items = [
            self._market_item_from_summary(summary, currency)
            for summary in payload.get("itemSummaries") or []
        ]
        return [item for item in items if item is not None]

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

    async def item_active(self, item_id: str) -> bool | None:
        active, _ = await self.item_ended_state(item_id)
        return active

    async def item_ended_state(self, item_id: str) -> tuple[bool | None, str | None]:
        # The Browse API seller-filtered search can transiently omit items
        # that are still listed; verify before declaring an item ended.
        # Returns (active, end_date): active True = still listed, False = ended,
        # None = unknown; end_date is the eBay itemEndDate when the item has ended.
        try:
            item = await self._get_item_by_legacy_id(item_id)
        except Exception as exc:
            message = str(exc).lower()
            if "404" in message or "not found" in message:
                return False, None
            log.debug("Could not verify active state for %s", item_id, exc_info=True)
            return None, None
        end_date = item.get("itemEndDate")
        if not end_date:
            return True, None
        try:
            parsed = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        except ValueError:
            return True, None
        # eBay normally sends a Z-suffixed (aware) value, but coerce a naive one to
        # UTC so the comparison never raises TypeError and crashes the poll loop.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed > datetime.now(timezone.utc):
            return True, None
        return False, str(end_date)

    async def _oauth_access_token(self, scope: str = BASE_SCOPE) -> str:
        cached = self._oauth_tokens.get(scope)
        if cached and time.time() < cached[1]:
            return cached[0]
        if not self.app_id or not self.client_secret:
            raise RuntimeError("EBAY_APP_ID and EBAY_CLIENT_SECRET are required for the eBay API")

        credentials = base64.b64encode(f"{self.app_id}:{self.client_secret}".encode()).decode()
        response = await self.client.post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": scope},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay OAuth request failed ({response.status_code}): {self._error_text(response)}"
            )
        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 7200))
        self._oauth_tokens[scope] = (token, time.time() + max(60, expires_in - 60))
        return token

    async def _authorized_headers(
        self, marketplace: str | None = None, scope: str = BASE_SCOPE
    ) -> dict[str, str]:
        token = await self._oauth_access_token(scope)
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": (marketplace or self._marketplace_id()),
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
            seller_feedback_percent=str(seller_info.get("feedbackPercentage") or ""),
            seller_feedback_score=str(seller_info.get("feedbackScore") or ""),
        )

    async def search_item_sales(
        self,
        query: str,
        *,
        condition: str | None = None,
        category_ids: str | None = None,
        marketplace: str | None = None,
        limit: int = 200,
    ) -> list[SoldItem]:
        """Real completed-sale history from the Marketplace Insights API.

        Requires the (Limited Release) marketplace-insights OAuth scope; if the
        app is not entitled the OAuth/search call raises and the caller falls
        back to the live-listing estimate.
        """
        headers = await self._authorized_headers(marketplace, scope=INSIGHTS_SCOPE)
        currency = self._marketplace_currency(marketplace)
        filters = []
        condition_ids = CONDITION_IDS.get((condition or "").strip().lower())
        if condition_ids:
            filters.append(f"conditionIds:{{{condition_ids}}}")
        params = {"q": query, "limit": min(max(limit, 1), 200)}
        if filters:
            params["filter"] = ",".join(filters)
        if category_ids:
            params["category_ids"] = category_ids
        response = await self.client.get(INSIGHTS_SALES_URL, headers=headers, params=params)
        if response.status_code != 200:
            raise RuntimeError(
                f"eBay Marketplace Insights search failed ({response.status_code}): "
                f"{self._error_text(response)}"
            )
        payload = response.json()
        sales = [
            self._sold_item_from_summary(summary, currency)
            for summary in payload.get("itemSales") or []
        ]
        return [sale for sale in sales if sale is not None]

    @staticmethod
    def _sold_item_from_summary(summary: dict, currency: str) -> SoldItem | None:
        price = summary.get("lastSoldPrice") or {}
        value = _to_float(price.get("value"))
        if value is None:
            return None
        return SoldItem(
            item_id=str(summary.get("itemId") or summary.get("legacyItemId") or ""),
            title=str(summary.get("title") or "Untitled"),
            total_price=value,
            currency=str(price.get("currency") or currency),
            sold_date=summary.get("lastSoldDate"),
            quantity=_to_int(summary.get("totalSoldQuantity")) or 1,
        )

    def _market_item_from_summary(self, summary: dict, currency: str) -> MarketItem | None:
        item_id = (
            str(summary.get("legacyItemId") or "")
            or self._legacy_id_from_browse_id(str(summary.get("itemId") or ""))
            or self._extract_item_id(str(summary.get("itemWebUrl") or ""))
        )
        url = str(summary.get("itemWebUrl") or "")
        if not item_id or not url:
            return None
        price = summary.get("price") or {}
        bid = summary.get("currentBidPrice") or {}
        item_price = _to_float(price.get("value"))
        current_bid = _to_float(bid.get("value"))
        buying_options = tuple(summary.get("buyingOptions") or ())
        is_auction = "AUCTION" in buying_options
        # Auctions are priced off the live bid; fixed listings off the asking price.
        effective = current_bid if (is_auction and current_bid is not None) else item_price
        if effective is None:
            return None
        shipping_cost = self._lowest_shipping(summary)
        seller_info = summary.get("seller") or {}
        categories = summary.get("categories") or []
        category = categories[0] if categories else {}
        return MarketItem(
            item_id=item_id,
            title=str(summary.get("title") or "Untitled"),
            url=url,
            seller=str(seller_info.get("username") or ""),
            currency=str(price.get("currency") or bid.get("currency") or currency),
            item_price=item_price if item_price is not None else effective,
            # Unknown shipping is treated as 0 (best case); most BIN listings
            # expose a shipping cost or ship free.
            total_price=effective + (shipping_cost or 0.0),
            shipping_cost=shipping_cost,
            condition=str(summary.get("condition") or ""),
            buying_options=buying_options,
            image_url=(summary.get("image") or {}).get("imageUrl"),
            listed_at=summary.get("itemCreationDate"),
            seller_feedback_percent=_to_float(seller_info.get("feedbackPercentage")),
            seller_feedback_score=_to_int(seller_info.get("feedbackScore")),
            category_id=str(category.get("categoryId") or ""),
            category_name=str(category.get("categoryName") or ""),
            current_bid=current_bid,
            bid_count=_to_int(summary.get("bidCount")),
            end_date=summary.get("itemEndDate"),
            item_location=str((summary.get("itemLocation") or {}).get("country") or ""),
        )

    @staticmethod
    def _lowest_shipping(summary: dict) -> float | None:
        costs = []
        for option in summary.get("shippingOptions") or []:
            value = (option.get("shippingCost") or {}).get("value")
            if value is None:
                continue
            try:
                costs.append(float(value))
            except (TypeError, ValueError):
                continue
        return min(costs) if costs else None

    def _marketplace_currency(self, marketplace: str | None = None) -> str:
        return MARKETPLACE_CURRENCY.get(marketplace or self._marketplace_id(), "USD")

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
