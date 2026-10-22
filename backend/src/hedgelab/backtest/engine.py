"""Hedge-performance backtest on real venue history.

Replays the *actual* hourly closes of both legs (Delta Exchange and
Hyperliquid candles) and each venue's *settled* funding through the real
hedge objectives, rounding and cost model, and reports what the hedge did:
residual exposure over time, funding carry, fees, spread cost, drawdown and
margin usage.

What is real and what is not, stated once:
  * prices -- real hourly candle closes, aligned on timestamps both venues
    printed;
  * funding -- real settled rates, charged on each venue's own clock (Delta
    every 8h, 4h for PAXG; Hyperliquid hourly);
  * execution -- taker fee from the spec, plus the cost of sweeping the
    book.  Candles carry no depth, so that cost comes from an impact curve
    measured on real recorded books (``marketdata.impact``) when one is
    supplied, and from half the spec's measured spread otherwise.  Orders
    larger than any recorded book are counted as unmeasured.

No claim of profitability is made anywhere. The report states what the run
produced; interpreting it is the reader's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..domain.enums import HedgeObjective
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, normalize, quantize, safe_div
from ..domain.quantity import QuantityConverter
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from ..marketdata.history import HistoryBundle, align_closes
from ..marketdata.impact import ImpactCurve

log = get_logger(__name__)

D = Decimal


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    source_key: str
    hedge_key: str
    #: Signed source position held throughout the run, in source units.
    source_quantity: Decimal
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = D(1)
    #: Use at most this many of the most recent aligned hours (0 = all).
    steps: int = 0
    #: Rebalance when residual exceeds this, in bps of source notional.
    tolerance_bps: Decimal = D(25)
    #: How often to *check* for a rebalance, in hourly steps.
    rebalance_every: int = 1
    account_currency: str = "USD"

    #: One step is one real hourly candle.
    step_seconds: Decimal = D(3600)


@dataclass
class BacktestPoint:
    step: int
    source_price: Decimal
    hedge_price: Decimal
    hedge_quantity: Decimal
    residual_bps: Decimal
    hedge_ratio: Decimal
    net_pnl: Decimal
    cumulative_funding: Decimal
    cumulative_fees: Decimal
    cumulative_slippage: Decimal
    margin_used: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "source_price": str(quantize(self.source_price, 6)),
            "hedge_price": str(quantize(self.hedge_price, 6)),
            "hedge_quantity": str(normalize(self.hedge_quantity)),
            "residual_bps": str(quantize(self.residual_bps, 4)),
            "hedge_ratio": str(quantize(self.hedge_ratio, 6)),
            "net_pnl": str(quantize(self.net_pnl, 4)),
            "cumulative_funding": str(quantize(self.cumulative_funding, 4)),
            "cumulative_fees": str(quantize(self.cumulative_fees, 4)),
            "cumulative_slippage": str(quantize(self.cumulative_slippage, 4)),
            "margin_used": str(quantize(self.margin_used, 2)),
        }


@dataclass
class BacktestReport:
    config: BacktestConfig
    points: list[BacktestPoint] = field(default_factory=list)
    rebalances: int = 0
    funding_settlements: int = 0
    start: datetime | None = None
    end: datetime | None = None
    #: "impact curve from N recorded books" or "half the measured spread".
    execution_model: str = ""
    #: Trades larger than any recorded book, charged at the deepest measured cost.
    unmeasured_trades: int = 0

    # --- outcome ------------------------------------------------------
    final_net_pnl: Decimal = ZERO
    total_funding: Decimal = ZERO
    total_fees: Decimal = ZERO
    total_slippage: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    #: Drawdown of the *price* P&L only, with carry and costs excluded.
    #: This is the apples-to-apples comparison against the unhedged book:
    #: net P&L declines monotonically under negative carry, so its drawdown
    #: measures the cost of hedging, not the risk that was removed.
    gross_drawdown: Decimal = ZERO
    max_residual_bps: Decimal = ZERO
    mean_residual_bps: Decimal = ZERO
    peak_margin: Decimal = ZERO
    unhedged_pnl: Decimal = ZERO
    unhedged_max_drawdown: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                "source": self.config.source_key, "hedge": self.config.hedge_key,
                "source_quantity": str(self.config.source_quantity),
                "objective": self.config.objective.value,
                "steps": len(self.points),
                "step_seconds": str(self.config.step_seconds),
                "days": str(quantize(
                    self.config.step_seconds * D(len(self.points)) / D(86400), 3)),
                "tolerance_bps": str(self.config.tolerance_bps),
                "rebalance_every": self.config.rebalance_every,
            },
            "data": {
                "execution": self.execution_model,
                "unmeasured_trades": self.unmeasured_trades,
                "prices": "real hourly candle closes, both venues",
                "funding": "real settled funding, each venue on its own clock",
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None,
            },
            "rebalances": self.rebalances,
            "funding_settlements": self.funding_settlements,
            "final_net_pnl": str(quantize(self.final_net_pnl, 4)),
            "total_funding": str(quantize(self.total_funding, 4)),
            "total_fees": str(quantize(self.total_fees, 4)),
            "total_slippage": str(quantize(self.total_slippage, 4)),
            "max_drawdown": str(quantize(self.max_drawdown, 4)),
            "gross_drawdown": str(quantize(self.gross_drawdown, 4)),
            "max_residual_bps": str(quantize(self.max_residual_bps, 4)),
            "mean_residual_bps": str(quantize(self.mean_residual_bps, 4)),
            "peak_margin": str(quantize(self.peak_margin, 2)),
            "unhedged_pnl": str(quantize(self.unhedged_pnl, 4)),
            "unhedged_max_drawdown": str(quantize(self.unhedged_max_drawdown, 4)),
            "risk_reduction_pct": str(quantize(self.risk_reduction_pct, 2)),
            "points": [p.to_dict() for p in self.points],
        }

    @property
    def risk_reduction_pct(self) -> Decimal:
        """Share of the unhedged price drawdown that the hedge removed.

        Deliberately **not** a profitability claim. It compares price P&L
        against price P&L; carry and execution cost are reported separately,
        and in these runs they are usually negative. A hedge that removes 99%
        of the drawdown while costing money is doing exactly its job.
        """
        if self.unhedged_max_drawdown <= ZERO:
            return ZERO
        removed = self.unhedged_max_drawdown - abs(self.gross_drawdown)
        return safe_div(removed, self.unhedged_max_drawdown) * D(100)

    def summary(self) -> str:
        days = quantize(self.config.step_seconds * D(len(self.points)) / D(86400), 1)
        return (
            f"{len(self.points)} real hours (~{days}d), {self.rebalances} rebalance(s): "
            f"net {quantize(self.final_net_pnl, 2)} "
            f"(funding {quantize(self.total_funding, 2)}, fees "
            f"{quantize(self.total_fees, 2)}, slippage "
            f"{quantize(self.total_slippage, 2)}); residual mean "
            f"{quantize(self.mean_residual_bps, 2)} bps, max "
            f"{quantize(self.max_residual_bps, 2)} bps; price drawdown "
            f"{quantize(abs(self.gross_drawdown), 2)} versus "
            f"{quantize(self.unhedged_max_drawdown, 2)} unhedged "
            f"({quantize(self.risk_reduction_pct, 1)}% removed)."
        )


class Backtester:
    """Replays real venue history through the real hedge maths."""

    def __init__(
        self,
        instruments: Sequence[InstrumentSpec],
        history: HistoryBundle,
        fx: FxService | None = None,
        impact: dict[str, ImpactCurve] | None = None,
    ) -> None:
        self.instruments = {spec.key: spec for spec in instruments}
        self.history = history
        self.fx = fx or FxService()
        self.impact = impact or {}

    def run(self, config: BacktestConfig) -> BacktestReport:
        source = self.instruments[config.source_key]
        hedge = self.instruments[config.hedge_key]
        path = align_closes(self.history, source.key, hedge.key, "1h")
        if config.steps:
            path = path[-(config.steps + 1):]
        if len(path) < 2:
            raise ValueError(
                f"not enough overlapping hourly history for {source.key} and {hedge.key}"
            )
        source_history = self.history[source.key]
        hedge_history = self.history[hedge.key]
        source_conv, hedge_conv = QuantityConverter(source), QuantityConverter(hedge)

        account = config.account_currency
        source_fx = self.fx.try_rate(source.quote_asset, account)
        hedge_fx = self.fx.try_rate(hedge.quote_asset, account)

        report = BacktestReport(config=config, start=path[0][0], end=path[-1][0])
        curves = [self.impact.get(k) for k in (source.key, hedge.key)]
        report.execution_model = (
            f"impact curves from {min(c.frames for c in curves if c)} recorded books"
            if all(curves) else "half the spread measured at sync time"
        )

        # Open both legs at the first real close, crossing half the spread.
        _, first_source, first_hedge = path[0]
        source_entry = self._cross(source, first_source, config.source_quantity, report)
        hedge_target = self._target_hedge(
            source_conv, hedge_conv, source, hedge, config,
            config.source_quantity, first_source, first_hedge, source_fx, hedge_fx,
        )
        hedge_entry = self._cross(hedge, first_hedge, hedge_target, report)
        hedge_quantity = hedge_target

        cumulative_fees = self._entry_cost(source, source_conv, config.source_quantity,
                                           source_entry, source_fx)
        cumulative_fees += self._entry_cost(hedge, hedge_conv, hedge_quantity,
                                            hedge_entry, hedge_fx)
        cumulative_slippage = (
            abs(source_conv.quote_delta(config.source_quantity, first_source))
            * abs(source_entry - first_source) * source_fx
            + abs(hedge_conv.quote_delta(hedge_quantity, first_hedge))
            * abs(hedge_entry - first_hedge) * hedge_fx
        )
        cumulative_funding = ZERO
        peak_pnl = ZERO
        peak_gross = ZERO
        peak_unhedged = ZERO
        max_drawdown = ZERO
        gross_drawdown = ZERO
        unhedged_drawdown = ZERO
        residuals: list[Decimal] = []

        for step, (opened_at, source_mid, hedge_mid) in enumerate(path[1:], start=1):
            # A candle stamped ``opened_at`` closes an hour later.
            closed_at = opened_at + timedelta(hours=1)
            source_pnl = self._leg_pnl(source, config.source_quantity,
                                       source_entry, source_mid) * source_fx
            hedge_pnl = self._leg_pnl(hedge, hedge_quantity,
                                      hedge_entry, hedge_mid) * hedge_fx

            for spec, history, quantity, mid, fx_rate in (
                (source, source_history, config.source_quantity, source_mid, source_fx),
                (hedge, hedge_history, hedge_quantity, hedge_mid, hedge_fx),
            ):
                if not self._settles(spec, closed_at):
                    continue
                rate = history.funding_at(closed_at)
                if rate is None:
                    continue
                notional = QuantityConverter(spec).notional_quote(quantity, mid)
                # Positive funding: longs pay shorts.
                cumulative_funding += -notional * rate * fx_rate
                report.funding_settlements += 1

            net_pnl = source_pnl + hedge_pnl + cumulative_funding - cumulative_fees - cumulative_slippage
            unhedged_pnl = source_pnl

            gross_pnl = source_pnl + hedge_pnl
            peak_pnl = max(peak_pnl, net_pnl)
            peak_gross = max(peak_gross, gross_pnl)
            peak_unhedged = max(peak_unhedged, unhedged_pnl)
            max_drawdown = min(max_drawdown, net_pnl - peak_pnl)
            gross_drawdown = min(gross_drawdown, gross_pnl - peak_gross)
            unhedged_drawdown = min(unhedged_drawdown, unhedged_pnl - peak_unhedged)

            residual_bps = self._residual_bps(
                source_conv, hedge_conv, config.source_quantity, hedge_quantity,
                source_mid, hedge_mid, source_fx, hedge_fx, source_entry, hedge_entry,
            )
            residuals.append(abs(residual_bps))

            # Rebalance on schedule, when out of tolerance.
            if step % config.rebalance_every == 0 and abs(residual_bps) > config.tolerance_bps:
                new_target = self._target_hedge(
                    source_conv, hedge_conv, source, hedge, config,
                    config.source_quantity, source_mid, hedge_mid, source_fx, hedge_fx,
                    source_entry,
                )
                delta = new_target - hedge_quantity
                rounded = hedge_conv.round_quantity(delta).rounded
                if rounded != ZERO:
                    trade_price = self._cross(hedge, hedge_mid, rounded, report)
                    cumulative_fees += self._entry_cost(
                        hedge, hedge_conv, rounded, trade_price, hedge_fx
                    )
                    cumulative_slippage += abs(
                        hedge_conv.quote_delta(rounded, hedge_mid)
                    ) * abs(trade_price - hedge_mid) * hedge_fx
                    # Blend the new trade into the weighted entry price.
                    total = hedge_quantity + rounded
                    if total != ZERO:
                        hedge_entry = (
                            hedge_entry * hedge_quantity + trade_price * rounded
                        ) / total
                    hedge_quantity = total
                    report.rebalances += 1

            source_notional = abs(source_conv.notional_quote(config.source_quantity, source_mid))
            hedge_notional = abs(hedge_conv.notional_quote(hedge_quantity, hedge_mid))
            margin = (
                source_notional * source_fx * source.initial_margin_rate_at(source_notional)
                + hedge_notional * hedge_fx * hedge.initial_margin_rate_at(hedge_notional)
            )
            report.peak_margin = max(report.peak_margin, margin)

            report.points.append(BacktestPoint(
                step=step, source_price=source_mid, hedge_price=hedge_mid,
                hedge_quantity=hedge_quantity, residual_bps=residual_bps,
                hedge_ratio=self._ratio(source_conv, hedge_conv, config.source_quantity,
                                        hedge_quantity, source_mid, hedge_mid,
                                        source_fx, hedge_fx, source_entry, hedge_entry),
                net_pnl=net_pnl, cumulative_funding=cumulative_funding,
                cumulative_fees=cumulative_fees, cumulative_slippage=cumulative_slippage,
                margin_used=margin,
            ))

        last = report.points[-1] if report.points else None
        report.final_net_pnl = last.net_pnl if last else ZERO
        report.total_funding = cumulative_funding
        report.total_fees = cumulative_fees
        report.total_slippage = cumulative_slippage
        report.max_drawdown = max_drawdown
        report.gross_drawdown = gross_drawdown
        report.unhedged_max_drawdown = abs(unhedged_drawdown)
        report.unhedged_pnl = (
            self._leg_pnl(source, config.source_quantity, source_entry, path[-1][1]) * source_fx
        )
        report.max_residual_bps = max(residuals) if residuals else ZERO
        report.mean_residual_bps = (
            sum(residuals, ZERO) / D(len(residuals)) if residuals else ZERO
        )
        log.info("backtest complete", extra={"summary": report.summary()})
        return report

    @staticmethod
    def _settles(spec: InstrumentSpec, at: datetime) -> bool:
        """Funding settles at UTC multiples of the venue's interval."""
        hours = int(spec.funding_interval_hours)
        return hours > 0 and at.minute == 0 and at.hour % hours == 0

    def _cross(
        self, spec: InstrumentSpec, mid: Decimal, quantity: Decimal, report: BacktestReport
    ) -> Decimal:
        """Average taker price for ``quantity`` (signed) at ``mid``.

        With a measured impact curve the cost grows with size, as it does on
        the real book; without one it is half the spec's measured spread.
        """
        is_buy = quantity > ZERO
        curve = self.impact.get(spec.key)
        if curve is not None:
            notional = abs(QuantityConverter(spec).notional_quote(quantity, mid))
            cost_bps, measured = curve.cost_bps(notional, is_buy)
            if not measured:
                report.unmeasured_trades += 1
        else:
            cost_bps = spec.typical_spread_bps / D(2)
        offset = mid * cost_bps / D(10000)
        return mid + offset if is_buy else mid - offset

    # ------------------------------------------------------------------
    def _target_hedge(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source: InstrumentSpec,
        hedge: InstrumentSpec,
        config: BacktestConfig,
        source_quantity: Decimal,
        source_price: Decimal,
        hedge_price: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
        source_entry: Decimal | None = None,
    ) -> Decimal:
        # The held source position's sensitivity depends on where it was
        # entered: for an inverse contract dPnL/dS = N / entry, not N / mark.
        source_exposure = source_conv.exposure(
            source_quantity, source_price, account_currency=config.account_currency,
            fx_rate=source_fx, entry_price=source_entry,
        )
        unit = hedge_conv.exposure(
            D(1), hedge_price, account_currency=config.account_currency, fx_rate=hedge_fx
        )
        from ..hedge.objectives import RiskParameters, resolve_objective

        result = resolve_objective(
            config.objective, source_exposure=source_exposure,
            hedge_unit_exposure=unit, target_ratio=config.target_ratio,
            params=RiskParameters(),
        )
        return hedge_conv.round_quantity(result.quantity).rounded

    @staticmethod
    def _leg_pnl(
        spec: InstrumentSpec, quantity: Decimal, entry: Decimal, mark: Decimal
    ) -> Decimal:
        if quantity == ZERO or entry <= ZERO:
            return ZERO
        if spec.is_inverse:
            notional = quantity * spec.units_per_quantity
            return notional * (D(1) / entry - D(1) / mark) * mark
        return (mark - entry) * quantity * spec.units_per_quantity

    @staticmethod
    def _entry_cost(
        spec: InstrumentSpec, converter: QuantityConverter, quantity: Decimal,
        price: Decimal, fx_rate: Decimal,
    ) -> Decimal:
        from ..domain.numeric import from_bps

        if quantity == ZERO:
            return ZERO
        notional = abs(converter.notional_quote(quantity, price))
        return notional * from_bps(spec.taker_fee_bps) * fx_rate

    def _residual_bps(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source_quantity: Decimal,
        hedge_quantity: Decimal,
        source_mid: Decimal,
        hedge_mid: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
        source_entry: Decimal,
        hedge_entry: Decimal,
    ) -> Decimal:
        source_exposure = source_conv.exposure(
            source_quantity, source_mid, account_currency="", fx_rate=source_fx,
            entry_price=source_entry,
        )
        hedge_exposure = hedge_conv.exposure(
            hedge_quantity, hedge_mid, account_currency="", fx_rate=hedge_fx,
            entry_price=hedge_entry,
        )
        residual = abs(
            source_exposure.account_delta + hedge_exposure.account_delta
        ) * hedge_mid
        return safe_div(residual, abs(source_exposure.notional_account)) * D(10000)

    def _ratio(
        self,
        source_conv: QuantityConverter,
        hedge_conv: QuantityConverter,
        source_quantity: Decimal,
        hedge_quantity: Decimal,
        source_mid: Decimal,
        hedge_mid: Decimal,
        source_fx: Decimal,
        hedge_fx: Decimal,
        source_entry: Decimal,
        hedge_entry: Decimal,
    ) -> Decimal:
        source_exposure = source_conv.exposure(
            source_quantity, source_mid, account_currency="", fx_rate=source_fx,
            entry_price=source_entry,
        )
        hedge_exposure = hedge_conv.exposure(
            hedge_quantity, hedge_mid, account_currency="", fx_rate=hedge_fx,
            entry_price=hedge_entry,
        )
        return safe_div(-hedge_exposure.account_delta, source_exposure.account_delta)
