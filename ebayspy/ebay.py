from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urlencode

import httpx
from bs4 import BeautifulSoup

from .models import Listing

log = logging.getLogger(__name__)


class EbayClient:
    def __init__(
        self,
        app_id: str | None,
        global_id: str,
        timeout_seconds: int,
        max_items: int,
        description_concurrency: int = 5,
    ) -> None:
        self.app_id = app_id
        self.global_id = global_id
        self.max_items = max_items
        self.description_concurrency = max(1, description_concurrency)
        self.client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; ebayspy/0.1; "
                    "+https://github.com/local/ebayspy)"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def seller_listings(self, seller: str) -> list[Listing]:
        api_error: Exception | None = None
        if self.app_id:
            try:
                return await self._seller_listings_api(seller)
            except Exception as exc:
                api_error = exc
                log.warning(
                    "eBay API lookup failed for %s with %s; trying search-page fallback",
                    seller,
                    exc.__class__.__name__,
                )
        listings = await self._seller_listings_scrape(seller)
        if api_error is not None and not listings:
            raise RuntimeError(
                "eBay API lookup failed and search-page fallback returned no listings; "
                f"API error: {api_error}"
            ) from api_error
        return listings

    async def seller_exists(self, seller: str) -> bool | None:
        if self.app_id:
            try:
                return True if await self._seller_listings_api(seller) else None
            except Exception as exc:
                log.warning(
                    "Could not validate eBay seller %s with %s",
                    seller,
                    exc.__class__.__name__,
                )
        response = await self.client.get(f"https://www.ebay.com/usr/{quote_plus(seller)}")
        if response.status_code == 404:
            return False
        if response.status_code < 400:
            return True
        return None

    async def item_seller(self, item_id: str) -> str | None:
        if not self.app_id:
            return None
        params = {
            "callname": "GetSingleItem",
            "responseencoding": "XML",
            "appid": self.app_id,
            "siteid": self._site_id(),
            "version": "1199",
            "ItemID": item_id,
            "IncludeSelector": "Details",
        }
        response = await self.client.get("https://open.api.ebay.com/shopping", params=params)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.findtext("e:Ack", namespaces=ns) or root.findtext("Ack")
        if ack and ack.lower() not in {"success", "warning"}:
            return None
        return (
            root.findtext(".//e:Seller/e:UserID", namespaces=ns)
            or root.findtext(".//Seller/UserID")
            or None
        )

    async def _seller_listings_api(self, seller: str) -> list[Listing]:
        params: dict[str, str | int] = {
            "OPERATION-NAME": "findItemsAdvanced",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self.app_id or "",
            "GLOBAL-ID": self.global_id,
            "RESPONSE-DATA-FORMAT": "XML",
            "REST-PAYLOAD": "",
            "paginationInput.entriesPerPage": self.max_items,
            "sortOrder": "StartTimeNewest",
            "itemFilter(0).name": "Seller",
            "itemFilter(0).value(0)": seller,
            "itemFilter(1).name": "LocatedIn",
            "itemFilter(1).value": "WorldWide",
            "itemFilter(2).name": "ListingType",
            "itemFilter(2).value": "All",
            "outputSelector(0)": "SellerInfo",
        }
        response = await self.client.get("https://svcs.ebay.com/services/search/FindingService/v1", params=params)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        ns = {"e": "http://www.ebay.com/marketplace/search/v1/services"}
        ack = root.findtext("e:ack", namespaces=ns)
        if ack and ack.lower() not in {"success", "warning"}:
            message = root.findtext(".//e:message", namespaces=ns) or "unknown eBay API error"
            raise RuntimeError(message)

        listings: list[Listing] = []
        for item in root.findall(".//e:item", namespaces=ns):
            item_id = item.findtext("e:itemId", namespaces=ns) or ""
            title = item.findtext("e:title", namespaces=ns) or "Untitled"
            url = item.findtext("e:viewItemURL", namespaces=ns) or ""
            listed_at = item.findtext("e:listingInfo/e:startTime", namespaces=ns)
            listing_type = item.findtext("e:listingInfo/e:listingType", namespaces=ns) or ""
            category = item.findtext("e:primaryCategory/e:categoryName", namespaces=ns) or ""
            quantity_available = item.findtext("e:quantityAvailable", namespaces=ns) or ""
            seller_name = item.findtext("e:sellerInfo/e:sellerUserName", namespaces=ns) or seller
            price_value = item.findtext("e:sellingStatus/e:currentPrice", namespaces=ns) or ""
            price_currency = (
                item.find("e:sellingStatus/e:currentPrice", namespaces=ns).attrib.get("currencyId", "")
                if item.find("e:sellingStatus/e:currentPrice", namespaces=ns) is not None
                else ""
            )
            image_url = item.findtext("e:galleryURL", namespaces=ns)
            if not item_id or not url:
                continue
            listings.append(
                Listing(
                    item_id=item_id,
                    seller=seller_name,
                    title=title,
                    price=f"{price_value} {price_currency}".strip(),
                    url=url,
                    listed_at=listed_at,
                    image_url=image_url,
                    listing_type=listing_type,
                    category=category,
                    quantity_available=quantity_available,
                )
            )

        return await self._hydrate_descriptions(listings[: self.max_items])

    async def _seller_listings_scrape(self, seller: str) -> list[Listing]:
        params = urlencode({"_ssn": seller, "_sop": "10", "_ipg": str(self.max_items)})
        url = f"https://www.ebay.com/sch/i.html?{params}"
        response = await self.client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        listings: list[Listing] = []
        for card in soup.select("li.s-item"):
            title_node = card.select_one(".s-item__title")
            link_node = card.select_one("a.s-item__link")
            price_node = card.select_one(".s-item__price")
            purchase_node = card.select_one(".s-item__purchase-options, .s-item__dynamic")
            if not title_node or not link_node:
                continue
            title = title_node.get_text(" ", strip=True)
            item_url = str(link_node.get("href") or "")
            if not title or title.lower() == "shop on ebay" or not item_url:
                continue
            item_id = self._extract_item_id(item_url)
            if not item_id:
                continue
            listings.append(
                Listing(
                    item_id=item_id,
                    seller=seller,
                    title=title,
                    price=price_node.get_text(" ", strip=True) if price_node else "",
                    url=item_url,
                    listing_type=(
                        purchase_node.get_text(" ", strip=True) if purchase_node else ""
                    ),
                )
            )
            if len(listings) >= self.max_items:
                break
        return await self._hydrate_descriptions(listings)

    async def _hydrate_descriptions(self, listings: list[Listing]) -> list[Listing]:
        semaphore = asyncio.Semaphore(self.description_concurrency)

        async def hydrate_one(listing: Listing) -> Listing:
            async with semaphore:
                description = await self._fetch_description(listing.url)
            return Listing(
                item_id=listing.item_id,
                seller=listing.seller,
                title=listing.title,
                price=listing.price,
                url=listing.url,
                description=description,
                listed_at=listing.listed_at,
                image_url=listing.image_url,
                listing_type=listing.listing_type,
                category=listing.category,
                quantity_available=listing.quantity_available,
            )

        results = await asyncio.gather(
            *(hydrate_one(listing) for listing in listings), return_exceptions=True
        )
        hydrated = []
        for result in results:
            if isinstance(result, Listing):
                hydrated.append(result)
            else:
                log.debug("Could not hydrate listing description", exc_info=result)
        return hydrated

    async def _fetch_description(self, url: str) -> str:
        try:
            response = await self.client.get(url)
            response.raise_for_status()
        except Exception:
            log.debug("Could not fetch listing description from %s", url, exc_info=True)
            return ""
        soup = BeautifulSoup(response.text, "html.parser")
        for selector in [
            ('meta[property="og:description"]', "content"),
            ('meta[name="description"]', "content"),
        ]:
            node = soup.select_one(selector[0])
            if node and node.get(selector[1]):
                return self._clean_description(str(node.get(selector[1])))
        return ""

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
    def seller_url(seller: str) -> str:
        return f"https://www.ebay.com/sch/i.html?_ssn={quote_plus(seller)}&_sop=10"

    def _site_id(self) -> str:
        site_ids = {
            "EBAY-US": "0",
            "EBAY-GB": "3",
            "EBAY-DE": "77",
            "EBAY-AU": "15",
            "EBAY-ENCA": "2",
            "EBAY-FR": "71",
            "EBAY-IT": "101",
            "EBAY-ES": "186",
        }
        return site_ids.get(self.global_id.upper(), "0")
