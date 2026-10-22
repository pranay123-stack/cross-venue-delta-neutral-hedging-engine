"""Real market feed, stress overlays, FX, funding and P&L attribution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import make_cfd, make_perp

from hedgelab.costs.funding import project_funding
from hedgelab.costs.pnl import LegAccounting, PnLCalculator
from hedgelab.domain.enums import MarketScenario
from hedgelab.domain.market import MarketSnapshot, OrderBook, OrderBookLevel, Ticker
from hedgelab.domain.numeric import (
    NumericError,
    ceil_to_step,
    dec,
    decimal_places,
    floor_to_step,
    from_bps,
    nearest_step,
    normalize,
    round_to_step,
    safe_div,
)
from hedgelab.domain.orders import Fill, Position
from hedgelab.marketdata.feed import MarketFeed, NoMarketData
from hedgelab.marketdata.frames import MarketFrame
from hedgelab.marketdata.fx import FxRateUnavailable, FxService

D = Decimal


# ======================================================================
# numeric primitives
# ======================================================================
def test_float_input_does_not_leak_binary_error() -> None:
    assert dec(0.1) == D("0.1")
    assert dec(0.1) + dec(0.2) == D("0.3")


def test_bool_is_not_a_number() -> None:
    with pytest.raises(NumericError):
        dec(True)


def test_unparseable_input_is_rejected() -> None:
    with pytest.raises(NumericError):
        dec("not a number")


def test_out_of_range_input_is_rejected() -> None:
    with pytest.raises(NumericError):
        dec("1e30")


@pytest.mark.parametrize(
    ("value", "step", "down", "up", "near"),
    [
        ("1.27", "0.1", "1.2", "1.3", "1.3"),
        ("-1.27", "0.1", "-1.3", "-1.2", "-1.3"),
        ("5", "2", "4", "6", "6"),
    ],
)
def test_step_rounding_directions(value, step, down, up, near) -> None:
    assert floor_to_step(D(value), D(step)) == D(down)
    assert ceil_to_step(D(value), D(step)) == D(up)
    assert nearest_step(D(value), D(step)) == D(near)


def test_round_to_step_toward_zero_is_symmetric() -> None:
    assert round_to_step(D("1.27"), D("0.1")) == D("1.2")
    assert round_to_step(D("-1.27"), D("0.1")) == D("-1.2")


def test_zero_step_is_rejected() -> None:
    with pytest.raises(NumericError):
        round_to_step(D("1"), D("0"))


def test_safe_div_returns_the_default_on_zero() -> None:
    assert safe_div(D("1"), D("0")) == D("0")
    assert safe_div(D("1"), D("0"), D("-1")) == D("-1")


def test_decimal_places_from_step() -> None:
    assert decimal_places(D("0.001")) == 3
    assert decimal_places(D("1")) == 0
    assert decimal_places(D("100")) == 0


def test_normalize_keeps_integers_readable() -> None:
    assert str(normalize(D("50.00"))) == "50"
    assert str(normalize(D("0.500"))) == "0.5"


def test_bps_conversion_round_trips() -> None:
    assert from_bps(D("5")) == D("0.0005")


# ======================================================================
# tickers and books
# ======================================================================
def _ticker(bid: str, ask: str) -> Ticker:
    return Ticker(venue="V", symbol="S", bid=D(bid), ask=D(ask), last=D(bid),
                  volume=D("1"), timestamp=datetime.now(UTC))


def test_ticker_derives_mid_and_spread() -> None:
    ticker = _ticker("99", "101")
    assert ticker.mid == D("100")
    assert ticker.spread == D("2")
    assert ticker.spread_bps == D("200")


def test_ticker_price_for_side() -> None:
    ticker = _ticker("99", "101")
    assert ticker.price_for(is_buy=True) == D("101")
    assert ticker.price_for(is_buy=False) == D("99")


def test_book_sweep_returns_partial_when_depth_runs_out() -> None:
    book = OrderBook(
        venue="V", symbol="S", bids=(),
        asks=(OrderBookLevel(D("100"), D("2")), OrderBookLevel(D("101"), D("3"))),
        timestamp=datetime.now(UTC),
    )
    filled, vwap = book.sweep(True, D("10"))
    assert filled == D("5")
    assert vwap == (D("2") * D("100") + D("3") * D("101")) / D("5")


def test_book_sweep_on_an_empty_side_returns_zero() -> None:
    book = OrderBook(venue="V", symbol="S", bids=(), asks=(), timestamp=datetime.now(UTC))
    assert book.sweep(True, D("1")) == (D("0"), D("0"))


def test_snapshot_requires_known_keys() -> None:
    snapshot = MarketSnapshot()
    with pytest.raises(KeyError):
        snapshot.require_ticker("V:S")


# ======================================================================
# the real market feed (replaying a recorded live session)
# ======================================================================
DELTA_BTC = "PAPER_DELTA:BTCUSD"
HL_BTC = "PAPER_HYPERLIQUID:BTC"


def test_replay_is_reproducible(registry, frames) -> None:
    a = MarketFeed(registry.all(), frames=frames, source="replay")
    b = MarketFeed(registry.all(), frames=frames, source="replay")
    a.advance(150)
    b.advance(150)
    assert a.state_digest() == b.state_digest()
    assert a.clock == b.clock == frames[150].timestamp


def test_replay_walks_the_recording_in_order_and_stops_at_the_end(registry, frames) -> None:
    feed = MarketFeed(registry.all(), frames=frames, source="replay")
    assert feed.clock == frames[0].timestamp
    assert feed.advance(len(frames) + 10) == len(frames) - 1
    assert feed.clock == frames[-1].timestamp
    assert feed.advance(1) == 0          # nothing is invented past the end


def test_the_feed_serves_the_recorded_book_unchanged(market: MarketFeed, frames) -> None:
    raw = frames[20].quotes[HL_BTC]
    book = market.orderbook(HL_BTC, 5)
    assert [(lvl.price, lvl.size) for lvl in book.asks] == list(raw.asks[:5])
    assert [(lvl.price, lvl.size) for lvl in book.bids] == list(raw.bids[:5])
    ticker = market.ticker(HL_BTC)
    assert (ticker.bid, ticker.ask) == (raw.bids[0][0], raw.asks[0][0])
    assert ticker.funding_rate == raw.funding_rate


def test_legs_on_different_venues_have_a_real_basis(market: MarketFeed) -> None:
    """The same coin on two venues: close, never identical."""
    delta = market.ticker(DELTA_BTC)
    hl = market.ticker(HL_BTC)
    assert delta.mid != hl.mid
    assert abs(delta.mid - hl.mid) / hl.mid < D("0.002")


def test_prices_respect_the_tick_lattice(market: MarketFeed, registry) -> None:
    for spec in registry.all():
        ticker = market.ticker(spec.key)
        assert (ticker.bid / spec.tick_size) % 1 == 0, spec.key
        assert (ticker.ask / spec.tick_size) % 1 == 0, spec.key
        assert ticker.ask > ticker.bid


def test_spread_widening_overlay_reaches_its_floor(market: MarketFeed) -> None:
    before = market.ticker(HL_BTC).spread_bps
    market.set_scenario(MarketScenario.SPREAD_WIDENING)
    after = market.ticker(HL_BTC).spread_bps
    # Real BTC spreads are a fraction of a basis point; x20 alone would not
    # get anywhere near a dislocated market, so the overlay has a floor.
    assert before < D("1")
    assert after >= D("100")


def test_frozen_feed_stops_updating_and_goes_stale(market: MarketFeed) -> None:
    market.set_scenario(MarketScenario.STALE_DATA)
    first = market.ticker(HL_BTC)
    market.advance(20)
    second = market.ticker(HL_BTC)
    assert first.timestamp == second.timestamp
    assert first.mid == second.mid
    assert second.is_stale is True


def test_a_venue_missing_from_a_poll_is_flagged_stale(registry, frames) -> None:
    """When a venue stops answering, its last real quote is kept and flagged --
    never replaced with a guess."""
    # A real poll ~60s later, with Hyperliquid's answer withheld as if the
    # venue had not responded.
    later = next(f for f in frames if f.timestamp - frames[0].timestamp > timedelta(seconds=60))
    outage = MarketFrame(
        timestamp=later.timestamp,
        quotes={k: q for k, q in later.quotes.items() if not k.startswith("PAPER_HYPER")},
        missing=tuple(k for k in later.quotes if k.startswith("PAPER_HYPER")),
    )
    feed = MarketFeed(registry.all(), frames=[frames[0], outage], source="replay")
    before = feed.ticker(HL_BTC)
    feed.advance(1)
    after = feed.ticker(HL_BTC)
    assert after.is_stale is True
    assert after.mid == before.mid
    assert feed.ticker(DELTA_BTC).is_stale is False


def test_disconnect_overlay_is_reported(market: MarketFeed) -> None:
    market.set_scenario(MarketScenario.EXCHANGE_DISCONNECT, venue="PAPER_HYPERLIQUID")
    assert market.is_disconnected("PAPER_HYPERLIQUID") is True
    assert market.is_disconnected("PAPER_DELTA") is False


def test_overlay_can_be_scoped_to_one_venue(market: MarketFeed) -> None:
    market.set_scenario(MarketScenario.SPREAD_WIDENING, venue="PAPER_HYPERLIQUID")
    assert market.ticker(HL_BTC).spread_bps > D("50")
    assert market.ticker(DELTA_BTC).spread_bps < D("10")


def test_liquidity_reduction_thins_the_real_book(market: MarketFeed) -> None:
    before = market.ticker(HL_BTC).bid_size
    market.set_scenario(MarketScenario.LIQUIDITY_REDUCTION)
    after = market.ticker(HL_BTC).bid_size
    assert after < before


def test_shock_moves_every_venue_on_the_underlying(market: MarketFeed) -> None:
    before_delta = market.ticker(DELTA_BTC).mid
    before_hl = market.ticker(HL_BTC).mid
    market.apply_shock("BTC", D("-0.1"))
    # Within one tick of exactly -10% (bids floor, asks ceil to the lattice).
    assert abs(market.ticker(DELTA_BTC).mid - before_delta * D("0.9")) <= D("0.5")
    assert abs(market.ticker(HL_BTC).mid - before_hl * D("0.9")) <= D("1")


def test_clearing_overlays_returns_to_the_real_market(market: MarketFeed) -> None:
    real = market.ticker(HL_BTC)
    market.apply_shock("BTC", D("-0.25"))
    market.set_funding_rate(HL_BTC, D("0.01"))
    market.set_scenario(MarketScenario.SPREAD_WIDENING)
    market.clear_scenario()
    restored = market.ticker(HL_BTC)
    assert (restored.bid, restored.ask) == (real.bid, real.ask)
    assert restored.funding_rate == real.funding_rate
    assert market.active_overlays()["price_shocks"] == {}


def test_unknown_underlying_shock_is_rejected(market: MarketFeed) -> None:
    with pytest.raises(KeyError):
        market.apply_shock("NOTATHING", D("0.1"))


def test_an_instrument_without_real_data_is_never_priced(market: MarketFeed, registry) -> None:
    """The old simulator synthesised a price for anything; the feed refuses."""
    from hedgelab.domain.instrument import InstrumentSpec

    unseen = InstrumentSpec.model_validate({
        "symbol": "AVAX", "venue": "PAPER_HYPERLIQUID", "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL", "base_asset": "AVAX", "quote_asset": "USDC",
        "settlement_asset": "USDC", "quantity_unit": "BASE_UNIT",
        "price_source": "hyperliquid",
    })
    market.add_instrument(unseen)
    with pytest.raises(NoMarketData):
        market.ticker(unseen.key)
    assert unseen.key not in market.tickers()


def test_every_perpetual_publishes_its_real_funding_rate(market: MarketFeed, registry) -> None:
    for spec in registry.all():
        assert market.ticker(spec.key).funding_rate is not None, spec.key


def test_the_recording_carries_measured_fx(frames) -> None:
    rate = frames[0].fx[("USDC", "USD")]
    assert D("0.99") < rate < D("1.01")
    assert rate != D(1)                   # measured, not an assumed peg


# ======================================================================
# FX
# ======================================================================
def test_fx_identity_is_one(fx: FxService) -> None:
    assert fx.rate("USD", "USD") == D("1")


def test_fx_inverse_is_derived(fx: FxService) -> None:
    forward = fx.rate("USDC", "USD")
    assert abs(fx.rate("USD", "USDC") * forward - D("1")) < D("1e-12")


def test_fx_triangulates_through_the_pivot(fx: FxService) -> None:
    expected = fx.rate("USDC", "USD") * fx.rate("USD", "USDT")
    assert fx.rate("USDC", "USDT") == expected


def test_no_rate_is_assumed_before_one_is_measured() -> None:
    with pytest.raises(FxRateUnavailable):
        FxService().rate("USDC", "USD")


def test_missing_fx_path_raises_on_the_execution_path(fx: FxService) -> None:
    with pytest.raises(FxRateUnavailable):
        fx.rate("ZZZ", "INR")


def test_missing_fx_path_degrades_on_the_presentation_path(fx: FxService) -> None:
    assert fx.try_rate("ZZZ", "INR") == D("1")


def test_negative_fx_rate_is_rejected(fx: FxService) -> None:
    with pytest.raises(ValueError):
        fx.set_rate("USD", "XYZ", D("-1"))


# ======================================================================
# funding projection
# ======================================================================
def test_projection_signs_match_who_pays() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1", swap_long="-100", swap_short="-40")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd,
        source_quantity=D("10"), hedge_quantity=D("-10"),
        source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert projection.source_per_day < 0        # long pays a positive rate
    assert projection.hedge_per_day < 0         # short pays swap
    assert projection.net_per_day == projection.source_per_day + projection.hedge_per_day
    assert projection.is_positive_carry is False


def test_short_source_receives_funding() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd,
        source_quantity=D("-10"), hedge_quantity=D("10"),
        source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert projection.source_per_day > 0


def test_projection_scales_with_the_horizon() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd, source_quantity=D("10"),
        hedge_quantity=D("-10"), source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"), horizon_days=D("7"),
    )
    assert projection.net_over_horizon == projection.net_per_day * 7


def test_projection_explains_itself() -> None:
    perp = make_perp(contract_size="1")
    cfd = make_cfd(units_per_lot="1")
    projection = project_funding(
        source_spec=perp, hedge_spec=cfd, source_quantity=D("10"),
        hedge_quantity=D("-10"), source_price=D("1000"), hedge_price=D("1000"),
        source_funding_rate=D("0.0001"),
    )
    assert "funding" in projection.explanation
    assert "swap" in projection.explanation


# ======================================================================
# P&L attribution
# ======================================================================
def _legs():
    source = make_perp(contract_size="0.001")
    hedge = make_cfd(units_per_lot="1")
    source_leg = LegAccounting(
        spec=source,
        position=Position(venue=source.venue, symbol=source.symbol,
                          quantity=D("5000"), average_entry=D("100000")),
        funding_paid=D("120"),
    )
    hedge_leg = LegAccounting(
        spec=hedge,
        position=Position(venue=hedge.venue, symbol=hedge.symbol,
                          quantity=D("-5"), average_entry=D("100050")),
        swap_financing=D("-90"),
    )
    source_leg.add_fill(Fill(order_id="a", quantity=D("5000"), price=D("100000"),
                             fee=D("250"), is_maker=False, slippage=D("5")))
    hedge_leg.add_fill(Fill(order_id="b", quantity=D("5"), price=D("100050"),
                            fee=D("0"), is_maker=False, slippage=D("-8")))
    return source_leg, hedge_leg


def _tick(venue: str, symbol: str, mid: str) -> Ticker:
    m = D(mid)
    return Ticker(venue=venue, symbol=symbol, bid=m - D("5"), ask=m + D("5"),
                  last=m, volume=D("1"), timestamp=datetime.now(UTC))


def test_components_sum_to_the_net(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.check_consistency() is True
    total = sum(c.amount for c in breakdown.components)
    assert abs(total - breakdown.net_pnl) < D("1e-9")


def test_costs_are_negative_and_separated(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.trading_fees < 0
    assert breakdown.spread_cost < 0
    assert breakdown.total_costs == (
        breakdown.trading_fees + breakdown.spread_cost + breakdown.slippage_cost
    )


def test_net_funding_combines_both_legs(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.net_funding == D("-120") + D("-90")


def test_fx_impact_is_separated_from_price_pnl(fx: FxService) -> None:
    """The USDT leg's conversion effect is its own line, not hidden in price."""
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.fx_conversion_impact != 0
    assert any(c.name == "fx_conversion" for c in breakdown.components)


def test_break_even_cost_is_what_the_book_must_earn(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert breakdown.break_even_cost > 0
    assert breakdown.to_dict()["consistent"] is True


def test_every_component_carries_an_explanation(fx: FxService) -> None:
    source_leg, hedge_leg = _legs()
    breakdown = PnLCalculator(fx, "USD").for_pair(
        scope="pair", source=source_leg, hedge=hedge_leg,
        source_ticker=_tick(source_leg.spec.venue, source_leg.spec.symbol, "102000"),
        hedge_ticker=_tick(hedge_leg.spec.venue, hedge_leg.spec.symbol, "102000"),
    )
    assert all(c.explanation for c in breakdown.components)
