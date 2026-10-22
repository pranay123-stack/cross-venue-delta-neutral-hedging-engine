"""Property-based tests: invariants that must hold for *every* input.

The table-driven tests check chosen examples; these let Hypothesis search for
the rounding edge, the position flip or the overlay sequence nobody thought
to write down.  The instrument specs are the real generated catalogue, and the
market-feed properties run over the real recorded books, so the search is over
sizes, prices and event sequences -- not over invented markets.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import LIVE_FRAMES, SPEC_DIR

from hedgelab.domain.enums import MarketScenario
from hedgelab.domain.market import OrderBook, OrderBookLevel
from hedgelab.domain.orders import Position
from hedgelab.domain.quantity import QuantityConverter
from hedgelab.instruments.registry import InstrumentRegistry
from hedgelab.marketdata.feed import MarketFeed
from hedgelab.marketdata.frames import load_frames
from hedgelab.marketdata.stats import RollingStats

D = Decimal
REGISTRY = InstrumentRegistry.from_directory(SPEC_DIR)
SPECS = REGISTRY.all()
PAIRS = [(REGISTRY.get(m.source_key), REGISTRY.get(m.hedge_key)) for m in REGISTRY.mappings()]
FRAMES = load_frames(LIVE_FRAMES)

PROPERTY = settings(max_examples=300, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])

prices = st.decimals(min_value="0.5", max_value="250000", places=4,
                     allow_nan=False, allow_infinity=False)
quantities = st.decimals(min_value="-10000000", max_value="10000000", places=6,
                         allow_nan=False, allow_infinity=False)


# ======================================================================
# lattice rounding
# ======================================================================
@PROPERTY
@given(spec=st.sampled_from(SPECS), raw=quantities)
def test_rounding_always_lands_on_the_venue_lattice(spec, raw: Decimal) -> None:
    result = QuantityConverter(spec).round_quantity(raw)
    rounded = result.rounded
    assert (rounded / spec.quantity_step) % 1 == 0
    assert abs(rounded) <= spec.max_quantity
    assert rounded == 0 or abs(rounded) >= spec.min_quantity
    # Never flips the direction of the trade.
    assert rounded == 0 or (rounded > 0) == (raw > 0)


@PROPERTY
@given(spec=st.sampled_from(SPECS), raw=quantities)
def test_rounding_never_trades_more_than_asked(spec, raw: Decimal) -> None:
    """Rounding toward zero: an over-hedge creates exposure nobody asked for."""
    rounded = QuantityConverter(spec).round_quantity(raw).rounded
    assert abs(rounded) <= abs(raw)


# ======================================================================
# unit conversion
# ======================================================================
@PROPERTY
@given(spec=st.sampled_from(SPECS), qty=quantities, price=prices)
def test_base_unit_conversion_round_trips(spec, qty: Decimal, price: Decimal) -> None:
    converter = QuantityConverter(spec)
    back = converter.quantity_from_base_units(converter.base_units(qty, price), price)
    assert abs(back - qty) <= abs(qty) * D("1e-20") + D("1e-20")


@PROPERTY
@given(pair=st.sampled_from(PAIRS), reverse=st.booleans(), steps=st.integers(1, 10_000_000),
       price=prices)
def test_a_coin_matched_hedge_never_over_hedges(pair, reverse: bool, steps: int,
                                                price: Decimal) -> None:
    """For every shipped pair, in both directions: the rounded hedge holds no
    more coins than the source, and misses by less than one hedge step.

    Delta -> Hyperliquid never actually rounds (0.001 BTC per contract always
    lands on Hyperliquid's 0.00001 lattice); Hyperliquid -> Delta does, since
    0.00037 BTC is not a whole number of 0.001 BTC contracts.  Both are held to
    the same rule.
    """
    source, hedge = pair[::-1] if reverse else pair
    position = source.quantity_step * steps
    if position > source.max_quantity:
        return
    base = QuantityConverter(source).base_units(position, price)
    hedge_conv = QuantityConverter(hedge)
    required = -hedge_conv.quantity_from_base_units(base, price)
    rounded = hedge_conv.round_quantity(required).rounded
    hedged = abs(hedge_conv.base_units(rounded, price))
    assert hedged <= base
    if hedge.min_quantity <= abs(required) <= hedge.max_quantity:
        assert base - hedged < hedge.quantity_step * hedge.units_per_quantity


# ======================================================================
# position accounting
# ======================================================================
fills = st.lists(
    st.tuples(
        st.decimals(min_value="-50", max_value="50", places=3,
                    allow_nan=False, allow_infinity=False).filter(lambda q: q != 0),
        st.decimals(min_value="1000", max_value="200000", places=1,
                    allow_nan=False, allow_infinity=False),
    ),
    min_size=1, max_size=30,
)


@PROPERTY
@given(trades=fills, mark=st.decimals(min_value="1000", max_value="200000", places=1,
                                      allow_nan=False, allow_infinity=False))
def test_realised_plus_unrealised_equals_the_cash_flows(trades, mark: Decimal) -> None:
    """Whatever sequence of increases, reductions and flips: the books balance."""
    position = Position(venue="V", symbol="S")
    cash = D(0)
    for qty, price in trades:
        position.apply(qty, price)
        cash -= qty * price
    marked = cash + position.quantity * mark
    accounted = position.realized_pnl + position.unrealized_pnl_linear(mark, D(1))
    assert abs(marked - accounted) <= D("1e-12") * (abs(marked) + 1)


@PROPERTY
@given(notional=st.decimals(min_value="1", max_value="10000000", places=0,
                            allow_nan=False, allow_infinity=False),
       entry=prices, mark=prices)
def test_inverse_pnl_matches_the_closed_form(notional: Decimal, entry: Decimal, mark: Decimal) -> None:
    """PnL_quote = N x (S/E - 1) for a long inverse position of N quote units."""
    position = Position(venue="V", symbol="S", quantity=notional, average_entry=entry)
    pnl = position.unrealized_pnl_inverse(mark, D(1))
    expected = notional * (mark / entry - 1)
    assert abs(pnl - expected) <= abs(expected) * D("1e-20") + D("1e-12")


# ======================================================================
# book sweeps
# ======================================================================
@PROPERTY
@given(frame=st.sampled_from(FRAMES), key=st.sampled_from(sorted(FRAMES[0].quotes)),
       is_buy=st.booleans(), fraction=st.decimals(min_value="0.0001", max_value="2", places=4,
                                                  allow_nan=False, allow_infinity=False))
def test_sweeping_a_real_book_is_bounded_by_its_levels(frame, key, is_buy, fraction) -> None:
    quote = frame.quotes[key]
    side = quote.asks if is_buy else quote.bids
    book = OrderBook(
        venue="V", symbol="S",
        bids=tuple(OrderBookLevel(p, s) for p, s in quote.bids),
        asks=tuple(OrderBookLevel(p, s) for p, s in quote.asks),
        timestamp=frame.timestamp,
    )
    total = sum((s for _, s in side), D(0))
    wanted = total * fraction
    filled, vwap = book.sweep(is_buy, wanted)
    assert filled == min(wanted, total)
    prices_taken = [p for p, _ in side]
    assert min(prices_taken) <= vwap <= max(prices_taken)
    # Buying walks up the book, selling walks down: never better than the touch.
    assert (vwap >= side[0][0]) if is_buy else (vwap <= side[0][0])


# ======================================================================
# stress overlays on the real feed
# ======================================================================
events = st.lists(
    st.one_of(
        st.tuples(st.just("shock"), st.sampled_from(["BTC", "ETH", "SOL", "PAXG"]),
                  st.decimals(min_value="-0.5", max_value="0.5", places=3,
                              allow_nan=False, allow_infinity=False)),
        st.tuples(st.just("scenario"),
                  st.sampled_from([MarketScenario.SPREAD_WIDENING,
                                   MarketScenario.LIQUIDITY_REDUCTION,
                                   MarketScenario.HIGH_VOLATILITY]),
                  st.sampled_from([None, "PAPER_DELTA", "PAPER_HYPERLIQUID"])),
        st.tuples(st.just("advance"), st.integers(1, 5), st.none()),
    ),
    max_size=12,
)


@PROPERTY
@given(script=events)
def test_stressed_books_stay_valid_and_clearing_restores_the_real_market(script) -> None:
    feed = MarketFeed(SPECS, frames=FRAMES, source="replay", seed=7)
    feed.advance(10)
    for kind, a, b in script:
        if kind == "shock":
            feed.apply_shock(a, b)
        elif kind == "scenario":
            feed.set_scenario(a, b)
        else:
            feed.advance(a)
        for spec in SPECS:
            ticker = feed.ticker(spec.key)
            assert ticker.ask > ticker.bid > 0, spec.key
            assert (ticker.bid / spec.tick_size) % 1 == 0, spec.key
            assert (ticker.ask / spec.tick_size) % 1 == 0, spec.key

    feed.clear_scenario()
    position = feed.replay_position[0] - 1
    for spec in SPECS:
        raw = FRAMES[position].quotes[spec.key]
        ticker = feed.ticker(spec.key)
        assert (ticker.bid, ticker.ask) == (raw.bids[0][0], raw.asks[0][0]), spec.key


# ======================================================================
# statistics
# ======================================================================
@PROPERTY
@given(returns=st.lists(
    st.tuples(st.floats(-0.05, 0.05, allow_nan=False), st.floats(-0.05, 0.05, allow_nan=False)),
    min_size=40, max_size=200,
))
def test_correlation_is_bounded_and_volatility_non_negative(returns) -> None:
    stats = RollingStats(window=500, sample_seconds=60.0, min_samples=30)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    a = b = 100.0
    for step, (ra, rb) in enumerate(returns):
        a *= math.exp(ra)
        b *= math.exp(rb)
        stats.observe_many({"A": D(repr(a)), "B": D(repr(b))},
                           start + timedelta(seconds=60 * step))
    rho, _ = stats.correlation("A", "B")
    assert D(-1) <= rho <= D(1)
    assert stats.estimate("A").daily_volatility >= 0
