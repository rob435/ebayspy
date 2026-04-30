from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
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
        client_secret: str | None = None,
        browser_headless: bool = True,
        browser_profile_dir: Path | None = None,
        browser_executable_path: str | None = None,
        browser_block_wait_seconds: int = 0,
    ) -> None:
        self.app_id = app_id
        self.client_secret = client_secret
        self.global_id = global_id
        self.max_items = max_items
        self.description_concurrency = max(1, description_concurrency)
        self.browser_headless = browser_headless
        self.browser_profile_dir = browser_profile_dir
        self.browser_executable_path = browser_executable_path
        self.browser_block_wait_seconds = max(0, browser_block_wait_seconds)
        self._oauth_token: str | None = None
        self._oauth_token_expires_at = 0.0
        self._playwright = None
        self._browser = None
        self._browser_context = None
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
        if self._browser_context is not None:
            await self._browser_context.close()
            self._browser_context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await self.client.aclose()

    async def seller_listings(self, seller: str) -> list[Listing]:
        return await self._seller_listings_browser(seller)

    async def seller_exists(self, seller: str) -> bool | None:
        try:
            await self._seller_listings_browser(seller)
        except Exception as exc:
            log.warning(
                "Could not validate eBay seller %s with browser scraper %s",
                seller,
                exc.__class__.__name__,
            )
            return None
        return True

    async def item_seller(self, item_id: str) -> str | None:
        return None

    async def _browser_page(self):
        if self._browser_context is not None:
            return await self._browser_context.new_page()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Browser scraping requires Playwright. Run `pip install -e .` and "
                "`python -m playwright install chromium`."
            ) from exc

        self._playwright = await async_playwright().start()
        launch_options = {
            "headless": self.browser_headless,
            "executable_path": self.browser_executable_path,
        }
        launch_options = {key: value for key, value in launch_options.items() if value is not None}
        context_options = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "viewport": {"width": 1365, "height": 900},
        }
        if self.browser_profile_dir is not None:
            self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
            self._browser_context = await self._playwright.chromium.launch_persistent_context(
                str(self.browser_profile_dir),
                **launch_options,
                **context_options,
            )
        else:
            self._browser = await self._playwright.chromium.launch(**launch_options)
            self._browser_context = await self._browser.new_context(**context_options)
        return await self._browser_context.new_page()

    async def _seller_listings_browser(self, seller: str) -> list[Listing]:
        page = await self._browser_page()
        try:
            await page.goto(self.seller_url(seller), wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            content = await page.content()
            if (
                self._is_block_page(content)
                and not self.browser_headless
                and self.browser_block_wait_seconds > 0
            ):
                log.warning(
                    "eBay blocked the visible browser scraper for %s; waiting %s seconds "
                    "for manual browser interaction",
                    seller,
                    self.browser_block_wait_seconds,
                )
                await page.wait_for_timeout(self.browser_block_wait_seconds * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                content = await page.content()
        finally:
            await page.close()
        return self._parse_seller_search_html(seller, content)

    def _parse_seller_search_html(self, seller: str, html: str) -> list[Listing]:
        if self._is_block_page(html):
            raise RuntimeError("eBay blocked the browser scraper for the seller search page")
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []
        for card in soup.select("li.s-item"):
            listing = self._listing_from_search_card(seller, card)
            if listing is None:
                continue
            listings.append(listing)
            if len(listings) >= self.max_items:
                break
        return listings

    def _listing_from_search_card(self, seller: str, card) -> Listing | None:
        title_node = card.select_one(".s-item__title")
        link_node = card.select_one("a.s-item__link")
        price_node = card.select_one(".s-item__price")
        purchase_node = card.select_one(".s-item__purchase-options, .s-item__dynamic")
        image_node = card.select_one(".s-item__image img")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        item_url = str(link_node.get("href") or "") if link_node else ""
        item_id = self._extract_item_id(item_url)
        if not title or title.lower() == "shop on ebay" or not item_id:
            return None
        card_text = card.get_text(" ", strip=True)
        return Listing(
            item_id=item_id,
            seller=seller,
            title=title,
            price=price_node.get_text(" ", strip=True) if price_node else "",
            url=item_url,
            image_url=str(image_node.get("src") or image_node.get("data-src") or "")
            if image_node
            else None,
            listing_type=purchase_node.get_text(" ", strip=True) if purchase_node else "",
            quantity_available=self._quantity_from_text(card_text),
        )

    @staticmethod
    def _is_block_page(html: str) -> bool:
        return "Access Denied" in html or "Service Unavailable" in html

    @staticmethod
    def _quantity_from_text(text: str) -> str:
        match = re.search(r"(\d+)\s+available", text, flags=re.IGNORECASE)
        return match.group(1) if match else ""

    async def _item_seller_api(self, item_id: str) -> str | None:
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

    async def _oauth_access_token(self) -> str:
        if self._oauth_token and time.time() < self._oauth_token_expires_at:
            return self._oauth_token
        if not self.app_id or not self.client_secret:
            raise RuntimeError("EBAY_APP_ID and EBAY_CLIENT_SECRET are required for OAuth")

        credentials = base64.b64encode(f"{self.app_id}:{self.client_secret}".encode()).decode()
        response = await self.client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 7200))
        self._oauth_token = token
        self._oauth_token_expires_at = time.time() + max(60, expires_in - 60)
        return token

    async def _seller_listings_browse(self, seller: str) -> list[Listing]:
        token = await self._oauth_access_token()
        response = await self.client.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id(),
            },
            params={
                "filter": f"sellers:{{{seller}}}",
                "sort": "newlyListed",
                "limit": min(self.max_items, 200),
            },
        )
        response.raise_for_status()
        payload = response.json()
        listings = [
            self._listing_from_browse_item(seller, item)
            for item in payload.get("itemSummaries", [])
        ]
        return [listing for listing in listings if listing.item_id and listing.url]

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
            listed_at=item.get("itemCreationDate") or item.get("itemOriginDate"),
            image_url=(item.get("image") or {}).get("imageUrl"),
            listing_type=", ".join(item.get("buyingOptions") or []),
            category=str(categories[0].get("categoryName") or "") if categories else "",
            quantity_available=self._availability_quantity(item),
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

    def _marketplace_id(self) -> str:
        return self.global_id.upper().replace("-", "_")
