"""Venue clients, spec generation, frames, history and the backtest.

The clients are exercised against **captured real responses** from Delta
Exchange (India), Hyperliquid and Kraken (``tests/data/raw``), served through
an in-process transport -- so the unit conversions are tested against what
the venues actually send, without touching the network.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.conftest import DATA_DIR

from hedgelab.backtest.engine import BacktestConfig, Backtester
from hedgelab.domain.enums import HedgeObjective
from hedgelab.domain.instrument import InstrumentSpec
from hedgelab.instruments.sync import (
    delta_spec,
    hyperliquid_spec,
    hyperliquid_tick,
    mappings_for,
    write_catalogue,
)
from hedgelab.marketdata.collector import LiveCollector
from hedgelab.marketdata.frames import MarketFrame, load_frames, write_frames
from hedgelab.marketdata.fx import FxService
from hedgelab.marketdata.history import HistoryBundle, align_closes
from hedgelab.marketdata.sources import (
    DeltaPublicClient,
    HyperliquidPublicClient,
    KrakenFxClient,
    MarketDataUnavailable,
)

D = Decimal
RAW = DATA_DIR / "raw"


def raw(name: str) -> Any:
    return json.loads((RAW / name).read_text())


def venue_transport() -> httpx.MockTransport:
    """Answers each endpoint with the response the venue really returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.hyperliquid.xyz":
            kind = json.loads(request.content)["type"]
            files = {"metaAndAssetCtxs": "hyperliquid_metaAndAssetCtxs.json",
                     "l2Book": "hyperliquid_l2Book_BTC.json",
                     "candleSnapshot": "hyperliquid_candleSnapshot_BTC_1h.json",
                     "fundingHistory": "hyperliquid_fundingHistory_BTC.json"}
            return httpx.Response(200, json=raw(files[kind]))
        if request.url.host == "api.kraken.com":
            return httpx.Response(200, json=raw("kraken_ticker.json"))
        if path == "/v2/history/candles":
            symbol = request.url.params["symbol"].replace(":", "_")
            return httpx.Response(200, json=raw(f"delta_candles_{symbol}_1h.json"))
        name = {"/v2/products/BTCUSD": "delta_product_BTCUSD.json",
                "/v2/products/PAXGUSD": "delta_product_PAXGUSD.json",
                "/v2/tickers/BTCUSD": "delta_ticker_BTCUSD.json",
                "/v2/l2orderbook/BTCUSD": "delta_l2orderbook_BTCUSD.json"}.get(path)
        if name is None:
            return httpx.Response(404, json={"success": False, "error": "not captured"})
        return httpx.Response(200, json=raw(name))

    return httpx.MockTransport(handler)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=venue_transport())


# ======================================================================
# Delta Exchange
# ======================================================================
async def test_delta_funding_is_converted_from_percent_to_a_fraction() -> None:
    quote = await DeltaPublicClient(client=client()).quote("BTCUSD")
    published = D(raw("delta_ticker_BTCUSD.json")["result"]["funding_rate"])
    assert quote.funding_rate == published / 100


async def test_delta_book_sizes_stay_in_contracts() -> None:
    quote = await DeltaPublicClient(client=client()).quote("BTCUSD", depth=5)
    first = raw("delta_l2orderbook_BTCUSD.json")["result"]["buy"][0]
    assert quote.bids[0] == (D(first["price"]), D(first["size"]))
    assert quote.asks[0][0] > quote.bids[0][0]


async def test_delta_settled_funding_history_is_a_fraction_per_interval() -> None:
    end = datetime.now(UTC)
    points = await DeltaPublicClient(client=client()).funding_history(
        "BTCUSD", end - timedelta(days=1), end)
    candle = raw("delta_candles_FUNDING_BTCUSD_1h.json")["result"][0]
    assert points
    assert D(str(candle["close"])) / 100 in {p.rate for p in points}
    assert points == sorted(points, key=lambda p: p.time)


async def test_delta_unknown_symbol_is_reported_not_guessed() -> None:
    with pytest.raises(MarketDataUnavailable):
        await DeltaPublicClient(client=client()).quote("NOPEUSD")


# ======================================================================
# Hyperliquid
# ======================================================================
async def test_hyperliquid_funding_is_already_a_fraction_per_hour() -> None:
    quotes = await HyperliquidPublicClient(client=client()).quotes(["BTC"])
    _, contexts = raw("hyperliquid_metaAndAssetCtxs.json")
    assert quotes["BTC"].funding_rate == D(contexts[0]["funding"])


async def test_hyperliquid_book_sizes_are_coins() -> None:
    quotes = await HyperliquidPublicClient(client=client()).quotes(["BTC"])
    level = raw("hyperliquid_l2Book_BTC.json")["levels"][0][0]
    assert quotes["BTC"].bids[0] == (D(level["px"]), D(level["sz"]))


async def test_hyperliquid_unknown_coin_is_reported() -> None:
    with pytest.raises(MarketDataUnavailable):
        await HyperliquidPublicClient(client=client()).quotes(["NOPE"])


async def test_kraken_gives_measured_stablecoin_rates() -> None:
    rates = await KrakenFxClient(client=client()).rates()
    assert set(rates) == {("USDC", "USD"), ("USDT", "USD")}
    for rate in rates.values():
        assert D("0.99") < rate < D("1.01")


# ======================================================================
# spec generation from the venues' own metadata
# ======================================================================
def test_delta_spec_is_built_from_the_product_endpoint() -> None:
    product = raw("delta_product_BTCUSD.json")["result"]
    book = raw("delta_l2orderbook_BTCUSD.json")["result"]
    ticker = raw("delta_ticker_BTCUSD.json")["result"]
    spec = InstrumentSpec.model_validate(delta_spec(product, book, ticker))
    assert spec.units_per_quantity == D("0.001")
    # initial_margin is published in percent: 0.5 means 0.5 %, i.e. 200x.
    assert spec.effective_initial_margin_rate == D("0.005")
    assert spec.max_leverage == D(200)
    assert spec.taker_fee_bps == D(5)
    assert spec.funding_interval_hours == D(8)


def test_delta_paxg_funds_every_four_hours() -> None:
    product = raw("delta_product_PAXGUSD.json")["result"]
    spec = delta_spec(product, {"buy": [], "sell": []}, {"funding_rate": "0"})
    assert D(spec["funding_interval_hours"]) == D(4)


def test_hyperliquid_spec_follows_the_published_rules() -> None:
    meta, contexts = raw("hyperliquid_metaAndAssetCtxs.json")
    spec = InstrumentSpec.model_validate(hyperliquid_spec(
        meta["universe"][0], contexts[0], raw("hyperliquid_l2Book_BTC.json")))
    assert spec.symbol == "BTC"
    assert spec.quantity_step == D("0.00001")       # szDecimals = 5
    assert spec.min_notional == D(10)
    assert spec.effective_initial_margin_rate == D(1) / D(40)
    assert spec.effective_maintenance_margin_rate == D(1) / D(80)
    assert spec.funding_interval_hours == D(1)


@pytest.mark.parametrize("price,sz_decimals,tick", [
    ("81802.4", 5, "1"),       # BTC: five significant figures -> whole dollars
    ("2403.85", 4, "0.1"),     # ETH
    ("104.31", 2, "0.01"),     # SOL
    ("4072.35", 3, "0.1"),     # PAXG
    ("0.123456", 0, "0.00001"),   # five significant figures below 1
    ("0.123456", 3, "0.001"),     # capped at 6 - szDecimals = 3 decimals
])
def test_hyperliquid_tick_is_five_significant_figures(price, sz_decimals, tick) -> None:
    assert hyperliquid_tick(D(price), sz_decimals) == D(tick)


def test_the_generated_catalogue_round_trips(tmp_path: Path) -> None:
    from hedgelab.instruments.registry import InstrumentRegistry

    product = raw("delta_product_BTCUSD.json")["result"]
    delta = delta_spec(product, raw("delta_l2orderbook_BTCUSD.json")["result"],
                       raw("delta_ticker_BTCUSD.json")["result"])
    meta, contexts = raw("hyperliquid_metaAndAssetCtxs.json")
    hl = hyperliquid_spec(meta["universe"][0], contexts[0], raw("hyperliquid_l2Book_BTC.json"))
    mappings = [m for m in mappings_for() if m["source"] == "PAPER_DELTA:BTCUSD"]
    write_catalogue(tmp_path, [delta], [hl], mappings)
    registry = InstrumentRegistry.from_directory(tmp_path)
    assert [m.name for m in registry.mappings()] == ["BTC Delta perp -> Hyperliquid perp"]
    assert "GENERATED" in (tmp_path / "hyperliquid.yaml").read_text()


# ======================================================================
# collector and frames
# ======================================================================
async def test_collector_assembles_one_frame_from_both_venues(registry) -> None:
    http = client()
    collector = LiveCollector(
        delta=DeltaPublicClient(client=http), hyperliquid=HyperliquidPublicClient(client=http),
        fx=KrakenFxClient(client=http), depth=3,
    )
    specs = [registry.get("PAPER_DELTA:BTCUSD"), registry.get("PAPER_HYPERLIQUID:BTC")]
    frame = await collector.collect(specs)
    assert set(frame.quotes) == {"PAPER_DELTA:BTCUSD", "PAPER_HYPERLIQUID:BTC"}
    assert frame.missing == ()
    assert len(frame.quotes["PAPER_DELTA:BTCUSD"].bids) == 3
    assert ("USDC", "USD") in frame.fx


async def test_a_failing_venue_marks_its_instruments_missing(registry) -> None:
    http = client()
    collector = LiveCollector(
        delta=DeltaPublicClient(client=http), hyperliquid=HyperliquidPublicClient(client=http),
        fx=KrakenFxClient(client=http),
    )
    # ETHUSD was not captured, so the "venue" answers 404 for it.
    specs = [registry.get("PAPER_DELTA:ETHUSD"), registry.get("PAPER_HYPERLIQUID:BTC")]
    frame = await collector.collect(specs)
    assert frame.missing == ("PAPER_DELTA:ETHUSD",)
    assert "PAPER_HYPERLIQUID:BTC" in frame.quotes


def test_frames_round_trip_through_gzip(tmp_path: Path, frames: list[MarketFrame]) -> None:
    path = tmp_path / "copy.jsonl.gz"
    assert write_frames(path, frames[:25]) == 25
    assert load_frames(path) == frames[:25]


def test_the_committed_recording_is_a_clean_live_session(frames: list[MarketFrame]) -> None:
    assert len(frames) == 500
    assert not any(f.missing for f in frames)
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in itertools.pairwise(frames)]
    assert max(gaps) < 10                 # polled every ~2s, no holes
    assert {k.split(":")[0] for k in frames[0].quotes} == {"PAPER_DELTA", "PAPER_HYPERLIQUID"}


# ======================================================================
# history and the backtest
# ======================================================================
def test_history_round_trips(tmp_path: Path, history: HistoryBundle) -> None:
    path = tmp_path / "h.json.gz"
    history.save(path)
    again = HistoryBundle.load(path)
    assert again.instruments.keys() == history.instruments.keys()
    assert again["PAPER_HYPERLIQUID:BTC"].funding == history["PAPER_HYPERLIQUID:BTC"].funding


def test_closes_align_on_timestamps_both_venues_printed(history: HistoryBundle) -> None:
    path = align_closes(history, "PAPER_DELTA:BTCUSD", "PAPER_HYPERLIQUID:BTC")
    assert len(path) >= 700
    times = [at for at, _, _ in path]
    assert times == sorted(times)
    assert all(abs(a - b) / b < D("0.01") for _, a, b in path)


def test_funding_lookup_returns_the_last_settled_rate(history: HistoryBundle) -> None:
    funding = history["PAPER_HYPERLIQUID:BTC"].funding
    at = funding[10].time + timedelta(minutes=30)
    assert history["PAPER_HYPERLIQUID:BTC"].funding_at(at) == funding[10].rate
    assert history["PAPER_HYPERLIQUID:BTC"].funding_at(funding[0].time - timedelta(hours=1)) is None


@pytest.fixture
def backtester(registry, history: HistoryBundle, fx: FxService) -> Backtester:
    return Backtester(registry.all(), history, fx)


def run(backtester: Backtester, ratio: str, **kwargs: Any):
    return backtester.run(BacktestConfig(
        source_key="PAPER_DELTA:BTCUSD", hedge_key="PAPER_HYPERLIQUID:BTC",
        source_quantity=D(1000), target_ratio=D(ratio), **kwargs,
    ))


def test_backtest_replays_a_month_of_real_hours(backtester: Backtester) -> None:
    report = run(backtester, "1")
    assert len(report.points) >= 700
    # Delta settles every 8h, Hyperliquid every hour: ~1 + 8 per 8 hours.
    hours = len(report.points)
    assert abs(report.funding_settlements - (hours // 8 + hours)) <= 2


def test_risk_reduction_tracks_the_hedge_ratio(backtester: Backtester) -> None:
    """The correctness check: removing half the exposure removes about half
    the real drawdown."""
    full = run(backtester, "1").risk_reduction_pct
    half = run(backtester, "0.5").risk_reduction_pct
    none = run(backtester, "0.0001").risk_reduction_pct
    assert full > D(95)
    assert D(40) < half < D(60)
    assert none < D(5)


def test_backtest_costs_are_separated_and_sum_to_the_net(backtester: Backtester) -> None:
    report = run(backtester, "1")
    last = report.points[-1]
    gross = last.net_pnl - last.cumulative_funding + last.cumulative_fees + last.cumulative_slippage
    assert report.total_fees > 0
    assert report.total_slippage > 0
    # Funding is real and its sign is whatever the venues charged.
    assert report.total_funding != 0
    assert abs(report.final_net_pnl - (gross + report.total_funding
                                       - report.total_fees - report.total_slippage)) < D("1e-9")


def test_a_short_window_uses_the_most_recent_hours(backtester: Backtester, history) -> None:
    report = run(backtester, "1", steps=48)
    assert len(report.points) == 48
    last_close = align_closes(history, "PAPER_DELTA:BTCUSD", "PAPER_HYPERLIQUID:BTC")[-1][0]
    assert report.end == last_close


def test_backtest_refuses_without_overlapping_history(registry, fx) -> None:
    empty = HistoryBundle(fetched_at=datetime.now(UTC))
    from hedgelab.marketdata.history import InstrumentHistory

    for key in ("PAPER_DELTA:BTCUSD", "PAPER_HYPERLIQUID:BTC"):
        empty.instruments[key] = InstrumentHistory(key=key)
    with pytest.raises(ValueError, match="not enough overlapping"):
        Backtester(registry.all(), empty, fx).run(BacktestConfig(
            source_key="PAPER_DELTA:BTCUSD", hedge_key="PAPER_HYPERLIQUID:BTC",
            source_quantity=D(1000), objective=HedgeObjective.BASE_ASSET_NEUTRAL,
        ))


# ======================================================================
# settlement schedule, checked against the venues' own history
# ======================================================================
@pytest.mark.parametrize("key", ["PAPER_DELTA:BTCUSD", "PAPER_DELTA:ETHUSD"])
def test_delta_rates_change_only_at_its_eight_hour_settlements(history, registry, key) -> None:
    """The backtest settles at UTC multiples of the interval; a month of
    Delta's settled funding says that is when the rate actually changes.

    (SOLUSD and the 4-hourly PAXGUSD sat at their baseline all month, so
    their schedule cannot be confirmed from this history.)
    """
    spec = registry.get(key)
    funding = history[key].funding
    changes = [b.time for a, b in itertools.pairwise(funding) if b.rate != a.rate]
    assert len(changes) > 50
    assert all(Backtester._settles(spec, at) for at in changes)
    assert {at.hour for at in changes} == {0, 8, 16}


def test_hyperliquid_rate_changes_every_hour(history, registry) -> None:
    spec = registry.get("PAPER_HYPERLIQUID:BTC")
    funding = history["PAPER_HYPERLIQUID:BTC"].funding
    changes = [b.time for a, b in itertools.pairwise(funding) if b.rate != a.rate]
    assert len({at.hour for at in changes}) == 24
    assert all(Backtester._settles(spec, at.replace(second=0, microsecond=0)) for at in changes)


# ======================================================================
# price impact measured from real books
# ======================================================================
@pytest.fixture(scope="module")
def impact_curves(frames_module):
    from hedgelab.marketdata.impact import measure_impact

    registry, frames = frames_module
    return {spec.key: measure_impact(spec, frames) for spec in registry.all()}


@pytest.fixture(scope="module")
def frames_module():
    from tests.conftest import LIVE_FRAMES, SPEC_DIR

    from hedgelab.instruments.registry import InstrumentRegistry

    return InstrumentRegistry.from_directory(SPEC_DIR), load_frames(LIVE_FRAMES)


def test_impact_grows_with_order_size(impact_curves) -> None:
    for key, curve in impact_curves.items():
        assert len(curve.points) >= 3, key
        costs = [p.buy_bps for p in curve.points]
        assert costs[-1] > costs[0], key           # a bigger sweep costs more
        assert all(c > 0 for c in costs), key     # never better than the mid


def test_small_orders_pay_about_half_the_real_spread(impact_curves, frames) -> None:
    curve = impact_curves["PAPER_HYPERLIQUID:BTC"]
    spreads = sorted(
        (f.quotes["PAPER_HYPERLIQUID:BTC"].asks[0][0] - f.quotes["PAPER_HYPERLIQUID:BTC"].bids[0][0])
        / f.quotes["PAPER_HYPERLIQUID:BTC"].bids[0][0] * 10000
        for f in frames
    )
    median_half_spread = spreads[len(spreads) // 2] / 2
    assert abs(curve.points[0].buy_bps - median_half_spread) < D("0.01")


def test_impact_interpolates_and_flags_sizes_beyond_the_recording(impact_curves) -> None:
    curve = impact_curves["PAPER_HYPERLIQUID:PAXG"]
    first, second = curve.points[0], curve.points[1]
    middle = (first.notional + second.notional) / 2
    cost, measured = curve.cost_bps(middle, True)
    assert measured and min(first.buy_bps, second.buy_bps) <= cost <= max(first.buy_bps, second.buy_bps)
    # Hyperliquid's PAXG book is thin: a $10M order was never fillable.
    cost, measured = curve.cost_bps(D(10_000_000), True)
    assert measured is False and cost == curve.points[-1].buy_bps


def test_measured_impact_costs_more_than_a_flat_spread_for_large_size(
    registry, history, fx, impact_curves,
) -> None:
    """On a 10 BTC (~$820k) hedge the book walks; a flat half-spread hides that.
    (The recording holds ~10 levels, about $1M of Hyperliquid BTC per side, so
    a much larger hedge is beyond what was measured -- see the next test.)"""
    config = BacktestConfig(source_key="PAPER_DELTA:BTCUSD", hedge_key="PAPER_HYPERLIQUID:BTC",
                            source_quantity=D(10_000))
    flat = Backtester(registry.all(), history, fx).run(config)
    walked = Backtester(registry.all(), history, fx, impact_curves).run(config)
    assert "recorded books" in walked.execution_model
    assert "half the spread" in flat.execution_model
    assert walked.total_slippage > flat.total_slippage
    assert walked.unmeasured_trades == 0       # inside the recorded depth


def test_a_hedge_beyond_the_recorded_depth_is_counted(registry, history, fx, impact_curves) -> None:
    config = BacktestConfig(source_key="PAPER_DELTA:PAXGUSD", hedge_key="PAPER_HYPERLIQUID:PAXG",
                            source_quantity=D(100_000))   # 100 PAXG, ~$400k
    report = Backtester(registry.all(), history, fx, impact_curves).run(config)
    assert report.unmeasured_trades >= 1
