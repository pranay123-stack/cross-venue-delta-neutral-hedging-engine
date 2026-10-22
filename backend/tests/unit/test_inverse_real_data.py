"""Inverse-contract maths, checked on a real inverse perpetual.

Neither Delta Exchange nor Hyperliquid lists a live inverse perpetual, so the
inverse path is exercised on Bybit's BTCUSD (1 contract = 1 USD, settled in
BTC, funding every 8h): its spec comes from Bybit's instruments-info response
and its prices and settled funding from Bybit's public history
(``scripts/capture_bybit_inverse.py``), over the same 30 days as the committed
Delta/Hyperliquid history.  The hedge leg is Hyperliquid BTC.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from tests.conftest import DATA_DIR

from hedgelab.backtest.engine import BacktestConfig, Backtester
from hedgelab.domain.enums import HedgeObjective
from hedgelab.domain.instrument import InstrumentSpec
from hedgelab.domain.quantity import QuantityConverter
from hedgelab.hedge.objectives import RiskParameters, resolve_objective
from hedgelab.marketdata.history import HistoryBundle, align_closes

D = Decimal
INVERSE = "BYBIT:BTCUSD"
HL_BTC = "PAPER_HYPERLIQUID:BTC"
NOTIONAL = D(500_000)            # contracts of 1 USD


@pytest.fixture(scope="module")
def inverse_spec() -> InstrumentSpec:
    info = json.loads((DATA_DIR / "raw" / "bybit_instrument_BTCUSD.json").read_text())
    row = info["result"]["list"][0]
    assert row["contractType"] == "InversePerpetual" and row["settleCoin"] == "BTC"
    return InstrumentSpec.model_validate({
        "symbol": row["symbol"], "venue": "BYBIT", "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL", "base_asset": row["baseCoin"],
        "quote_asset": row["quoteCoin"], "settlement_asset": row["settleCoin"],
        "underlying_key": row["baseCoin"], "quantity_unit": "CONTRACT",
        "settlement_style": "INVERSE", "contract_size": "1",
        "tick_size": row["priceFilter"]["tickSize"],
        "min_quantity": row["lotSizeFilter"]["minOrderQty"],
        "quantity_step": row["lotSizeFilter"]["qtyStep"],
        "max_quantity": row["lotSizeFilter"]["maxOrderQty"],
        "price_precision": int(row["priceScale"]), "quantity_precision": 0,
        "max_leverage": row["leverageFilter"]["maxLeverage"],
        "funding_model": "PERPETUAL_FUNDING",
        "funding_interval_hours": str(int(row["fundingInterval"]) // 60),
    })


@pytest.fixture(scope="module")
def combined(history_module) -> HistoryBundle:
    bybit = HistoryBundle.load(DATA_DIR / "bybit_inverse_btcusd.json.gz")
    history_module.instruments[INVERSE] = bybit[INVERSE]
    return history_module


@pytest.fixture(scope="module")
def history_module() -> HistoryBundle:
    return HistoryBundle.load(DATA_DIR / "history.json.gz")


def test_the_captured_spec_is_inverse_and_funds_every_eight_hours(inverse_spec) -> None:
    assert inverse_spec.is_inverse
    assert inverse_spec.units_per_quantity == D(1)          # 1 USD per contract
    assert inverse_spec.funding_interval_hours == D(8)


def test_inverse_base_holding_moves_with_the_real_price(inverse_spec, combined) -> None:
    """The BTC an inverse position represents is N / S: it changes as the
    market moves, unlike a linear contract's."""
    path = align_closes(combined, INVERSE, HL_BTC)
    converter = QuantityConverter(inverse_spec)
    low = min(path, key=lambda p: p[1])[1]
    high = max(path, key=lambda p: p[1])[1]
    at_low = converter.base_units(NOTIONAL, low)
    at_high = converter.base_units(NOTIONAL, high)
    assert at_low > at_high
    assert at_low / at_high == high / low


def hedge_after_the_fact(inverse_spec, hl_spec, objective, entry, now, hedge_price):
    """Size a hedge for an inverse position opened at ``entry`` but hedged at ``now``."""
    source = QuantityConverter(inverse_spec).exposure(
        NOTIONAL, now, account_currency="USD", entry_price=entry)
    unit = QuantityConverter(hl_spec).exposure(D(1), hedge_price, account_currency="USD")
    result = resolve_objective(objective, source_exposure=source, hedge_unit_exposure=unit,
                               target_ratio=D(1), params=RiskParameters())
    return result.quantity


def residual_drawdown(path, entry, hedge_coins, start_index) -> Decimal:
    """Worst USD drawdown of (inverse long + Hyperliquid hedge) from ``start_index``."""
    _, s0, h0 = path[start_index]

    def value(s, h):
        inverse_pnl = NOTIONAL * (s / entry - 1)            # PnL_quote = N(S/E - 1)
        hedge_pnl = hedge_coins * (h - h0)
        return inverse_pnl + hedge_pnl

    start_value = value(s0, h0)
    peak, worst = D(0), D(0)
    for _, s, h in path[start_index:]:
        pnl = value(s, h) - start_value
        peak = max(peak, pnl)
        worst = min(worst, pnl - peak)
    return abs(worst)


def test_hedging_an_aged_inverse_position_by_its_entry_price(inverse_spec, combined, registry) -> None:
    """The trap the README describes, on real prices.

    Open $500k of inverse BTCUSD at the first real close; hedge it later, at
    the hour BTC had moved furthest from that entry (leaving at least three
    days of real path to measure).  QUOTE_PNL_NEUTRAL sizes the hedge by entry
    (N/E) and holds; BASE_ASSET_NEUTRAL sizes it by the current holding (N/S)
    and is wrong by exactly the price move.
    """
    hl_spec = registry.get(HL_BTC)
    path = align_closes(combined, INVERSE, HL_BTC)
    entry = path[0][1]
    later = max(range(24, len(path) - 72), key=lambda i: abs(path[i][1] / entry - 1))
    _, now, hedge_price = path[later]
    assert abs(now / entry - 1) > D("0.02")             # BTC really did move

    quote = hedge_after_the_fact(inverse_spec, hl_spec, HedgeObjective.QUOTE_PNL_NEUTRAL,
                                 entry, now, hedge_price)
    base = hedge_after_the_fact(inverse_spec, hl_spec, HedgeObjective.BASE_ASSET_NEUTRAL,
                                entry, now, hedge_price)
    assert abs(quote) == NOTIONAL / entry
    assert abs(base) == NOTIONAL / now
    assert abs(abs(quote) / abs(base) - now / entry) < D("1e-20")

    unhedged = residual_drawdown(path, entry, D(0), later)
    by_entry = residual_drawdown(path, entry, quote, later)
    by_holding = residual_drawdown(path, entry, base, later)
    assert by_entry < unhedged * D("0.05")              # >95% of the drawdown removed
    assert by_entry < by_holding                        # and better than the naive hedge


def test_backtest_hedges_an_inverse_leg_on_real_history(inverse_spec, combined, registry, fx) -> None:
    config = BacktestConfig(source_key=INVERSE, hedge_key=HL_BTC, source_quantity=NOTIONAL,
                            objective=HedgeObjective.QUOTE_PNL_NEUTRAL)
    report = Backtester([*registry.all(), inverse_spec], combined, fx).run(config)
    assert report.risk_reduction_pct > D(95)
    # Bybit funds every 8h, Hyperliquid every hour.
    hours = len(report.points)
    assert abs(report.funding_settlements - (hours // 8 + hours)) <= 2


def test_rebalancing_an_inverse_leg_keeps_sizing_by_entry(inverse_spec, combined, registry, fx) -> None:
    """Regression: rebalances used to size the target from the *current*
    price as if it were the entry, which for an inverse contract is the
    N/S-versus-N/E trap.  With a zero tolerance every hour is checked; the
    correct target never changes, so the hedge must not drift with BTC."""
    config = BacktestConfig(source_key=INVERSE, hedge_key=HL_BTC, source_quantity=NOTIONAL,
                            objective=HedgeObjective.QUOTE_PNL_NEUTRAL, tolerance_bps=D(0))
    report = Backtester([*registry.all(), inverse_spec], combined, fx).run(config)
    quantities = {p.hedge_quantity for p in report.points}
    assert len(quantities) == 1, f"hedge drifted across {len(quantities)} sizes"
    assert report.risk_reduction_pct > D(95)
