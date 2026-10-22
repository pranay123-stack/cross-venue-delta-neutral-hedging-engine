"""Rolling volatility, correlation and beta estimation, on real venue history.

The load-bearing property is that the estimate equals the realised volatility
of the prices it was fed, *independently of how often the feed ticks*.  The
inputs are real Delta Exchange and Hyperliquid candles from
``tests/data/history.json.gz``, so there is no configured "true" volatility to
recover; instead each estimate is checked against a direct computation from
the same closes.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hedgelab.hedge.objectives import RiskParameters
from hedgelab.marketdata.history import HistoryBundle
from hedgelab.marketdata.stats import RollingStats

D = Decimal
DELTA_BTC = "PAPER_DELTA:BTCUSD"
HL_BTC = "PAPER_HYPERLIQUID:BTC"
HL_PAXG = "PAPER_HYPERLIQUID:PAXG"
PERIODS_PER_DAY = {"5m": 288, "1h": 24}


def drive(
    history: HistoryBundle, *, resolution: str = "5m", sample_seconds: float = 300.0,
    keys: tuple[str, ...] | None = None, limit: int | None = None, window: int = 5000,
) -> RollingStats:
    """Feed real closes, aligned by timestamp, to a fresh estimator."""
    stats = RollingStats(window=window, sample_seconds=sample_seconds, min_samples=30)
    by_time: dict[datetime, dict[str, Decimal]] = {}
    for key in keys or tuple(history.instruments):
        for at, close in history[key].closes(resolution):
            by_time.setdefault(at, {})[key] = close
    for at in sorted(by_time)[:limit]:
        stats.observe_many(by_time[at], at)
    return stats


def direct_daily_vol(history: HistoryBundle, key: str, resolution: str) -> float:
    """Sample standard deviation of log returns, scaled to a day."""
    closes = [float(c) for _, c in history[key].closes(resolution)]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(PERIODS_PER_DAY[resolution])


# ======================================================================
# accuracy
# ======================================================================
@pytest.mark.parametrize("key", [
    DELTA_BTC, HL_BTC, "PAPER_DELTA:ETHUSD", "PAPER_HYPERLIQUID:SOL",
    "PAPER_DELTA:PAXGUSD", HL_PAXG,
])
def test_estimate_equals_the_realised_volatility_of_the_feed(history, key: str) -> None:
    estimate = drive(history).estimate(key)
    assert estimate.is_reliable
    expected = direct_daily_vol(history, key, "5m")
    assert abs(float(estimate.daily_volatility) - expected) < 1e-8, key


def test_estimate_tracks_very_different_volatilities(history) -> None:
    """SOL moves roughly three times as much as tokenised gold; the estimator
    must follow the market rather than gravitate to a default."""
    stats = drive(history)
    sol = float(stats.estimate("PAPER_DELTA:SOLUSD").daily_volatility)
    paxg = float(stats.estimate("PAPER_DELTA:PAXGUSD").daily_volatility)
    assert sol > 2 * paxg


def test_a_slower_feed_than_configured_is_not_overstated(history) -> None:
    """The regression this module exists to prevent.

    ``sample_seconds`` is a minimum spacing, not a guarantee.  Feeding hourly
    candles to an estimator configured for five minutes must give the hourly
    realised volatility -- scaling by the configured interval instead would
    overstate it by sqrt(3600/300) = 3.46x.
    """
    estimate = drive(history, resolution="1h", sample_seconds=300.0).estimate(DELTA_BTC)
    expected = direct_daily_vol(history, DELTA_BTC, "1h")
    assert abs(float(estimate.daily_volatility) - expected) < 1e-8


def test_estimate_is_independent_of_the_configured_sample_interval(history) -> None:
    coarse = drive(history, resolution="1h", sample_seconds=3600.0).estimate(DELTA_BTC)
    fine = drive(history, resolution="1h", sample_seconds=300.0).estimate(DELTA_BTC)
    assert abs(float(coarse.daily_volatility) - float(fine.daily_volatility)) < 1e-9


# ======================================================================
# correlation and beta
# ======================================================================
def test_the_same_coin_on_two_venues_is_almost_perfectly_correlated(history) -> None:
    rho, overlap = drive(history).correlation(DELTA_BTC, HL_BTC)
    assert overlap > 800
    assert rho > D("0.99")


def test_different_underlyings_are_far_less_correlated(history) -> None:
    rho, _ = drive(history).correlation(DELTA_BTC, HL_PAXG)
    assert abs(rho) < D("0.5")


def test_correlation_is_bounded(history) -> None:
    """Floating-point accumulation must never produce |rho| > 1.

    A correlation above one makes the residual volatility imaginary
    downstream.
    """
    stats = drive(history)
    for source, hedge in ((DELTA_BTC, HL_BTC), (DELTA_BTC, HL_PAXG)):
        rho, _ = stats.correlation(source, hedge)
        assert D("-1") <= rho <= D("1")


def test_beta_is_near_one_for_a_matched_pair(history) -> None:
    estimate = drive(history).pair_estimate(DELTA_BTC, HL_BTC)
    assert estimate.is_reliable
    assert D("0.95") < estimate.beta < D("1.05")


def test_correlation_only_pairs_overlapping_samples() -> None:
    """An instrument that starts quoting later must not shift the other."""
    stats = RollingStats(window=100, sample_seconds=60.0, min_samples=5)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for step in range(40):
        at = start + timedelta(seconds=60 * step)
        prices = {"A": D(100 + step)}
        if step >= 20:                      # B joins halfway through
            prices["B"] = D(200 + step)
        stats.observe_many(prices, at)
    _, overlap = stats.correlation("A", "B")
    assert 0 < overlap < 20


# ======================================================================
# refusing to guess
# ======================================================================
def test_estimate_is_refused_without_enough_samples() -> None:
    stats = RollingStats(min_samples=30)
    estimate = stats.estimate("NOPE:NOPE")
    assert estimate.is_reliable is False
    assert estimate.daily_volatility == D("0")
    assert "30 needed" in estimate.note


def test_pair_estimate_reports_why_it_is_unreliable(history) -> None:
    estimate = drive(history, limit=5).pair_estimate(DELTA_BTC, HL_BTC)
    assert estimate.is_reliable is False
    assert estimate.beta == D("0")
    assert "insufficient data" in estimate.note


def test_first_observation_produces_no_return() -> None:
    stats = RollingStats(sample_seconds=1.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert stats.observe("A", D("100"), at) is False
    assert stats.observe("A", D("101"), at + timedelta(seconds=2)) is True


def test_ticks_faster_than_the_sample_interval_are_ignored() -> None:
    stats = RollingStats(sample_seconds=60.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    stats.observe("A", D("100"), at)
    assert stats.observe("A", D("101"), at + timedelta(seconds=5)) is False
    assert stats.observe("A", D("102"), at + timedelta(seconds=90)) is True


def test_non_positive_prices_are_ignored() -> None:
    stats = RollingStats(sample_seconds=1.0)
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert stats.observe("A", D("0"), at) is False
    assert stats.observe("A", D("-5"), at) is False


def test_window_is_bounded(history) -> None:
    stats = drive(history, window=50)
    assert stats.sample_counts()[DELTA_BTC] == 50


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="window"):
        RollingStats(window=1)
    with pytest.raises(ValueError, match="sample_seconds"):
        RollingStats(sample_seconds=0)


# ======================================================================
# integration with RiskParameters
# ======================================================================
def test_with_estimates_replaces_measurements_but_keeps_policy() -> None:
    base = RiskParameters(risk_aversion=D("400"), horizon_days=D("5"),
                          max_ratio=D("1.1"))
    updated = base.with_estimates(
        source_daily_vol=D("0.02"), hedge_daily_vol=D("0.021"),
        correlation=D("0.98"), provenance="200 samples",
    )
    # Measurements replaced.
    assert updated.source_daily_vol == D("0.02")
    assert updated.correlation == D("0.98")
    assert updated.estimated is True
    assert updated.provenance == "200 samples"
    # Policy preserved.
    assert updated.risk_aversion == D("400")
    assert updated.horizon_days == D("5")
    assert updated.max_ratio == D("1.1")


def test_defaults_declare_themselves_as_assumptions() -> None:
    params = RiskParameters()
    assert params.estimated is False
    assert "default" in params.provenance


# ======================================================================
# uncertainty
# ======================================================================
def test_beta_standard_error_shrinks_with_the_square_root_of_samples(history) -> None:
    """SE = sigma_residual / (sigma_hedge * sqrt(n)), so 4x the data halves it."""
    pair = (DELTA_BTC, HL_BTC)
    se_small = float(drive(history, keys=pair, limit=217)
                     .pair_estimate(*pair).beta_standard_error)
    se_large = float(drive(history, keys=pair, limit=865)
                     .pair_estimate(*pair).beta_standard_error)
    assert se_small > se_large > 0
    assert 1.6 < se_small / se_large < 2.6


def test_beta_is_not_called_significant_on_thin_data(history) -> None:
    """On the first ~18 hours alone, beta's distance from 1 is inside two
    standard errors -- noise, and the estimate must say so.

    Reporting a point estimate without its uncertainty invites reading
    sampling error as signal -- which for a hedge ratio means trading it.
    """
    pair = (DELTA_BTC, HL_BTC)
    estimate = drive(history, keys=pair, limit=217).pair_estimate(*pair)
    assert estimate.is_reliable
    assert abs(estimate.beta - D("1")) < estimate.beta_standard_error * D("2")
    assert estimate.beta_is_distinguishable_from_one is False


def test_beta_becomes_significant_with_enough_data(history) -> None:
    """Measured, not asserted: over three days Hyperliquid BTC's 5-minute
    returns run about 1.5% larger than Delta's, so beta sits measurably
    below one."""
    estimate = drive(history, keys=(DELTA_BTC, HL_BTC)).pair_estimate(DELTA_BTC, HL_BTC)
    assert estimate.overlapping_samples > 800
    assert estimate.beta < D("1")
    assert estimate.beta_is_distinguishable_from_one is True


def test_significance_matches_the_two_standard_error_rule(history) -> None:
    stats = drive(history)
    for hedge in (HL_BTC, HL_PAXG, "PAPER_HYPERLIQUID:ETH"):
        estimate = stats.pair_estimate(DELTA_BTC, hedge)
        outside = abs(estimate.beta - D(1)) > D(2) * estimate.beta_standard_error
        assert estimate.beta_is_distinguishable_from_one is outside, hedge


def test_unreliable_estimate_reports_no_standard_error(history) -> None:
    estimate = drive(history, limit=5).pair_estimate(DELTA_BTC, HL_BTC)
    assert estimate.is_reliable is False
    assert estimate.beta_standard_error == D("0")
    assert estimate.beta_is_distinguishable_from_one is False


def test_perfect_correlation_gives_a_zero_standard_error() -> None:
    """Identical series have no residual, so the slope has no sampling error."""
    stats = RollingStats(window=200, sample_seconds=1.0, min_samples=10)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for step in range(60):
        at = start + timedelta(seconds=2 * step)
        price = D(100) + D(step) * D("0.5")
        stats.observe_many({"A": price, "B": price * D(3)}, at)
    estimate = stats.pair_estimate("A", "B")
    assert estimate.correlation == D("1")
    assert estimate.beta_standard_error == D("0")
    assert estimate.beta_is_distinguishable_from_one is False
