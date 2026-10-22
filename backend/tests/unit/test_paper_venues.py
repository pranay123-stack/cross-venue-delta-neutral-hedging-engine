"""Paper venue adapters: order lifecycle, margin, funding, faults and safety.

Every fill here walks a *real* order book -- Delta Exchange or Hyperliquid --
from the committed recording of a live session.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from hedgelab.config import Settings
from hedgelab.domain.enums import (
    FaultKind,
    Leg,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    TradingMode,
)
from hedgelab.domain.orders import Fill, Order, OrderRequest, Position
from hedgelab.faults.injector import FaultInjector
from hedgelab.marketdata.feed import MarketFeed
from hedgelab.marketdata.fx import FxService
from hedgelab.venues.base import (
    InsufficientMargin,
    LiveTradingDisabledError,
    OrderRejected,
    VenueDisconnected,
    VenueTimeout,
)
from hedgelab.venues.factory import LIVE_ADAPTERS, PAPER_ADAPTERS, VenueFactory
from hedgelab.venues.live_stubs import DeltaAdapter, HyperliquidAdapter

D = Decimal
DELTA = "PAPER_DELTA"
HL = "PAPER_HYPERLIQUID"


@pytest.fixture
def venues(registry, market: MarketFeed, fx: FxService, faults: FaultInjector,
           settings: Settings):
    specs = {s.key: s for s in registry.all()}
    factory = VenueFactory(settings=settings, instruments=specs, market=market,
                           fx=fx, faults=faults)
    return factory.create_all()


def factory_with(registry, market, fx, faults, settings: Settings) -> VenueFactory:
    return VenueFactory(settings=settings, instruments={s.key: s for s in registry.all()},
                        market=market, fx=fx, faults=faults)


def order(venue: str, symbol: str, side: Side, qty: str, **kwargs) -> OrderRequest:
    return OrderRequest(venue=venue, symbol=symbol, side=side, quantity=D(qty), **kwargs)


# ======================================================================
# safety: no live path exists
# ======================================================================
def test_only_paper_adapters_are_registered() -> None:
    assert LIVE_ADAPTERS == {}
    assert set(PAPER_ADAPTERS) == {DELTA, HL}
    assert all(cls.is_paper for cls in PAPER_ADAPTERS.values())


def test_factory_refuses_live_mode(registry, market, fx, faults, settings) -> None:
    live = settings.model_copy(update={"trading_mode": TradingMode.LIVE})
    with pytest.raises(LiveTradingDisabledError, match="paper-only"):
        factory_with(registry, market, fx, faults, live).create(DELTA)


def test_factory_refuses_live_even_with_allow_live_set(
    registry, market, fx, faults, settings
) -> None:
    """The flag alone is not enough: there is nothing registered to build."""
    live = settings.model_copy(update={"trading_mode": TradingMode.LIVE, "allow_live": True})
    with pytest.raises(LiveTradingDisabledError):
        factory_with(registry, market, fx, faults, live).create(HL)


@pytest.mark.parametrize("adapter", [DeltaAdapter, HyperliquidAdapter])
def test_live_adapters_cannot_be_constructed(adapter: type) -> None:
    with pytest.raises(LiveTradingDisabledError, match=r"paper-trading only|no live trading"):
        adapter()


def test_every_paper_order_is_marked_paper(venues) -> None:
    assert order(DELTA, "BTCUSD", Side.BUY, "10").is_paper is True


def test_unknown_venue_is_rejected(registry, market, fx, faults, settings) -> None:
    with pytest.raises(KeyError, match="no paper adapter"):
        factory_with(registry, market, fx, faults, settings).create("BINANCE")


# ======================================================================
# order lifecycle
# ======================================================================
async def test_market_order_fills_and_moves_the_position(venues) -> None:
    delta = venues[DELTA]
    result = await delta.place_order(order(DELTA, "BTCUSD", Side.BUY, "1000"))
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == D("1000")
    book_ask = (await delta.get_orderbook("BTCUSD", 1)).best_ask
    assert result.average_price >= book_ask       # a taker buy pays the real ask

    positions = await delta.get_positions()
    assert positions[0].quantity == D("1000")


async def test_selling_reduces_and_realises(venues) -> None:
    delta = venues[DELTA]
    await delta.place_order(order(DELTA, "BTCUSD", Side.BUY, "1000"))
    before = (await delta.get_balance()).balance
    await delta.place_order(order(DELTA, "BTCUSD", Side.SELL, "1000"))
    assert await delta.get_positions() == []     # flat positions are filtered out
    assert (await delta.get_balance()).balance != before


async def test_each_venue_charges_its_own_published_taker_fee(venues) -> None:
    """Delta 5 bps, Hyperliquid 4.5 bps -- both explicit commission."""
    delta = await venues[DELTA].place_order(order(DELTA, "BTCUSD", Side.BUY, "1000"))
    hl = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "1"))
    # 1000 contracts x 0.001 BTC and 1 BTC: the same exposure, two fee schedules.
    delta_notional = delta.filled_quantity * D("0.001") * delta.average_price
    hl_notional = hl.filled_quantity * hl.average_price
    assert abs(delta.fees_paid - delta_notional * D("0.0005")) < D("0.01")
    # Hyperliquid settles in USDC; the fee is converted at the measured rate.
    assert abs(hl.fees_paid - hl_notional * D("0.00045")) < hl_notional * D("0.0000005")


async def test_order_to_the_wrong_venue_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="wrong venue"):
        await venues[DELTA].place_order(order(HL, "BTC", Side.BUY, "1"))


async def test_unknown_symbol_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="unknown symbol"):
        await venues[DELTA].place_order(order(DELTA, "DOGEUSD", Side.BUY, "1"))


async def test_off_step_quantity_is_rejected(venues) -> None:
    # BTC trades in 0.00001 coins on Hyperliquid (szDecimals = 5).
    with pytest.raises(OrderRejected, match="not a multiple of step"):
        await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.100015"))


async def test_below_minimum_quantity_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="below minimum"):
        await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.000001"))


async def test_orders_under_ten_dollars_are_rejected_on_hyperliquid(venues) -> None:
    # 0.0001 BTC is a valid size but worth about $8.
    with pytest.raises(OrderRejected, match=r"order value .* below minimum 10"):
        await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.0001"))


async def test_reducing_orders_are_exempt_from_the_minimum_value(venues) -> None:
    """A dust remainder must always be closable."""
    hl = venues[HL]
    await hl.place_order(order(HL, "BTC", Side.BUY, "0.0005"))      # ~$41
    closing = await hl.place_order(order(HL, "BTC", Side.SELL, "0.0001"))  # ~$8
    assert closing.status is OrderStatus.FILLED


async def test_above_maximum_quantity_is_rejected(venues) -> None:
    # Delta caps BTCUSD positions at 125,000 contracts (position_size_limit).
    # The venue's own rule, so ask the venue behind the hard-limit guard.
    with pytest.raises(OrderRejected, match="above maximum"):
        await venues[DELTA].inner.place_order(order(DELTA, "BTCUSD", Side.BUY, "200000"))


async def test_off_tick_limit_price_is_rejected(venues) -> None:
    with pytest.raises(OrderRejected, match="not a multiple of tick"):
        await venues[DELTA].place_order(
            OrderRequest(venue=DELTA, symbol="BTCUSD", side=Side.BUY,
                         quantity=D("10"), order_type=OrderType.LIMIT,
                         price=D("80000.13")),
        )


async def test_zero_quantity_order_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        OrderRequest(venue=HL, symbol="BTC", side=Side.BUY, quantity=D("0"))


async def test_limit_order_requires_a_price() -> None:
    with pytest.raises(ValueError, match="require a price"):
        OrderRequest(venue=HL, symbol="BTC", side=Side.BUY,
                     quantity=D("1"), order_type=OrderType.LIMIT)


async def test_unmarketable_limit_order_rests_unfilled(venues) -> None:
    hl = venues[HL]
    ticker = await hl.get_ticker("BTC")
    far_below = (ticker.bid * D("0.5")).quantize(D("1"))
    result = await hl.place_order(OrderRequest(
        venue=HL, symbol="BTC", side=Side.BUY, quantity=D("1"),
        order_type=OrderType.LIMIT, price=far_below,
    ))
    assert result.filled_quantity == D("0")
    assert result.status is OrderStatus.SUBMITTED
    assert result in await hl.get_open_orders()


async def test_insufficient_margin_is_rejected(registry, market, fx, faults, settings) -> None:
    poor = settings.model_copy(update={"paper_starting_balance": D("500")})
    hl = factory_with(registry, market, fx, faults, poor).create(HL)
    # 1 BTC at 40x needs ~$2,000 of initial margin.
    with pytest.raises(InsufficientMargin):
        await hl.place_order(order(HL, "BTC", Side.BUY, "1"))


async def test_closing_orders_are_never_blocked_by_margin(
    registry, market, fx, faults, settings
) -> None:
    """De-risking must always be possible, even at the margin limit."""
    tight = settings.model_copy(update={"paper_starting_balance": D("2200")})
    hl = factory_with(registry, market, fx, faults, tight).create(HL)
    await hl.place_order(order(HL, "BTC", Side.BUY, "1"))
    # Free margin is now nearly gone, but the closing order still goes through.
    closing = await hl.place_order(order(HL, "BTC", Side.SELL, "1"))
    assert closing.status is OrderStatus.FILLED


# ======================================================================
# fills, duplicates and slippage
# ======================================================================
def test_duplicate_execution_reports_do_not_double_count() -> None:
    request = OrderRequest(venue="V", symbol="S", side=Side.BUY, quantity=D("10"))
    order_obj = Order(order_id="o1", request=request)
    fill = Fill(order_id="o1", quantity=D("4"), price=D("100"), fee=D("1"), is_maker=False)
    order_obj.apply_fill(fill)
    order_obj.apply_fill(fill)          # replayed report
    assert order_obj.filled_quantity == D("4")
    assert order_obj.fees_paid == D("1")
    assert len(order_obj.fills) == 1


def test_average_price_is_quantity_weighted() -> None:
    request = OrderRequest(venue="V", symbol="S", side=Side.BUY, quantity=D("10"))
    order_obj = Order(order_id="o1", request=request)
    order_obj.apply_fill(Fill(order_id="o1", quantity=D("2"), price=D("100"),
                              fee=D("0"), is_maker=False))
    order_obj.apply_fill(Fill(order_id="o1", quantity=D("8"), price=D("110"),
                              fee=D("0"), is_maker=False))
    assert order_obj.average_price == D("108")
    assert order_obj.status is OrderStatus.FILLED


async def test_slippage_is_recorded_against_the_reference_price(venues) -> None:
    result = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.1"))
    assert result.fills[0].slippage > 0      # a buy fills above the real mid


async def test_large_orders_walk_the_real_book(venues) -> None:
    """Slippage emerges from the depth that was really there."""
    hl = venues[HL]
    small = await hl.place_order(order(HL, "BTC", Side.BUY, "0.1"))
    book = await hl.get_orderbook("BTC", 10)
    big_qty = book.asks[0].size + book.asks[1].size / D("2")
    filled, vwap = book.sweep(True, big_qty)
    assert filled == big_qty
    assert vwap > book.best_ask
    assert small.average_price <= vwap


async def test_an_order_larger_than_the_real_book_fills_only_what_was_there(venues) -> None:
    hl = venues[HL]
    book = await hl.get_orderbook("BTC", 20)
    available = book.total_size(True)
    result = await hl.place_order(order(HL, "BTC", Side.BUY, str(available + D(15))))
    assert result.filled_quantity == available
    # IOC: the remainder is cancelled, not left working.
    assert result.status is OrderStatus.CANCELLED


# ======================================================================
# position arithmetic
# ======================================================================
def test_position_flip_realises_the_whole_old_leg() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    realized = position.apply(D("-15"), D("110"))
    assert realized == D("100")            # 10 units x 10 profit
    assert position.quantity == D("-5")
    assert position.average_entry == D("110")


def test_position_average_updates_on_increase() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    position.apply(D("10"), D("120"))
    assert position.average_entry == D("110")


def test_position_close_resets_the_average() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("10"), D("100"))
    position.apply(D("-10"), D("110"))
    assert position.is_flat
    assert position.average_entry == D("0")


def test_short_position_average_is_correct() -> None:
    position = Position(venue="V", symbol="S")
    position.apply(D("-2"), D("100"))
    position.apply(D("-3"), D("110"))
    assert position.average_entry == D("106")


def test_inverse_unrealised_pnl_uses_the_reciprocal_formula() -> None:
    position = Position(venue="V", symbol="S", quantity=D("100000"),
                        average_entry=D("100000"))
    pnl = position.unrealized_pnl_inverse(D("125000"), D("1"))
    # PnL_base = 100000 * (1/100000 - 1/125000) = 0.2 BTC; x 125000 = 25000 USD
    assert pnl == D("25000")


# ======================================================================
# funding
# ======================================================================
async def test_perp_funding_charges_longs_on_a_positive_rate(venues, market) -> None:
    delta = venues[DELTA]
    await delta.place_order(order(DELTA, "BTCUSD", Side.BUY, "1000"))
    market.set_funding_rate("PAPER_DELTA:BTCUSD", D("0.0001"))
    accruals = delta.apply_funding()
    assert accruals and accruals[0].amount < 0
    assert accruals[0].kind == "FUNDING"


async def test_perp_funding_pays_shorts_on_a_positive_rate(venues, market) -> None:
    delta = venues[DELTA]
    await delta.place_order(order(DELTA, "BTCUSD", Side.SELL, "1000"))
    market.set_funding_rate("PAPER_DELTA:BTCUSD", D("0.0001"))
    assert delta.apply_funding()[0].amount > 0


async def test_funding_uses_the_real_recorded_rate(venues, market) -> None:
    hl = venues[HL]
    await hl.place_order(order(HL, "BTC", Side.SELL, "1"))
    rate = market.ticker("PAPER_HYPERLIQUID:BTC").funding_rate
    assert rate is not None and rate != 0
    accrual = hl.apply_funding()[0]
    assert accrual.rate == rate


async def test_eight_hours_on_hyperliquid_is_eight_hourly_settlements(venues, market) -> None:
    """Delta funds every 8h, Hyperliquid every hour: the same span of time
    must cost eight Hyperliquid intervals, not one."""
    hl = venues[HL]
    await hl.place_order(order(HL, "BTC", Side.SELL, "1"))
    market.set_funding_rate("PAPER_HYPERLIQUID:BTC", D("0.00001"))
    one_hour = hl.apply_funding()[0].amount
    eight_hours = hl.apply_funding(D(8))[0].amount
    assert abs(eight_hours - one_hour * 8) < D("0.0001")


# ======================================================================
# connectivity and faults
# ======================================================================
async def test_disconnect_blocks_every_operation(venues) -> None:
    hl = venues[HL]
    hl.engine.force_disconnect(True)
    with pytest.raises(VenueDisconnected):
        await hl.get_ticker("BTC")
    with pytest.raises(VenueDisconnected):
        await hl.get_positions()
    with pytest.raises(VenueDisconnected):
        await hl.place_order(order(HL, "BTC", Side.BUY, "1"))


async def test_reconnect_restores_service(venues) -> None:
    hl = venues[HL]
    hl.engine.force_disconnect(True)
    hl.engine.force_disconnect(False)
    assert (await hl.get_ticker("BTC")).mid > 0


async def test_injected_rejection_fires_once(venues, faults) -> None:
    faults.arm(FaultKind.ORDER_REJECTION, venue=HL, count=1)
    with pytest.raises(OrderRejected, match="injected rejection"):
        await venues[HL].place_order(order(HL, "BTC", Side.BUY, "1"))
    ok = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "1"))
    assert ok.status is OrderStatus.FILLED


async def test_injected_timeout_raises_venue_timeout(venues, faults) -> None:
    faults.arm(FaultKind.API_TIMEOUT, venue=HL)
    with pytest.raises(VenueTimeout):
        await venues[HL].place_order(order(HL, "BTC", Side.BUY, "1"))


async def test_injected_partial_fill_caps_the_quantity(venues, faults) -> None:
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "4", leg=Leg.HEDGE))
    assert result.filled_quantity == D("1")
    assert result.is_partial is True
    # The default time in force is IOC, so the unfilled remainder is cancelled
    # and the order is terminal -- it must not linger as a working order.
    assert result.status is OrderStatus.CANCELLED
    assert result.is_complete is True
    assert result not in await venues[HL].get_open_orders()


async def test_injected_high_slippage_worsens_the_fill(venues, faults) -> None:
    clean = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.1"))
    faults.arm(FaultKind.HIGH_SLIPPAGE, magnitude="0.01")
    slipped = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "0.1"))
    assert slipped.average_price > clean.average_price * D("1.005")


async def test_duplicate_execution_report_fault_does_not_move_the_position(
    venues, faults
) -> None:
    faults.arm(FaultKind.DUPLICATE_EXECUTION_REPORT, venue=HL)
    result = await venues[HL].place_order(order(HL, "BTC", Side.BUY, "1"))
    assert result.filled_quantity == D("1")
    positions = await venues[HL].get_positions()
    assert positions[0].quantity == D("1")


async def test_unexpected_position_appears_only_on_the_venue(venues) -> None:
    venues[HL].engine.inject_unexpected_position("PAXG", D("0.5"), D("4080"))
    positions = await venues[HL].get_positions()
    assert any(p.symbol == "PAXG" and p.quantity == D("0.5") for p in positions)


# ======================================================================
# stop-out
# ======================================================================
async def test_liquidation_force_closes_when_the_margin_level_collapses(
    registry, market, fx, faults, settings
) -> None:
    tight = settings.model_copy(update={"paper_starting_balance": D("2500")})
    hl = factory_with(registry, market, fx, faults, tight).create(HL)
    await hl.place_order(order(HL, "BTC", Side.BUY, "1"))
    market.apply_shock("BTC", D("-0.20"))
    assert hl.check_liquidations()
    assert await hl.get_positions() == []


async def test_fill_or_kill_rejects_a_partial(venues, faults) -> None:
    """FOK is all-or-nothing: a partial availability must reject, not fill."""
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    with pytest.raises(OrderRejected, match="fill-or-kill"):
        await venues[HL].place_order(OrderRequest(
            venue=HL, symbol="BTC", side=Side.BUY, quantity=D("4"),
            leg=Leg.HEDGE, time_in_force=TimeInForce.FOK,
        ))


async def test_gtc_partial_stays_working(venues, faults) -> None:
    """A good-till-cancelled order keeps its remainder on the book."""
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues[HL].place_order(OrderRequest(
        venue=HL, symbol="BTC", side=Side.BUY, quantity=D("4"),
        leg=Leg.HEDGE, time_in_force=TimeInForce.GTC,
    ))
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.is_complete is False
    assert result in await venues[HL].get_open_orders()


async def test_cancelling_a_partial_keeps_the_filled_quantity(venues, faults) -> None:
    faults.arm(FaultKind.LEG2_PARTIAL_FILL, magnitude="0.25", leg="HEDGE")
    result = await venues[HL].place_order(OrderRequest(
        venue=HL, symbol="BTC", side=Side.BUY, quantity=D("4"),
        leg=Leg.HEDGE, time_in_force=TimeInForce.GTC,
    ))
    cancelled = await venues[HL].cancel_order(result.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.filled_quantity == D("1")   # the fill is not forgotten


# ======================================================================
# Delta's size-based margin
# ======================================================================
def test_delta_margin_rate_rises_with_position_size(registry) -> None:
    """From Delta's published schedule for BTCUSD: 0.5% to $100k, then
    +0.0000025% per extra dollar."""
    spec = registry.get("PAPER_DELTA:BTCUSD")
    assert spec.initial_margin_rate_at(D(100_000)) == D("0.005")          # 200x
    assert spec.initial_margin_rate_at(D(500_000)) == D("0.015")          # ~67x
    assert spec.maintenance_margin_rate_at(D(500_000)) == D("0.0075")
    assert spec.initial_margin_rate_at(D(10**12)) == D(1)                 # capped at 100%


def test_hyperliquid_margin_is_flat(registry) -> None:
    spec = registry.get("PAPER_HYPERLIQUID:BTC")
    assert spec.initial_margin_rate_at(D(10_000_000)) == spec.effective_initial_margin_rate


async def test_a_position_too_large_for_scaled_margin_is_refused(venues) -> None:
    """~$8M of BTC at a flat 0.5% would need ~$41k; at Delta's scaled ~20.7%
    it needs ~$1.7M, which a $250k account does not have."""
    with pytest.raises(InsufficientMargin):
        await venues[DELTA].inner.place_order(order(DELTA, "BTCUSD", Side.BUY, "100000"))


async def test_growing_past_the_threshold_reprices_the_whole_position(venues) -> None:
    delta = venues[DELTA]
    await delta.place_order(order(DELTA, "BTCUSD", Side.BUY, "1000"))       # ~$82k, flat rate
    small = (await delta.get_balance()).used_margin
    await delta.place_order(order(DELTA, "BTCUSD", Side.BUY, "5000"))       # ~$490k total
    large = (await delta.get_balance()).used_margin
    # Six times the size, more than six times the margin: the rate rose on all of it.
    assert large > small * 6 * D("1.5")
