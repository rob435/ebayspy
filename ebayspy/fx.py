"""Lightweight currency conversion for cross-marketplace comparisons.

Ships with approximate static rates so it works offline, and can refresh live
rates from a free, key-less endpoint. All rates are expressed per US dollar;
conversion between any two currencies goes via USD.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

FX_URL = "https://open.er-api.com/v6/latest/USD"

# Approximate fallbacks (units of currency per 1 USD) so the feature degrades
# gracefully without network access. Refreshed live when possible.
DEFAULT_RATES_PER_USD = {
    "USD": 1.0,
    "GBP": 0.79,
    "EUR": 0.92,
    "AUD": 1.52,
    "CAD": 1.36,
    "CHF": 0.88,
    "NZD": 1.65,
    "PLN": 3.95,
    "JPY": 157.0,
    "CNY": 7.25,
}


class FxConverter:
    def __init__(self, static_overrides: dict[str, float] | None = None) -> None:
        self.rates = dict(DEFAULT_RATES_PER_USD)
        if static_overrides:
            self.rates.update(static_overrides)
        self._refreshed = False

    async def refresh(self, client) -> None:
        """Best-effort live-rate refresh; silently keeps fallbacks on failure.

        The payload is validated before it is adopted: each rate must be a finite
        positive number in a sane range, and — since the endpoint quotes per-USD —
        USD itself must read ~1. A malformed or differently-based payload is
        rejected wholesale rather than corrupting every later conversion.
        """
        try:
            response = await client.get(FX_URL, timeout=10)
            response.raise_for_status()
            rates = response.json().get("rates") or {}
            cleaned: dict[str, float] = {}
            for code, value in rates.items():
                try:
                    rate = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(rate) and 0 < rate < 1e6:
                    cleaned[str(code).upper()] = rate
            usd = cleaned.get("USD")
            if usd is None or not 0.98 <= usd <= 1.02:
                log.debug("FX payload failed sanity check (USD=%s); keeping fallbacks", usd)
                return
            self.rates.update(cleaned)
            self._refreshed = True
        except Exception:
            log.debug("FX refresh failed; using fallback rates", exc_info=True)

    def convert(self, amount: float, frm: str, to: str) -> float | None:
        if frm == to:
            return amount
        rate_from = self.rates.get(frm)
        rate_to = self.rates.get(to)
        if not rate_from or not rate_to:
            return None
        return amount / rate_from * rate_to
