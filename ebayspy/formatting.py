"""Platform-neutral text formatting shared by alert renderers.

Everything here is pure string work — prices, seller ratings, timestamps —
with no knowledge of which chat platform the result is shown on.
"""

from __future__ import annotations

from datetime import datetime, timezone

EBAY_DOMAINS = {
    "EBAY-US": "ebay.com",
    "EBAY-GB": "ebay.co.uk",
    "EBAY-DE": "ebay.de",
    "EBAY-FR": "ebay.fr",
    "EBAY-IT": "ebay.it",
    "EBAY-ES": "ebay.es",
    "EBAY-IE": "ebay.ie",
    "EBAY-NL": "ebay.nl",
    "EBAY-AT": "ebay.at",
    "EBAY-BE": "ebay.be",
    "EBAY-AU": "ebay.com.au",
    "EBAY-CA": "ebay.ca",
    "EBAY-CH": "ebay.ch",
    "EBAY-PL": "ebay.pl",
}

CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
    "JPY": "¥",
    "CNY": "¥",
    "CHF": "CHF ",
}

LISTING_TYPE_LABELS = {
    "FIXED_PRICE": "Buy It Now",
    "AUCTION": "Auction",
    "AUCTION_WITH_BIN": "Auction + Buy It Now",
    "BEST_OFFER": "Best Offer",
    "CLASSIFIED_AD": "Classified Ad",
}

# eBay aspect names worth surfacing on a deal, in display order.
_SPEC_ASPECTS = ("Brand", "Model", "Storage Capacity", "Colour", "Color", "Size", "Type")


def truncate_text(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, preferring a line boundary.

    Every line of an alert is self-contained, so cutting at a newline keeps the
    message readable. Better a trimmed alert than an API rejection that drops
    it entirely.
    """
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1]
    newline = clipped.rfind("\n")
    if newline > limit // 2:
        clipped = clipped[:newline]
    return clipped.rstrip() + "…"


def format_price(price: str | None) -> str:
    """Turn an eBay "430.00 GBP" string into a friendly "£430.00"."""
    if not price or not price.strip():
        return "Unknown"
    tokens = price.split()
    currency = tokens[-1] if len(tokens) >= 2 else ""
    if len(currency) == 3 and currency.isalpha() and currency.isupper():
        amount = " ".join(tokens[:-1])
        symbol = CURRENCY_SYMBOLS.get(currency)
        try:
            amount = f"{float(amount):,.2f}"
        except ValueError:
            pass
        if symbol:
            return f"{symbol}{amount}"
        return f"{amount} {currency}"
    return price.strip()


def spec_line(aspects: dict[str, str]) -> str:
    """The handful of catalog aspects worth showing on a deal, joined with dots."""
    if not aspects:
        return ""
    lower = {k.lower(): (k, v) for k, v in aspects.items()}
    seen: set[str] = set()
    bits = []
    for name in _SPEC_ASPECTS:
        entry = lower.get(name.lower())
        if entry and entry[1] not in seen:
            seen.add(entry[1])
            bits.append(entry[1])
    return " · ".join(bits[:4])


def format_seller_rating(percent: object, score: object) -> str:
    """Render eBay's native seller feedback as "98.6% (1,004)".

    The percentage is eBay's positive-feedback rating; the count in parentheses
    is the total feedback received.
    """
    parts = []
    try:
        pct = float(percent)
        if pct > 0:
            parts.append(f"{pct:g}%")
    except (TypeError, ValueError):
        pass
    try:
        count = int(float(score))
        if count > 0:
            parts.append(f"({count:,})")
    except (TypeError, ValueError):
        pass
    return " ".join(parts)


def format_money(value: float, currency: str) -> str:
    """Format a numeric amount with its currency symbol (e.g. 430.0 GBP -> £430.00)."""
    symbol = CURRENCY_SYMBOLS.get(currency)
    try:
        amount = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}".strip()


def format_listing_type(listing_type: str) -> str:
    """Map eBay buying options to readable labels (FIXED_PRICE -> Buy It Now)."""
    labels = [
        LISTING_TYPE_LABELS.get(token, token.replace("_", " ").title())
        for token in (part.strip() for part in listing_type.split(","))
        if token
    ]
    return " · ".join(labels)


def format_timestamp(value: str | None) -> str:
    """Format an ISO 8601 timestamp as "13 Jun 2026, 09:36 UTC"."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.day} {parsed:%b %Y}, {parsed:%H:%M} UTC"


_EVIDENCE_NAMES = {"asks": "asking", "cleared": "ended", "sold": "sold"}


def evidence_line(quote, currency: str) -> str:
    """What the fair value rests on, e.g. "From 28 asking £430 · 9 ended £405"."""
    if quote is None or not quote.components:
        return ""
    return "From " + " · ".join(
        f"{part.n} {_EVIDENCE_NAMES.get(part.source, part.source)} "
        f"{format_money(part.price, currency)}"
        for part in quote.components
    )
