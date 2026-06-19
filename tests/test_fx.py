import asyncio

from ebayspy.fx import FxConverter
from ebayspy.market import find_arbitrage


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get(self, url, timeout=None):
        return _FakeResp(self._payload)


def test_fx_refresh_adopts_sane_rates() -> None:
    fx = FxConverter()
    asyncio.run(fx.refresh(_FakeClient({"rates": {"USD": 1.0, "GBP": 0.5, "EUR": 0.9}})))
    assert fx.rates["GBP"] == 0.5  # adopted live rate


def test_fx_refresh_rejects_garbage_payload() -> None:
    fx = FxConverter()
    before = dict(fx.rates)
    # Wrong base (USD != ~1), negatives, and non-numeric must all be rejected
    # wholesale rather than corrupting later conversions.
    asyncio.run(fx.refresh(_FakeClient({"rates": {"USD": 2.0, "GBP": -1, "EUR": "x"}})))
    assert fx.rates == before
    # A payload missing USD entirely is also rejected (can't trust the base).
    asyncio.run(fx.refresh(_FakeClient({"rates": {"GBP": 0.79}})))
    assert fx.rates == before


def test_fx_convert_via_usd() -> None:
    fx = FxConverter({"USD": 1.0, "GBP": 0.80, "EUR": 1.00})
    # 100 EUR -> USD 100 -> GBP 80
    assert fx.convert(100, "EUR", "GBP") == 80
    assert fx.convert(50, "GBP", "GBP") == 50
    assert fx.convert(10, "GBP", "XYZ") is None


def test_find_arbitrage_detects_gap() -> None:
    fx = FxConverter({"USD": 1.0, "GBP": 0.80, "EUR": 1.00})
    # GBP market ~ £240 (=$300); EUR market ~ €400 (=$400). 25% gap.
    quotes = [("EBAY_GB", 240.0, "GBP"), ("EBAY_DE", 400.0, "EUR")]
    result = find_arbitrage(quotes, fx.convert, 20.0, "GBP")

    assert result is not None
    assert result["buy_marketplace"] == "EBAY_GB"
    assert result["sell_marketplace"] == "EBAY_DE"
    assert round(result["gap_percent"]) == 25


def test_find_arbitrage_below_threshold_is_none() -> None:
    fx = FxConverter({"USD": 1.0, "GBP": 0.80, "EUR": 1.00})
    quotes = [("EBAY_GB", 240.0, "GBP"), ("EBAY_DE", 310.0, "EUR")]  # ~3% gap
    assert find_arbitrage(quotes, fx.convert, 20.0, "GBP") is None
