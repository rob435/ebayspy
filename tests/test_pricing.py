import math
import random
import statistics

import pytest

from ebayspy import pricing
from ebayspy.pricing import Obs, fair_value, fuse, robust_estimate, weighted_quantile


def _obs(prices, seller_prefix="s"):
    return [Obs(p, seller=f"{seller_prefix}{i}") for i, p in enumerate(prices)]


def _legacy_trimmed_median(values):
    """The estimator this module replaced: a median, re-taken after dropping
    anything outside 0.4x-2.5x of it. Kept here as the yardstick to beat."""
    values = sorted(v for v in values if v > 0)
    rough = statistics.median(values)
    kept = [v for v in values if rough * 0.4 <= v <= rough * 2.5]
    return statistics.median(kept) if kept else rough


# --- building blocks -------------------------------------------------------


@pytest.mark.parametrize("values", [[3, 1, 2], [4, 1, 3, 2], [7], [5, 5, 5, 9]])
def test_weighted_quantile_matches_plain_median_for_equal_weights(values) -> None:
    assert weighted_quantile([(v, 1.0) for v in values], 0.5) == statistics.median(values)


def test_weighted_quantile_follows_the_weight() -> None:
    # 100 carries three quarters of the weight, so it is the median.
    assert weighted_quantile([(50, 1.0), (100, 3.0)], 0.5) == 100


def test_robust_estimate_clips_far_outliers_but_keeps_the_market() -> None:
    estimate = robust_estimate(_obs([5, 180, 200, 220, 5000]))
    assert estimate.price == 200
    assert estimate.n == 3  # the spare part and the bundle are gone


def test_robust_estimate_does_not_overclip_a_small_honest_sample() -> None:
    # Five listings, two of them 25% either side. With so few points the spread
    # estimate is itself shaky, so nothing here is confidently an outlier.
    estimate = robust_estimate(_obs([150, 190, 200, 210, 250]))
    assert estimate.price == 200 and estimate.n == 5


def test_identical_prices_are_not_zero_uncertainty() -> None:
    estimate = robust_estimate(_obs([100] * 8))
    assert estimate.sigma == pricing.MIN_SIGMA
    assert estimate.se > 0


def test_standard_error_shrinks_with_sample_size() -> None:
    rng = random.Random(1)
    small = robust_estimate(_obs([100 * math.exp(rng.gauss(0, 0.15)) for _ in range(6)]))
    large = robust_estimate(_obs([100 * math.exp(rng.gauss(0, 0.15)) for _ in range(120)]))
    assert large.se < small.se / 2


def test_one_sellers_duplicates_cannot_set_the_market() -> None:
    # 20 independent sellers around 100, plus one bulk lister with 24 copies at 120.
    rng = random.Random(7)
    market = [Obs(100 + rng.uniform(-6, 6), seller=f"s{i}") for i in range(20)]
    bulk = [Obs(120, seller="bulk") for _ in range(24)]
    naive = statistics.median([o.price for o in market + bulk])
    estimate = robust_estimate(market + bulk)
    assert naive == 120  # by listing count, the bulk lister IS the median
    assert abs(estimate.price - 100) < 5  # by independent sellers, it is not


def test_duplicates_from_one_seller_count_as_little_evidence() -> None:
    # Six listings from one seller are worth √6 ≈ 2.4 independent observations.
    estimate = robust_estimate([Obs(100, seller="only") for _ in range(6)])
    assert estimate.n == 6
    assert estimate.n_eff == pytest.approx(math.sqrt(6))


def test_empty_and_invalid_input() -> None:
    assert robust_estimate([]) is None
    assert robust_estimate([Obs(0), Obs(-5), Obs(float("nan"))]) is None
    assert fair_value() is None
    assert fuse([None]) is None


# --- fusion ---------------------------------------------------------------


def test_single_source_keeps_its_exact_centre() -> None:
    fair = fair_value(_obs([190, 200, 210, 200, 205]))
    assert fair.price == 200
    assert [c.source for c in fair.components] == ["asks"]


def test_sold_prices_dominate_asking_prices() -> None:
    asks = _obs([400 + i for i in range(12)], "a")
    sold = _obs([360 + i for i in range(12)], "b")
    fair = fair_value(asks, sold=sold)
    # Real transactions carry far more weight than what sellers hope for.
    assert 360 < fair.price < 372
    assert fair.component("sold").n == 12


def test_ended_listings_pull_fair_value_below_stale_asks() -> None:
    asks = _obs([120 + i for i in range(10)], "a")
    cleared = _obs([100 + i for i in range(10)], "c")
    asks_only = fair_value(asks)
    fused = fair_value(asks, cleared=cleared)
    assert fused.price < asks_only.price
    assert fused.price < 115  # nearer what cleared than what is still unsold


def test_disagreeing_sources_widen_the_interval() -> None:
    asks = _obs([100 + i * 0.1 for i in range(30)], "a")
    agree = fair_value(asks, sold=_obs([99 + i * 0.1 for i in range(30)], "b"))
    clash = fair_value(asks, sold=_obs([70 + i * 0.1 for i in range(30)], "b"))
    assert clash.se > agree.se * 2


def test_asking_prices_alone_never_earn_high_confidence() -> None:
    # However many listings, asks only tell you what people hope to get.
    many = fair_value(_obs([100 + (i % 7) for i in range(200)]))
    assert many.confidence <= 50
    backed = fair_value(
        _obs([100 + (i % 7) for i in range(200)], "a"),
        sold=_obs([98 + (i % 5) for i in range(40)], "b"),
    )
    assert backed.confidence > 85
    assert backed.low < backed.price < backed.high


def test_confidence_falls_as_evidence_thins() -> None:
    rng = random.Random(3)
    thin = fair_value(_obs([100 * math.exp(rng.gauss(0, 0.25)) for _ in range(5)]))
    deep = fair_value(_obs([100 * math.exp(rng.gauss(0, 0.25)) for _ in range(150)]))
    assert thin.confidence < deep.confidence
    assert (thin.high - thin.low) > (deep.high - deep.low)


# --- trading helpers ---------------------------------------------------------


def test_deal_ceiling_is_the_plain_discount_without_a_confidence_gate() -> None:
    assert pricing.deal_ceiling(100, 15) == pytest.approx(85)
    assert pricing.deal_ceiling(100, 15, predictive_sd=0.5, z=0) == pytest.approx(85)


def test_deal_ceiling_rises_only_when_the_market_is_noisy() -> None:
    tight = pricing.deal_ceiling(100, 15, predictive_sd=0.08, z=1.0)
    noisy = pricing.deal_ceiling(100, 15, predictive_sd=0.30, z=1.0)
    assert tight == pytest.approx(85)  # 15% already exceeds one sd
    assert noisy == pytest.approx(100 * math.exp(-0.30))  # ≈ 74: needs a bigger gap


def test_edge_z_ranks_a_tight_market_gap_above_a_noisy_one() -> None:
    assert pricing.edge_z(85, 100, 0.05) > pricing.edge_z(75, 100, 0.30)
    assert pricing.edge_z(0, 100, 0.1) == 0.0


def test_quick_sale_and_net_proceeds() -> None:
    assert pricing.quick_sale_price(100, 0.2) < pricing.quick_sale_price(100, 0.05) < 100
    # 12.8% + 0.30 fee on the buyer's total, and you still pay 3.50 postage.
    assert pricing.net_proceeds(100, 12.8, 0.30, 3.50) == pytest.approx(83.40)


def test_recency_weight_halves_each_half_life() -> None:
    assert pricing.recency_weight(0, 10) == 1.0
    assert pricing.recency_weight(10, 10) == pytest.approx(0.5)
    assert pricing.recency_weight(20, 10) == pytest.approx(0.25)
    assert pricing.recency_weight(-3, 10) == 1.0
    assert pricing.recency_weight(5, 0) == 1.0


# --- does it actually estimate better? ---------------------------------------
# Simulated markets with a known true price. The assumptions are stated in each
# test; they are the documented failure modes of a median of asking prices.


def _mean_abs_error(estimator, make_sample, truth=100.0, trials=300, seed=11) -> float:
    rng = random.Random(seed)
    return statistics.fmean(abs(estimator(make_sample(rng)) - truth) for _ in range(trials))


def test_beats_trimmed_median_when_strays_sit_inside_the_trim_band() -> None:
    # 30 genuine listings (10% spread) plus 12 cheaper look-alikes at 45-70% of
    # the price: too close to be cut by a fixed 0.4x band, far enough to bias it.
    def sample(rng):
        genuine = [Obs(100 * math.exp(rng.gauss(0, 0.10)), seller=f"g{i}") for i in range(30)]
        strays = [Obs(rng.uniform(45, 70), seller=f"x{i}") for i in range(12)]
        return genuine + strays

    legacy = _mean_abs_error(lambda s: _legacy_trimmed_median([o.price for o in s]), sample)
    robust = _mean_abs_error(lambda s: robust_estimate(s).price, sample)
    assert robust < legacy * 0.6


def test_beats_trimmed_median_under_seller_concentration() -> None:
    def sample(rng):
        market = [Obs(100 * math.exp(rng.gauss(0, 0.08)), seller=f"s{i}") for i in range(20)]
        return market + [Obs(125, seller="bulk") for _ in range(18)]

    legacy = _mean_abs_error(lambda s: _legacy_trimmed_median([o.price for o in s]), sample)
    robust = _mean_abs_error(lambda s: robust_estimate(s).price, sample)
    assert robust < legacy * 0.5


def test_no_worse_than_trimmed_median_on_a_clean_market() -> None:
    def sample(rng):
        return [Obs(100 * math.exp(rng.gauss(0, 0.12)), seller=f"s{i}") for i in range(40)]

    legacy = _mean_abs_error(lambda s: _legacy_trimmed_median([o.price for o in s]), sample)
    robust = _mean_abs_error(lambda s: robust_estimate(s).price, sample)
    assert robust <= legacy * 1.1


def test_confidence_gate_cuts_noise_deals_in_a_wide_market() -> None:
    # 200 listings of pure scatter (25% spread), no real bargains at all. A flat
    # 15% rule calls about a quarter of them deals; the gate asks for more.
    rng = random.Random(5)
    prices = [100 * math.exp(rng.gauss(0, 0.25)) for _ in range(200)]
    fair = fair_value(_obs(prices))
    flat = sum(p <= pricing.deal_ceiling(fair.price, 15) for p in prices)
    gated = sum(p <= pricing.deal_ceiling(fair.price, 15, fair.predictive_sd, 1.0) for p in prices)
    assert flat > 35
    assert gated < flat * 0.7


def test_interval_covers_the_truth_about_as_often_as_it_claims() -> None:
    # Calibration on clean sold data: the ~90% interval should contain the true
    # price most of the time (and not be so wide it always does trivially).
    rng = random.Random(21)
    hits = widths = 0.0
    trials = 400
    for _ in range(trials):
        sold = _obs([100 * math.exp(rng.gauss(0, 0.15)) for _ in range(25)])
        fair = fair_value(sold=sold)
        hits += fair.low <= 100 <= fair.high
        widths += fair.high - fair.low
    assert 0.85 <= hits / trials <= 0.995
    assert widths / trials < 25
