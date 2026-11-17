"""Fair-value engine: turn noisy listing evidence into a quoted market.

A median of live asking prices answers "what are sellers asking?", not "what
does this trade at?". This module estimates the second, the way a market maker
marks a book: several imperfect evidence sources, each with an honest error bar,
fused into one fair value with a confidence interval.

Evidence sources (all optional except that at least one must be present):

  * **asks**    — live asking prices. Plentiful, but biased above the clearing
                  price: well-priced listings sell and vanish, overpriced ones
                  linger (survivorship).
  * **cleared** — last asking price of listings that were confirmed to have
                  ended. A proxy for a transaction, enriched in the prices that
                  actually cleared.
  * **sold**    — real completed-sale prices (Marketplace Insights), when the
                  account is entitled to them.

Each source is reduced to a robust location with a standard error:

  1. one seller's duplicate listings are down-weighted (weight 1/√k), so a
     bulk lister cannot set "the market" alone;
  2. the centre is a weighted median, the spread a weighted MAD in log space
     (prices are multiplicative), and observations beyond 3 robust sigmas — or
     outside a hard 0.4×–2.5× band — are clipped iteratively;
  3. the standard error is the sampling error of a median over the *effective*
     sample size, plus a per-source systematic term for how far that kind of
     evidence can sit from the true clearing price however many rows there are.

Sources are then fused by inverse variance in log space. When they disagree by
more than their error bars allow, the fused variance is inflated (Birge ratio)
instead of pretending to a precision the evidence does not support.

Everything here is pure and dependency-free: no I/O, no clock, no globals.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Systematic uncertainty per source, in log units (≈ fractions for small values).
# These are priors on how far each *kind* of evidence can sit from the clearing
# price regardless of sample size, and they set the evidence hierarchy:
# sold > cleared > asks.
ASK_BIAS_SE = 0.08
CLEARED_BIAS_SE = 0.035
SOLD_BIAS_SE = 0.015

# Dispersion floor: five identical prices are not a zero-uncertainty market.
MIN_SIGMA = 0.03
# Robust-sigma clip, and the hard ratio band it can never exceed.
CLIP_Z = 3.0
BAND_LOW = 0.4
BAND_HIGH = 2.5
# MAD → standard deviation under normality; and the efficiency loss of a median.
_MAD_TO_SIGMA = 1.4826
_MEDIAN_SE_FACTOR = 1.2533
# se at which confidence reads 50%.
_CONFIDENCE_HALF_SE = 0.08
# z for the displayed interval (≈ 90% two-sided).
INTERVAL_Z = 1.645
# The lower-quartile price: what you list at to sell ahead of most of the market.
_QUICK_SALE_Z = 0.6745


@dataclass(frozen=True)
class Obs:
    """One price observation. ``weight`` carries recency/quantity; ``seller``
    (when known) drives the duplicate-lister down-weighting."""

    price: float
    weight: float = 1.0
    seller: str = ""


@dataclass(frozen=True)
class Estimate:
    """A robust location estimate from one evidence source."""

    source: str
    price: float
    sigma: float  # per-observation dispersion, log units
    se: float  # standard error of ``price`` incl. systematic term, log units
    n: int  # observations kept after clipping
    n_eff: float  # effective sample size after weighting


@dataclass(frozen=True)
class FairValue:
    """The fused fair value and how much to trust it."""

    price: float
    se: float  # log-space standard error of ``price``
    sigma: float  # market dispersion around fair, log units
    n: int  # total observations behind the estimate
    components: tuple[Estimate, ...] = ()

    @property
    def low(self) -> float:
        return self.price * math.exp(-INTERVAL_Z * self.se)

    @property
    def high(self) -> float:
        return self.price * math.exp(INTERVAL_Z * self.se)

    @property
    def confidence(self) -> int:
        """0–100: how tightly the evidence pins the fair value."""
        return round(100 / (1 + (self.se / _CONFIDENCE_HALF_SE) ** 2))

    @property
    def predictive_sd(self) -> float:
        """Log-sd of one listing's price around our estimate: market noise plus
        our own estimation error. The yardstick for "is this cheap, or noise?"."""
        return math.hypot(self.sigma, self.se)

    def component(self, source: str) -> Estimate | None:
        return next((c for c in self.components if c.source == source), None)


def weighted_quantile(pairs: Sequence[tuple[float, float]], fraction: float) -> float:
    """Weighted quantile of (value, weight) pairs.

    At ``fraction=0.5`` with equal weights this is exactly ``statistics.median``
    (the two middle values are averaged for an even count).
    """
    ordered = sorted((v, w) for v, w in pairs if w > 0)
    if not ordered:
        raise ValueError("weighted_quantile needs at least one positive weight")
    total = sum(w for _, w in ordered)
    target = total * fraction
    tolerance = total * 1e-12
    lower = upper = None
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if lower is None and cumulative >= target - tolerance:
            lower = value
        if cumulative > target + tolerance:
            upper = value
            break
    if lower is None:
        lower = ordered[-1][0]
    if upper is None:
        upper = ordered[-1][0]
    return (lower + upper) / 2


def _seller_weighted(observations: Iterable[Obs]) -> list[tuple[float, float]]:
    """(price, weight) with each seller's k listings scaled by 1/√k."""
    valid = [
        o for o in observations
        if o.price and o.price > 0 and o.weight > 0 and math.isfinite(o.price)
    ]
    counts = Counter(o.seller for o in valid if o.seller)
    return [
        (o.price, o.weight / math.sqrt(counts[o.seller]) if o.seller else o.weight)
        for o in valid
    ]


def robust_estimate(
    observations: Iterable[Obs], source: str = "asks", bias_se: float = 0.0
) -> Estimate | None:
    """Robust centre, spread and standard error of one evidence source."""
    pairs = _seller_weighted(observations)
    if not pairs:
        return None
    kept = pairs
    centre = sigma = 0.0
    for _ in range(5):
        centre = weighted_quantile(kept, 0.5)
        deviations = [(abs(math.log(p / centre)), w) for p, w in kept]
        sigma = max(MIN_SIGMA, _MAD_TO_SIGMA * weighted_quantile(deviations, 0.5))
        # A spread measured from a handful of points is itself uncertain, so the
        # clip widens at small n rather than throwing away a third of the sample.
        reach = CLIP_Z * (1 + 3 / len(kept)) * sigma
        low = max(math.log(BAND_LOW), -reach)
        high = min(math.log(BAND_HIGH), reach)
        clipped = [(p, w) for p, w in pairs if low <= math.log(p / centre) <= high]
        if not clipped or clipped == kept:
            break
        kept = clipped
    weight_sum = sum(w for _, w in kept)
    # Effective sample size. Kish's formula accounts for unequal (recency)
    # weights; the plain weight sum accounts for one seller's listings not being
    # independent (k of them count as √k). Take whichever is more conservative.
    n_eff = min(weight_sum**2 / sum(w * w for _, w in kept), weight_sum)
    # One degree of freedom is spent locating the centre; this also keeps a
    # single observation from claiming a finite sampling error it cannot have.
    sampling_se = _MEDIAN_SE_FACTOR * sigma / math.sqrt(max(n_eff - 1.0, 0.5))
    return Estimate(
        source=source,
        price=centre,
        sigma=sigma,
        se=math.hypot(sampling_se, bias_se),
        n=len(kept),
        n_eff=n_eff,
    )


def fuse(estimates: Iterable[Estimate | None]) -> FairValue | None:
    """Inverse-variance fusion of source estimates in log space."""
    parts = [e for e in estimates if e is not None and e.n > 0 and e.se > 0]
    if not parts:
        return None
    if len(parts) == 1:  # nothing to fuse: keep the source's exact centre
        only = parts[0]
        return FairValue(only.price, only.se, only.sigma, only.n, (only,))
    weights = [1.0 / (e.se * e.se) for e in parts]
    total = sum(weights)
    mean = sum(w * math.log(e.price) for w, e in zip(weights, parts)) / total
    variance = 1.0 / total
    if len(parts) > 1:
        # Sources that disagree beyond their error bars widen the interval.
        chi2 = sum(w * (math.log(e.price) - mean) ** 2 for w, e in zip(weights, parts))
        variance *= max(1.0, chi2 / (len(parts) - 1))
    asks = next((e for e in parts if e.source == "asks"), None)
    sigma = asks.sigma if asks is not None else max(e.sigma for e in parts)
    return FairValue(
        price=math.exp(mean),
        se=math.sqrt(variance),
        sigma=sigma,
        n=sum(e.n for e in parts),
        components=tuple(parts),
    )


def fair_value(
    asks: Iterable[Obs] = (),
    cleared: Iterable[Obs] = (),
    sold: Iterable[Obs] = (),
) -> FairValue | None:
    """Fair value from whatever evidence exists, or None when there is none."""
    return fuse(
        (
            robust_estimate(asks, "asks", ASK_BIAS_SE),
            robust_estimate(cleared, "cleared", CLEARED_BIAS_SE),
            robust_estimate(sold, "sold", SOLD_BIAS_SE),
        )
    )


def recency_weight(age_days: float, half_life_days: float) -> float:
    """Exponential decay so last week's evidence outweighs last month's."""
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def deal_ceiling(
    price: float, discount_percent: float, predictive_sd: float = 0.0, z: float = 0.0
) -> float:
    """Highest price that still counts as a deal against fair value ``price``.

    The configured discount is always required. On top of that the edge must
    exceed ``z`` predictive standard deviations, so the bar rises on its own in
    thin or noisy markets — where a 15% gap is ordinary scatter, not a bargain —
    and is untouched in tight, well-sampled ones.
    """
    discount = min(max(discount_percent, 0.0), 99.0) / 100
    required = max(-math.log(1 - discount), max(z, 0.0) * max(predictive_sd, 0.0))
    return price * math.exp(-required)


def edge_z(price: float, fair: float, predictive_sd: float) -> float:
    """How many predictive standard deviations ``price`` sits below ``fair``."""
    if price <= 0 or fair <= 0:
        return 0.0
    return math.log(fair / price) / max(predictive_sd, MIN_SIGMA)


def quick_sale_price(fair: float, sigma: float) -> float:
    """A conservative exit: the lower-quartile price for this market's spread."""
    return fair * math.exp(-_QUICK_SALE_Z * max(sigma, MIN_SIGMA))


def net_proceeds(
    exit_price: float, fee_percent: float, fee_fixed: float, shipping: float = 0.0
) -> float:
    """What a resale at ``exit_price`` (buyer's total) actually leaves you: the
    marketplace fee is charged on the total, and you still pay the postage."""
    return exit_price * (1 - fee_percent / 100) - fee_fixed - shipping
