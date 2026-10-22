"""Application service -- the composition root.

Everything the platform can do is a method here.  The FastAPI routers, the CLI
and the scenario runner are all thin shells over this class, so behaviour
cannot drift between "what the API does" and "what the CLI does".

Construction is explicit rather than magical: the service builds the registry,
market feed, FX service, fault injector, venues, calculators and engines in
dependency order, and every one of them is an attribute you can reach in a
test.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .backtest.engine import BacktestConfig, Backtester, BacktestReport
from .config import Settings, get_settings
from .costs.funding import FundingProjection, project_funding
from .costs.pnl import LegAccounting, PnLBreakdown, PnLCalculator
from .db.recorder import DatabaseRecorder
from .db.repositories import (
    ControlStateRepository,
    CycleRepository,
    InstrumentRepository,
    MappingRepository,
    ObservabilityRepository,
    OrderRepository,
    PositionRepository,
)
from .db.session import Database
from .domain.account import AccountSnapshot
from .domain.enums import (
    EmergencyAction,
    FaultKind,
    HedgeObjective,
    MarketScenario,
    RiskLevel,
    Side,
)
from .domain.instrument import InstrumentSpec
from .domain.market import Ticker, utcnow
from .domain.numeric import ZERO, dec, quantize
from .domain.orders import Order, OrderRequest, Position
from .execution.coordinator import ExecutionResult, HedgeCoordinator, HedgeRequest
from .execution.ports import EventPublisher, NullPublisher
from .execution.rebalancer import RebalanceAssessment, Rebalancer
from .faults.injector import ArmedFault, FaultInjector
from .hedge.calculator import HedgeCalculation, HedgeCalculator, HedgeInputs
from .hedge.objectives import RiskParameters
from .hedge.optimizer import HedgeOptimizer, OptimizationResult, OptimizerConfig
from .instruments.registry import HedgeMapping, InstrumentRegistry
from .logging_setup import correlation_scope, get_logger, new_correlation_id
from .marketdata.collector import LiveCollector
from .marketdata.feed import MarketFeed
from .marketdata.frames import load_frames
from .marketdata.fx import FxService
from .marketdata.history import HistoryBundle, fetch_history
from .marketdata.impact import measure_impact
from .marketdata.stats import PairEstimate, RollingStats
from .reconciliation.engine import ReconciliationEngine, ReconciliationReport, RecoveryPlan
from .reconciliation.venue_records import (
    FillRecord,
    FundingRecord,
    RecordReport,
    reconcile_records,
    records_from_paper_engine,
)
from .risk.engine import PairRisk, PairRiskInputs, RiskEngine, RiskThresholds
from .risk.portfolio import PortfolioLimits, PortfolioRisk, PortfolioRiskEngine
from .venues.base import TradingVenue, VenueError
from .venues.factory import VenueFactory

log = get_logger(__name__)

KILL_SWITCH_KEY = "kill_switch"


@dataclass
class ServiceState:
    """Mutable operational state, separate from the wiring."""

    emergency_mode: bool = False
    trading_paused: bool = False
    paused_pairs: set[str] = field(default_factory=set)
    kill_switch_engaged: bool = False
    last_reconciliation: ReconciliationReport | None = None
    started_at_step: int = 0


class HedgeLabService:
    """The whole platform, assembled."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        database: Database | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.state = ServiceState()

        # --- reference data ---------------------------------------------
        self.registry = InstrumentRegistry.from_directory(self.settings.instrument_spec_dir)
        self.specs: dict[str, InstrumentSpec] = {s.key: s for s in self.registry.all()}

        # --- market data ------------------------------------------------
        self.fx = FxService()
        frames = []
        if self.settings.market_data_source == "replay":
            if self.settings.market_replay_path is None:
                raise ValueError("market_data_source=replay needs market_replay_path")
            frames = load_frames(self.settings.market_replay_path)
        self.market = MarketFeed(
            self.registry.all(),
            frames=frames,
            source=self.settings.market_data_source,
            stale_after_seconds=self.settings.stale_data_threshold_seconds,
            seed=self.settings.random_seed,
        )
        self._collector: LiveCollector | None = None
        self._history_cache: dict[tuple[str, ...], HistoryBundle] = {}
        self.faults = FaultInjector(seed=self.settings.random_seed)
        #: Realised volatility and correlation, measured from the prices this
        #: process has actually seen.  Fed by ``advance_market``.
        self.stats = RollingStats(
            window=self.settings.stats_window,
            sample_seconds=self.settings.stats_sample_seconds,
            min_samples=self.settings.stats_min_samples,
        )

        # --- venues -----------------------------------------------------
        self.venue_factory = VenueFactory(
            settings=self.settings, instruments=self.specs,
            market=self.market, fx=self.fx, faults=self.faults,
        )
        self.venues: dict[str, TradingVenue] = self.venue_factory.create_all()

        # --- engines ----------------------------------------------------
        self.calculator = HedgeCalculator(self.fx)
        self.optimizer = HedgeOptimizer(self.calculator)
        self.risk_engine = RiskEngine(self.fx, self._risk_thresholds())
        self.portfolio_engine = PortfolioRiskEngine(self._portfolio_limits())
        self.pnl_calculator = PnLCalculator(self.fx, self.settings.account_currency)
        self.reconciler = ReconciliationEngine(registry=self.registry, venues=self.venues)

        # --- persistence ------------------------------------------------
        self.database = database if database is not None else Database.from_settings(self.settings)
        self.recorder = DatabaseRecorder(self.database)
        self.publisher: EventPublisher = publisher or NullPublisher()

        self.coordinator = HedgeCoordinator(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, optimizer=self.optimizer,
            risk_engine=self.risk_engine, fx=self.fx,
            recorder=self.recorder, publisher=self.publisher,
        )
        self.rebalancer = Rebalancer(
            settings=self.settings, registry=self.registry, venues=self.venues,
            calculator=self.calculator, fx=self.fx, recorder=self.recorder,
        )
        self._ticker_task: asyncio.Task[None] | None = None

    # ==================================================================
    # lifecycle
    # ==================================================================
    async def startup(
        self,
        *,
        create_schema: bool = False,
        seed_reference_data: bool = True,
        rehydrate: bool = True,
        seed_stats: bool = True,
    ) -> None:
        if create_schema:
            await self.database.create_all()
        if seed_reference_data:
            await self.sync_reference_data()
        if self.settings.market_data_source == "live":
            try:
                await self.refresh_market()
            except Exception as exc:  # the API should still come up and say why
                log.error("initial market poll failed", extra={"error": str(exc)})
        else:
            self._sync_fx()
        if seed_stats:
            await self.seed_statistics()
        await self._restore_kill_switch()
        if rehydrate:
            restored = await self.rehydrate_paper_venues()
            if restored:
                log.info("paper venue state restored after restart", extra=restored)
        log.info(
            "service started",
            extra={
                "mode": self.settings.trading_mode.value,
                "venues": sorted(self.venues),
                "instruments": len(self.specs),
                "mappings": len(self.registry.mappings()),
            },
        )

    async def shutdown(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            self._ticker_task = None
        if self._collector is not None:
            await self._collector.close()
            self._collector = None
        await self.database.dispose()

    async def rehydrate_paper_venues(self) -> dict[str, Any]:
        """Reload venue state from the database after a process restart.

        The paper venues live in memory, so without this every restart would
        report the venue as flat and reconciliation would (correctly, but
        uselessly) flag every position as MISSING.  A real venue remembers; this
        makes the paper one behave the same way, which is what lets restart
        recovery test something meaningful.
        """
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            obs = ObservabilityRepository(session)
            latest_balance: dict[str, Decimal] = {}
            for venue_name in self.venues:
                snapshots = await obs.margin_snapshots(venue=venue_name, limit=1)
                if snapshots:
                    latest_balance[venue_name] = snapshots[0].balance

        restored = 0
        for position in positions:
            engine = getattr(self.venues.get(position.venue), "engine", None)
            if engine is None or position.is_flat:
                continue
            engine.restore_position(position)
            restored += 1
        for venue_name, balance in latest_balance.items():
            engine = getattr(self.venues[venue_name], "engine", None)
            if engine is not None:
                engine.restore_balance(balance)
        if restored or latest_balance:
            async with self.database.session() as session:
                await ObservabilityRepository(session).record_system_event(
                    "VENUE_REHYDRATED", "INFO", "service",
                    f"restored {restored} position(s) and {len(latest_balance)} balance(s) "
                    f"into the paper venues after restart",
                    {"positions": restored, "balances": sorted(latest_balance)},
                )
        return {"positions_restored": restored, "balances_restored": len(latest_balance)}

    async def sync_reference_data(self) -> None:
        """Write the YAML catalogue into the database."""
        async with self.database.session() as session:
            instruments = InstrumentRepository(session)
            mappings = MappingRepository(session)
            rows: dict[str, Any] = {}
            for spec in self.registry.all():
                rows[spec.key] = await instruments.upsert(spec)
            for mapping in self.registry.mappings():
                await mappings.upsert(
                    mapping.name, rows[mapping.source_key], rows[mapping.hedge_key],
                    enabled=mapping.enabled,
                )
            obs = ObservabilityRepository(session)
            for quote in self.fx.all_quotes():
                await obs.record_fx(quote.base, quote.quote, quote.rate, quote.source)
            await obs.record_system_event(
                "REFERENCE_DATA_SYNC", "INFO", "service",
                f"synchronised {len(rows)} instruments and {len(self.registry.mappings())} mappings",
                {"instruments": sorted(rows)},
            )

    # ==================================================================
    # market data
    # ==================================================================
    async def refresh_market(self) -> int:
        """Poll both venues once and apply the frame (live mode only)."""
        if self.settings.market_data_source != "live":
            return self.advance_market(1)
        if self._collector is None:
            self._collector = LiveCollector(depth=self.settings.market_depth)
        frame = await self._collector.collect(self.market.instruments)
        self.market.push(frame)
        return self.advance_market(1)

    def advance_market(self, steps: int = 1) -> int:
        """Apply the next real frame(s) and sample them for statistics.

        Live: applies the newest polled frame, if one arrived.  Replay: steps
        through the recording and stops at its end.  Returns frames applied.
        Sampling happens here so the estimator sees exactly the prices the
        rest of the platform sees.
        """
        applied = 0
        for _ in range(max(0, steps)):
            if not self.market.advance(1):
                break
            applied += 1
            self._sync_fx()
            self.stats.observe_many(
                {key: ticker.mid for key, ticker in self.market.tickers().items()},
                self.market.clock,
            )
        return applied

    def _sync_fx(self) -> None:
        for (base, quote), rate in self.market.fx_rates.items():
            self.fx.set_rate(base, quote, rate, source="kraken")

    async def history_bundle(self, keys: list[str]) -> HistoryBundle:
        """Real candle and funding history for ``keys``: the configured file,
        the history shipped beside a replay recording, or -- live -- fetched
        from the venues (30 days of hourly closes and settled funding)."""
        path = self.settings.market_history_path
        replay = self.settings.market_replay_path
        if path is None and replay is not None and (replay.parent / "history.json.gz").exists():
            path = replay.parent / "history.json.gz"
        if path is not None:
            bundle = HistoryBundle.load(path)
            if all(k in bundle.instruments for k in keys):
                return bundle
        cached = self._history_cache.get(tuple(sorted(keys)))
        if cached is not None:
            return cached
        bundle = await fetch_history([self.registry.get(k) for k in keys],
                                     hourly_days=30, five_minute_days=0)
        self._history_cache[tuple(sorted(keys))] = bundle
        return bundle

    async def run_backtest(
        self,
        mapping_name: str,
        *,
        source_quantity: Decimal,
        target_ratio: Decimal = Decimal(1),
        objective: HedgeObjective | None = None,
        steps: int = 0,
        tolerance_bps: Decimal = Decimal(25),
        book_impact: bool = True,
    ) -> BacktestReport:
        """Replay real hourly history of a pair through the hedge maths."""
        mapping = self.registry.mapping_by_name(mapping_name)
        bundle = await self.history_bundle([mapping.source_key, mapping.hedge_key])
        impact = None
        if book_impact and self.market.is_replay:
            recorded = self.market.recorded_frames
            impact = {key: measure_impact(self.registry.get(key), recorded)
                      for key in (mapping.source_key, mapping.hedge_key)}
        return Backtester(self.registry.all(), bundle, self.fx, impact).run(BacktestConfig(
            source_key=mapping.source_key, hedge_key=mapping.hedge_key,
            source_quantity=source_quantity, objective=objective or mapping.objective,
            target_ratio=target_ratio, steps=steps, tolerance_bps=tolerance_bps,
            account_currency=self.settings.account_currency,
        ))

    async def seed_statistics(self, bundle: HistoryBundle | None = None) -> int:
        """Prime the estimator with real 5-minute candles, so volatility and
        correlation are measured from the market from the first request.

        Returns the number of aligned samples observed.  A failure to fetch
        is logged, not raised: the objectives then fall back to configured
        assumptions and say so, exactly as before enough data exists.
        """
        if bundle is None:
            path = self.settings.market_history_path
            replay = self.settings.market_replay_path
            if path is None and self.settings.market_data_source == "replay" and replay:
                # Recordings ship with their history alongside (tests, image).
                sibling = replay.parent / "history.json.gz"
                path = sibling if sibling.exists() else None
            try:
                if path is not None:
                    bundle = HistoryBundle.load(path)
                elif self.settings.market_data_source == "live":
                    bundle = await fetch_history(
                        self.registry.all(), hourly_days=0,
                        five_minute_days=max(2, self.settings.stats_window // 288 + 1),
                    )
            except Exception as exc:
                log.warning("statistics history unavailable", extra={"error": str(exc)})
                return 0
        if bundle is None:
            return 0
        by_time: dict[Any, dict[str, Decimal]] = {}
        for key, history in bundle.instruments.items():
            if key not in self.specs:
                continue
            for at, close in history.closes("5m"):
                by_time.setdefault(at, {})[key] = close
        for at in sorted(by_time):
            self.stats.observe_many(by_time[at], at)
        log.info("statistics seeded from real history",
                 extra={"samples": len(by_time), "instruments": len(bundle.instruments)})
        return len(by_time)

    def tickers(self, venue: str | None = None) -> dict[str, Ticker]:
        return self.market.tickers(venue)

    def ticker(self, key: str) -> Ticker:
        return self.market.ticker(key)

    def set_scenario(
        self, scenario: MarketScenario, venue: str | None = None, **overrides: object
    ) -> dict[str, Any]:
        profile = self.market.set_scenario(scenario, venue, **overrides)
        return {
            "scenario": profile.scenario.value,
            "description": profile.description,
            "venue": venue or "ALL",
            "active": self.market.active_scenarios(),
        }

    def apply_shock(self, underlying: str, pct_move: Decimal) -> dict[str, str]:
        price = self.market.apply_shock(underlying, dec(pct_move))
        return {"underlying": underlying, "price": str(price), "pct_move": str(pct_move)}

    def set_funding_rate(self, instrument_key: str, rate: Decimal) -> dict[str, str]:
        self.market.set_funding_rate(instrument_key, dec(rate))
        return {"instrument": instrument_key, "funding_rate": str(rate)}

    async def persist_market_snapshot(self) -> int:
        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            tickers = self.market.tickers()
            for ticker in tickers.values():
                await obs.record_ticker(ticker, source=self.settings.market_data_source)
            return len(tickers)

    # ==================================================================
    # hedge calculation
    # ==================================================================
    async def build_inputs(
        self,
        *,
        source_key: str,
        hedge_key: str,
        source_quantity: Decimal,
        objective: HedgeObjective,
        target_ratio: Decimal = Decimal(1),
        risk_params: RiskParameters | None = None,
        use_live_positions: bool = True,
    ) -> HedgeInputs:
        source_spec = self.registry.get(source_key)
        hedge_spec = self.registry.get(hedge_key)
        hedge_venue = self.venues[hedge_spec.venue]

        source_position = Position(venue=source_spec.venue, symbol=source_spec.symbol)
        hedge_position = Position(venue=hedge_spec.venue, symbol=hedge_spec.symbol)
        hedge_account: AccountSnapshot | None = None
        if use_live_positions:
            source_position = await self._position(source_spec)
            hedge_position = await self._position(hedge_spec)
            hedge_account = await hedge_venue.get_balance()

        return HedgeInputs(
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_quantity=source_quantity,
            source_ticker=self.market.ticker(source_key),
            hedge_ticker=self.market.ticker(hedge_key),
            objective=objective,
            target_ratio=target_ratio,
            current_hedge_quantity=hedge_position.quantity,
            source_entry_price=source_position.average_entry or None,
            hedge_entry_price=hedge_position.average_entry or None,
            hedge_book=self.market.orderbook(hedge_key, 12),
            hedge_account=hedge_account,
            risk_params=self.risk_parameters_for(source_key, hedge_key, risk_params)[0],
            account_currency=self.settings.account_currency,
        )

    def risk_parameters_for(
        self,
        source_key: str,
        hedge_key: str,
        base: RiskParameters | None = None,
    ) -> tuple[RiskParameters, PairEstimate]:
        """Overlay measured statistics onto the caller's parameters.

        Falls back to the supplied (or default) assumptions when there is not
        yet enough data, and says so through ``provenance`` -- a number derived
        from twelve observations should not be presented as a measurement.
        """
        params = base or RiskParameters()
        estimate = self.stats.pair_estimate(source_key, hedge_key)
        if not estimate.is_reliable:
            return params, estimate
        return (
            params.with_estimates(
                source_daily_vol=estimate.source.daily_volatility,
                hedge_daily_vol=estimate.hedge.daily_volatility,
                correlation=estimate.correlation,
                provenance=estimate.note,
            ),
            estimate,
        )

    async def calculate_hedge(self, **kwargs: Any) -> HedgeCalculation:
        inputs = await self.build_inputs(**kwargs)
        return self.calculator.calculate(inputs)

    async def optimize_hedge(
        self, config: OptimizerConfig, **kwargs: Any
    ) -> OptimizationResult:
        inputs = await self.build_inputs(**kwargs)
        return self.optimizer.optimize(inputs, config)

    # ==================================================================
    # execution
    # ==================================================================
    async def execute_hedge(
        self,
        mapping_name: str,
        source_quantity: Decimal,
        *,
        objective: HedgeObjective | None = None,
        target_ratio: Decimal | None = None,
        optimizer: OptimizerConfig | None = None,
        risk_params: RiskParameters | None = None,
        reason: str = "operator request",
    ) -> ExecutionResult:
        """Run a full hedge cycle.  Blocked when trading is paused."""
        mapping = self.registry.mapping_by_name(mapping_name)
        self._guard_trading(mapping)
        request = HedgeRequest(
            mapping=mapping,
            source_quantity=dec(source_quantity),
            objective=objective,
            target_ratio=target_ratio,
            optimizer=optimizer,
            risk_params=risk_params or RiskParameters(),
            reason=reason,
        )
        result = await self.coordinator.execute(request)
        await self._persist_positions()
        await self._audit(
            action="EXECUTE_HEDGE", entity_type="hedge_cycle",
            entity_id=result.cycle.cycle_id,
            after={"state": result.cycle.state.value, "pair": mapping.name,
                   "source_quantity": str(source_quantity), "reason": reason},
        )
        return result

    async def assess_rebalance(self, mapping_name: str) -> RebalanceAssessment:
        mapping = self.registry.mapping_by_name(mapping_name)
        return await self.rebalancer.assess(mapping=mapping)

    async def rebalance(self, mapping_name: str) -> dict[str, Any]:
        mapping = self.registry.mapping_by_name(mapping_name)
        self._guard_trading(mapping)
        with correlation_scope(new_correlation_id()):
            before = await self.rebalancer.assess(mapping=mapping)
            if not before.needs_rebalance:
                return {"rebalanced": False, "assessment": before.to_dict()}
            orders = await self.rebalancer.execute(before)
            after = await self.rebalancer.assess(mapping=mapping)
            await self._persist_positions()
            await self._audit(
                action="REBALANCE", entity_type="hedge_pair", entity_id=mapping.name,
                before={"residual_bps": str(quantize(before.residual_bps, 4))},
                after={"residual_bps": str(quantize(after.residual_bps, 4))},
            )
            return {
                "rebalanced": True,
                "orders": [o.order_id for o in orders],
                "before": before.to_dict(),
                "after": after.to_dict(),
            }

    async def open_source_position(
        self, instrument_key: str, quantity: Decimal
    ) -> Order:
        """Take a naked position on the source venue.

        Used by the demo to create something that *needs* hedging.  It goes
        through the same paper venue as everything else -- there is no
        back-door that writes a position directly.
        """
        spec = self.registry.get(instrument_key)
        signed = dec(quantity)
        request = OrderRequest(
            venue=spec.venue, symbol=spec.symbol,
            side=Side.BUY if signed > ZERO else Side.SELL,
            quantity=abs(signed),
        )
        order = await self.venues[spec.venue].place_order(request)
        await self.recorder.record_order(order, None)
        await self._persist_positions()
        return order

    # ==================================================================
    # risk
    # ==================================================================
    async def pair_risk(self, mapping_name: str) -> PairRisk:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_position = await self._position(source_spec)
        hedge_position = await self._position(hedge_spec)
        funding = await self.funding_projection(mapping_name)
        return self.risk_engine.evaluate(PairRiskInputs(
            pair_name=mapping.name,
            source_spec=source_spec,
            hedge_spec=hedge_spec,
            source_position=source_position,
            hedge_position=hedge_position,
            source_ticker=self.market.ticker(source_spec.key),
            hedge_ticker=self.market.ticker(hedge_spec.key),
            source_account=await self.venues[source_spec.venue].get_balance(),
            hedge_account=await self.venues[hedge_spec.venue].get_balance(),
            funding_per_day=funding.net_per_day,
            daily_pnl=await self._daily_pnl(),
            account_currency=self.settings.account_currency,
        ))

    async def all_pair_risks(self) -> list[PairRisk]:
        risks: list[PairRisk] = []
        for mapping in self.registry.mappings(enabled_only=True):
            try:
                risks.append(await self.pair_risk(mapping.name))
            except VenueError as exc:
                log.error(
                    "cannot evaluate pair risk: venue unreachable",
                    extra={"pair": mapping.name, "error": str(exc)},
                )
        return risks

    async def portfolio_risk(self) -> PortfolioRisk:
        risks = await self.all_pair_risks()
        accounts = await self.accounts()
        underlying_by_pair = {
            m.name: self.registry.get(m.source_key).effective_underlying_key
            for m in self.registry.mappings(enabled_only=True)
        }
        currency_exposure = await self._currency_exposure()
        return self.portfolio_engine.evaluate(
            risks, accounts,
            underlying_by_pair=underlying_by_pair,
            currency_exposure=currency_exposure,
            currency=self.settings.account_currency,
        )

    async def persist_risk(self) -> dict[str, int]:
        """Snapshot risk, margin and P&L into the audit tables."""
        pair_risks = await self.all_pair_risks()
        portfolio = await self.portfolio_risk()
        accounts = await self.accounts()
        written = {"risk_events": 0, "margin_snapshots": 0, "pnl_records": 0}

        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            for risk in pair_risks:
                for breach in risk.breaches:
                    await obs.record_risk_event(
                        level=breach.level.value, scope="PAIR", metric=breach.metric,
                        value=breach.value, threshold=breach.threshold,
                        message=breach.message, pair_name=risk.pair_name,
                    )
                    written["risk_events"] += 1
            for breach in portfolio.breaches:
                await obs.record_risk_event(
                    level=breach.level.value, scope="PORTFOLIO", metric=breach.metric,
                    value=breach.value, threshold=breach.threshold, message=breach.message,
                )
                written["risk_events"] += 1
            for account in accounts:
                await obs.record_margin_snapshot(account)
                written["margin_snapshots"] += 1
            for mapping in self.registry.mappings(enabled_only=True):
                breakdown = await self.pair_pnl(mapping.name)
                await obs.record_pnl(mapping.name, breakdown.to_dict(), pair_name=mapping.name)
                written["pnl_records"] += 1
        return written

    # ==================================================================
    # emergency handling
    # ==================================================================
    async def apply_emergency_actions(
        self, level: RiskLevel, actions: tuple[EmergencyAction, ...], trigger: str,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute the risk engine's prescribed actions and record each one."""
        applied: list[dict[str, Any]] = []
        for action in actions:
            executed, result = await self._apply_action(action, scope)
            applied.append({"action": action.value, "executed": executed, "result": result})
            async with self.database.session() as session:
                await ObservabilityRepository(session).record_emergency_action(
                    trigger=trigger, level=level.value, action=action.value,
                    scope=scope, executed=executed, result=result,
                )
        return applied

    async def _apply_action(
        self, action: EmergencyAction, scope: str | None
    ) -> tuple[bool, str]:
        if action is EmergencyAction.STOP_NEW_TRADES:
            self.state.trading_paused = True
            return True, "new hedge cycles are blocked"
        if action is EmergencyAction.PAUSE_PAIR:
            if scope:
                self.state.paused_pairs.add(scope)
                return True, f"pair {scope} paused"
            return False, "no pair scope supplied"
        if action is EmergencyAction.ENTER_EMERGENCY_MODE:
            self.state.emergency_mode = True
            return True, "emergency mode engaged"
        if action is EmergencyAction.CANCEL_OPEN_ORDERS:
            cancelled = await self.cancel_all_orders()
            return True, f"cancelled {cancelled} working orders"
        if action is EmergencyAction.REBALANCE:
            if scope:
                outcome = await self.rebalancer.assess(
                    mapping=self.registry.mapping_by_name(scope)
                )
                if outcome.needs_rebalance:
                    orders = await self.rebalancer.execute(outcome)
                    return True, f"rebalanced with {len(orders)} order(s)"
                return False, outcome.reason
            return False, "no pair scope supplied"
        if action is EmergencyAction.REDUCE_EXPOSURE:
            return await self._reduce_exposure(scope)
        if action is EmergencyAction.FLATTEN_POSITIONS:
            return await self._flatten(scope)
        return False, f"unhandled action {action.value}"

    async def _reduce_exposure(self, scope: str | None) -> tuple[bool, str]:
        """Halve the source leg and re-hedge, rather than closing outright."""
        mappings = (
            [self.registry.mapping_by_name(scope)] if scope
            else self.registry.mappings(enabled_only=True)
        )
        messages: list[str] = []
        for mapping in mappings:
            source_spec = self.registry.get(mapping.source_key)
            position = await self._position(source_spec)
            if position.is_flat:
                continue
            reduction = -position.quantity / Decimal(2)
            request = HedgeRequest(
                mapping=mapping, source_quantity=reduction,
                bypass_risk=True, reason="emergency exposure reduction",
            )
            result = await self.coordinator.execute(request)
            messages.append(f"{mapping.name}: {result.cycle.state.value}")
        await self._persist_positions()
        return bool(messages), "; ".join(messages) or "no open source positions to reduce"

    async def _flatten(self, scope: str | None) -> tuple[bool, str]:
        """Close every position, then **verify** it worked.

        Reporting success without checking is how a kill switch quietly leaves
        exposure open after a partial fill.  The remaining positions are named
        in the result so the operator sees exactly what is still on the book.
        """
        mappings = (
            [self.registry.mapping_by_name(scope)] if scope
            else self.registry.mappings()
        )
        total = 0
        for mapping in mappings:
            orders = await self.rebalancer.flatten(mapping)
            total += len(orders)
        await self._persist_positions()

        unreachable = [
            venue.name for venue in self.venues.values()
            if (engine := getattr(venue, "engine", None)) is not None
            and not engine.is_connected
        ]
        remaining = [p for p in await self.positions() if not p.is_flat]
        if unreachable:
            detail = ", ".join(f"{p.key}={p.quantity}" for p in remaining) or "none visible"
            log.error(
                "flatten could not confirm every venue",
                extra={"unreachable": unreachable, "remaining": detail},
            )
            return False, (
                f"{total} closing order(s) placed, but {', '.join(unreachable)} "
                f"could not be reached, so its exposure is unconfirmed "
                f"(visible remainder: {detail})"
            )
        if remaining:
            detail = ", ".join(f"{p.key}={p.quantity}" for p in remaining)
            log.error(
                "flatten left open positions",
                extra={"remaining": detail, "orders": total},
            )
            return False, (
                f"{total} closing order(s) placed but {len(remaining)} position(s) "
                f"remain open: {detail}"
            )
        return True, f"flattened successfully with {total} closing order(s)"

    # ------------------------------------------------------------------
    # persisted control state
    # ------------------------------------------------------------------
    async def heartbeat(self, component: str = "api") -> None:
        """Record that this process is alive; the watchdog reads it."""
        async with self.database.session() as session:
            await ControlStateRepository(session).set(f"heartbeat:{component}", {
                "pid": os.getpid(), "at": utcnow().isoformat(),
                "market_clock": self.market.clock.isoformat() if self.market.has_data else None,
            })

    async def _persist_kill_switch(self, engaged: bool, reason: str, actor: str) -> None:
        async with self.database.session() as session:
            await ControlStateRepository(session).set(KILL_SWITCH_KEY, {
                "engaged": engaged, "reason": reason, "actor": actor,
                "at": utcnow().isoformat(),
            })

    async def _watchdog_status(self) -> dict[str, Any]:
        async with self.database.session() as session:
            row = await ControlStateRepository(session).get("heartbeat:watchdog")
        if row is None:
            return {"running": False, "detail": "no watchdog has checked in"}
        age = (utcnow() - datetime.fromisoformat(row.value["at"])).total_seconds()
        return {"running": age < 60, "last_check_age_s": round(age, 1),
                "verdict": row.value.get("verdict")}

    async def _restore_kill_switch(self) -> None:
        """A kill switch survives restarts: a crashed process must not come
        back trading because its in-memory flag was lost."""
        async with self.database.session() as session:
            row = await ControlStateRepository(session).get(KILL_SWITCH_KEY)
        if row is not None and row.value.get("engaged"):
            self.state.kill_switch_engaged = True
            self.state.trading_paused = True
            self.state.emergency_mode = True
            log.error("kill switch is engaged from a previous run; trading stays disabled",
                      extra={"reason": row.value.get("reason"), "actor": row.value.get("actor")})

    async def engage_kill_switch(self, reason: str, actor: str = "operator") -> dict[str, Any]:
        """Stop everything and flatten every position.

        Deliberately not reversible from inside the API: clearing it is a
        separate, audited call.
        """
        with correlation_scope(new_correlation_id()):
            log.error("KILL SWITCH ENGAGED", extra={"reason": reason, "actor": actor})
            self.state.kill_switch_engaged = True
            self.state.trading_paused = True
            self.state.emergency_mode = True
            # Persist first: if flattening crashes, the flag must already be durable.
            await self._persist_kill_switch(True, reason, actor)
            cancelled = await self.cancel_all_orders()
            flattened, message = await self._flatten(None)
            async with self.database.session() as session:
                obs = ObservabilityRepository(session)
                for action, executed, result in (
                    (EmergencyAction.STOP_NEW_TRADES, True, "trading halted"),
                    (EmergencyAction.CANCEL_OPEN_ORDERS, True, f"{cancelled} orders cancelled"),
                    (EmergencyAction.FLATTEN_POSITIONS, flattened, message),
                    (EmergencyAction.ENTER_EMERGENCY_MODE, True, "emergency mode engaged"),
                ):
                    await obs.record_emergency_action(
                        trigger=reason, level=RiskLevel.KILL_SWITCH.value,
                        action=action.value, scope=None, executed=executed,
                        result=result, actor=actor,
                    )
                await obs.record_audit(
                    actor=actor, action="KILL_SWITCH", entity_type="system",
                    entity_id=None, before={"kill_switch_engaged": False},
                    after={"kill_switch_engaged": True, "reason": reason},
                )
            return {
                "engaged": True, "reason": reason, "flattened": flattened,
                "orders_cancelled": cancelled, "flatten_result": message,
            }

    async def clear_kill_switch(self, actor: str = "operator") -> dict[str, Any]:
        self.state.kill_switch_engaged = False
        self.state.trading_paused = False
        self.state.emergency_mode = False
        self.state.paused_pairs.clear()
        await self._persist_kill_switch(False, "cleared", actor)
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_audit(
                actor=actor, action="CLEAR_KILL_SWITCH", entity_type="system",
                entity_id=None, before={"kill_switch_engaged": True},
                after={"kill_switch_engaged": False},
            )
        return {"engaged": False}

    async def cancel_all_orders(self) -> int:
        cancelled = 0
        for venue in self.venues.values():
            try:
                for order in await venue.get_open_orders():
                    await venue.cancel_order(order.order_id)
                    cancelled += 1
            except VenueError as exc:
                log.error(
                    "cannot cancel orders on an unreachable venue",
                    extra={"venue": venue.name, "error": str(exc)},
                )
        return cancelled

    # ==================================================================
    # costs and P&L
    # ==================================================================
    async def funding_projection(
        self, mapping_name: str, horizon_days: Decimal = Decimal(1)
    ) -> FundingProjection:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_position = await self._position(source_spec)
        hedge_position = await self._position(hedge_spec)
        source_ticker = self.market.ticker(source_spec.key)
        hedge_ticker = self.market.ticker(hedge_spec.key)
        acct = self.settings.account_currency
        return project_funding(
            source_spec=source_spec, hedge_spec=hedge_spec,
            source_quantity=source_position.quantity,
            hedge_quantity=hedge_position.quantity,
            source_price=source_ticker.mid, hedge_price=hedge_ticker.mid,
            source_funding_rate=source_ticker.funding_rate,
            hedge_funding_rate=hedge_ticker.funding_rate,
            source_fx=self.fx.try_rate(source_spec.quote_asset, acct),
            hedge_fx=self.fx.try_rate(hedge_spec.quote_asset, acct),
            horizon_days=horizon_days, currency=acct,
        )

    async def pair_pnl(self, mapping_name: str) -> PnLBreakdown:
        mapping = self.registry.mapping_by_name(mapping_name)
        source_spec = self.registry.get(mapping.source_key)
        hedge_spec = self.registry.get(mapping.hedge_key)
        source_leg = self._leg_accounting(source_spec)
        hedge_leg = self._leg_accounting(hedge_spec)
        return self.pnl_calculator.for_pair(
            scope=mapping.name, source=source_leg, hedge=hedge_leg,
            source_ticker=self.market.ticker(source_spec.key),
            hedge_ticker=self.market.ticker(hedge_spec.key),
        )

    def _leg_accounting(self, spec: InstrumentSpec) -> LegAccounting:
        """Assemble a leg's cost tally from the paper engine's own records."""
        engine = getattr(self.venues[spec.venue], "engine", None)
        position = Position(venue=spec.venue, symbol=spec.symbol)
        leg = LegAccounting(spec=spec, position=position)
        if engine is None:
            return leg
        leg.position = engine.positions.get(spec.key, position)
        order_ids = {
            order.order_id for order in engine.orders.values()
            if order.request.symbol == spec.symbol
        }
        leg.fills = [f for f in engine.fills if f.order_id in order_ids]
        for accrual in engine.funding_history:
            if accrual.symbol != spec.symbol:
                continue
            if accrual.kind == "SWAP":
                leg.swap_financing += accrual.amount
            elif accrual.amount >= ZERO:
                leg.funding_received += accrual.amount
            else:
                leg.funding_paid += -accrual.amount
        return leg

    def settle_funding(self, hours: Decimal | None = Decimal(8)) -> list[dict[str, Any]]:
        """Charge ``hours`` of funding on every venue at the current rates.

        The venues fund on different clocks -- Delta every 8 hours (4 for
        PAXG), Hyperliquid every hour -- so settling "one interval" each
        would compare 8 hours of carry against 1.  The default settles the
        same 8 hours on both: one Delta interval, eight Hyperliquid ones.
        """
        accruals: list[dict[str, Any]] = []
        for venue in self.venues.values():
            engine = getattr(venue, "engine", None)
            if engine is None:
                continue
            accruals.extend(a.to_dict() for a in engine.apply_funding(hours))
        return accruals

    async def persist_funding(self, accruals: list[dict[str, Any]]) -> int:
        async with self.database.session() as session:
            obs = ObservabilityRepository(session)
            for accrual in accruals:
                await obs.record_funding(
                    venue=str(accrual["venue"]), symbol=str(accrual["symbol"]),
                    kind=str(accrual["kind"]), rate=dec(accrual["rate"]),
                    amount=dec(accrual["amount"]), currency=str(accrual["currency"]),
                )
        return len(accruals)

    # ==================================================================
    # reconciliation
    # ==================================================================
    async def reconcile(self) -> ReconciliationReport:
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            open_orders = [o.order_id for o in await OrderRepository(session).open_orders()]
        report = await self.reconciler.reconcile(positions, open_orders)
        self.state.last_reconciliation = report
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_system_event(
                "RECONCILIATION", "WARNING" if not report.is_clean else "INFO",
                "reconciliation",
                f"{len(report.discrepancies)} discrepancies across {len(report.checked_venues)} venues",
                report.to_dict(),
            )
        return report

    async def reconcile_records(self) -> list[RecordReport]:
        """Fill- and funding-level reconciliation of the database against
        each venue's own records."""
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            order_rows = await OrderRepository(session).list(limit=100_000)
            fill_rows = await OrderRepository(session).fills(limit=100_000)
            funding_rows = await ObservabilityRepository(session).funding_history(limit=100_000)
        orders = {o.order_id: o for o in order_rows}
        reports: list[RecordReport] = []
        for name, venue in self.venues.items():
            engine = getattr(venue, "engine", None)
            if engine is None:
                continue
            fills = [
                FillRecord(f.exec_id, f.order_id, orders[f.order_id].symbol,
                           1 if orders[f.order_id].side == "BUY" else -1,
                           f.quantity, f.price, f.fee, f.timestamp)
                for f in fill_rows
                if f.order_id in orders and orders[f.order_id].venue == name
            ]
            funding = [FundingRecord(r.symbol, r.amount, r.timestamp)
                       for r in funding_rows if r.venue == name]
            reports.append(reconcile_records(
                records_from_paper_engine(engine), internal_positions=positions,
                internal_fills=fills, expected_funding=funding,
            ))
        return reports

    async def recover(self) -> RecoveryPlan:
        """Restart recovery: rebuild state and decide whether to resume."""
        async with self.database.session() as session:
            positions = await PositionRepository(session).to_domain()
            open_orders = [o.order_id for o in await OrderRepository(session).open_orders()]
            cycles = [
                {
                    "cycle_id": row.cycle_id, "pair_name": row.pair_name, "state": row.state,
                    "source_filled_quantity": str(row.source_filled_quantity),
                    "hedge_filled_quantity": str(row.hedge_filled_quantity),
                }
                for row in await CycleRepository(session).unfinished()
            ]
        plan = await self.reconciler.recover(
            database_positions=positions, unfinished_cycles=cycles,
            database_open_orders=open_orders,
        )
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_system_event(
                "RESTART_RECOVERY", "INFO" if plan.resumable else "WARNING", "reconciliation",
                f"recovery {'can' if plan.resumable else 'cannot'} resume automatically",
                plan.to_dict(),
            )
        return plan

    async def adopt_venue_state(self, actor: str = "operator") -> dict[str, Any]:
        """Overwrite the database with what the venues actually hold."""
        positions = await self.reconciler.adopt_venue_state()
        async with self.database.session() as session:
            repo = PositionRepository(session)
            before = [
                {"key": p.key, "quantity": str(p.quantity)} for p in await repo.to_domain()
            ]
            await repo.clear()
            for position in positions:
                await repo.upsert(position)
            await ObservabilityRepository(session).record_audit(
                actor=actor, action="ADOPT_VENUE_STATE", entity_type="positions",
                entity_id=None, before={"positions": before},
                after={"positions": [{"key": p.key, "quantity": str(p.quantity)} for p in positions]},
            )
        return {"adopted": len(positions)}

    # ==================================================================
    # fault injection
    # ==================================================================
    async def inject_fault(
        self,
        kind: FaultKind,
        *,
        venue: str | None = None,
        symbol: str | None = None,
        leg: str | None = None,
        count: int | None = 1,
        magnitude: Decimal = Decimal("0.5"),
        probability: Decimal = Decimal(1),
        reason: str = "operator-injected fault",
    ) -> dict[str, Any]:
        """Arm a fault, or apply one that acts immediately.

        Disconnects, stale data and price gaps are *states*, not events, so they
        are applied directly to the market feed as overlays instead of being armed.
        """
        immediate = await self._apply_immediate_fault(kind, venue, symbol, magnitude)
        if immediate is not None:
            await self._audit(
                action="INJECT_FAULT", entity_type="fault", entity_id=kind.value,
                after={"kind": kind.value, "venue": venue, "immediate": True, **immediate},
            )
            return {"kind": kind.value, "armed": False, "applied": True, **immediate}

        fault: ArmedFault = self.faults.arm(
            kind, venue=venue, symbol=symbol, leg=leg, count=count,
            probability=probability, magnitude=magnitude, reason=reason,
        )
        await self._audit(
            action="INJECT_FAULT", entity_type="fault", entity_id=kind.value,
            after=fault.to_dict(),
        )
        return {"kind": kind.value, "armed": True, "applied": False, "fault": fault.to_dict()}

    async def _apply_immediate_fault(
        self, kind: FaultKind, venue: str | None, symbol: str | None, magnitude: Decimal
    ) -> dict[str, Any] | None:
        if kind is FaultKind.DELTA_DISCONNECT:
            return self._disconnect("PAPER_DELTA")
        if kind is FaultKind.HYPERLIQUID_DISCONNECT:
            return self._disconnect("PAPER_HYPERLIQUID")
        if kind is FaultKind.STALE_MARKET_DATA:
            self.market.set_scenario(MarketScenario.STALE_DATA, venue)
            return {"scenario": MarketScenario.STALE_DATA.value, "venue": venue or "ALL"}
        if kind is FaultKind.WIDE_SPREAD:
            self.market.set_scenario(MarketScenario.SPREAD_WIDENING, venue)
            return {"scenario": MarketScenario.SPREAD_WIDENING.value, "venue": venue or "ALL"}
        if kind is FaultKind.PRICE_GAP:
            underlying = symbol or "BTC"
            price = self.market.apply_shock(underlying, -abs(magnitude))
            return {"underlying": underlying, "new_price": str(price),
                    "pct_move": str(-abs(magnitude))}
        if kind is FaultKind.UNEXPECTED_POSITION:
            target_venue = venue or "PAPER_HYPERLIQUID"
            engine = getattr(self.venues[target_venue], "engine", None)
            if engine is None:
                return {"error": f"{target_venue} has no paper engine"}
            target_symbol = symbol or next(iter(engine.instruments.values())).symbol
            spec = engine.spec(target_symbol)
            price = self.market.ticker(spec.key).mid
            quantity = spec.min_quantity * Decimal(10)
            engine.inject_unexpected_position(target_symbol, quantity, price)
            return {"venue": target_venue, "symbol": target_symbol,
                    "quantity": str(quantity),
                    "note": "position created on the venue only; the database does not know it"}
        return None

    def _disconnect(self, venue_name: str) -> dict[str, Any]:
        engine = getattr(self.venues[venue_name], "engine", None)
        if engine is None:
            return {"error": f"{venue_name} has no paper engine"}
        engine.force_disconnect(True)
        return {"venue": venue_name, "connected": False}

    def reconnect(self, venue_name: str) -> dict[str, Any]:
        engine = getattr(self.venues[venue_name], "engine", None)
        if engine is None:
            return {"error": f"{venue_name} has no paper engine"}
        engine.force_disconnect(False)
        self.market.clear_scenario(venue_name)
        return {"venue": venue_name, "connected": True}

    def clear_faults(self, kind: FaultKind | None = None) -> dict[str, Any]:
        removed = self.faults.disarm(kind)
        for name in self.venues:
            self.reconnect(name)
        self.market.clear_scenario()
        return {"disarmed": removed, "scenario": "NORMAL", "venues_reconnected": list(self.venues)}

    # ==================================================================
    # accounts and status
    # ==================================================================
    async def accounts(self) -> list[AccountSnapshot]:
        snapshots: list[AccountSnapshot] = []
        for venue in self.venues.values():
            snapshots.append(await venue.get_balance())
        return snapshots

    async def positions(self) -> list[Position]:
        positions: list[Position] = []
        for venue in self.venues.values():
            try:
                positions.extend(await venue.get_positions())
            except VenueError:
                continue
        return positions

    async def status(self) -> dict[str, Any]:
        venue_health: list[dict[str, Any]] = []
        for venue in self.venues.values():
            venue_health.append(await venue.health())
        return {
            "app": self.settings.app_name,
            "version": self.settings.version,
            "environment": self.settings.environment,
            "trading_mode": self.settings.trading_mode.value,
            "paper_only": True,
            "live_adapters_registered": 0,
            "watchdog": await self._watchdog_status(),
            "hard_limit_breaches": [
                b.to_dict() for venue in self.venues.values()
                for b in getattr(venue, "breaches", [])[-20:]
            ],
            "database_connected": await self.database.ping(),
            "venues": venue_health,
            "instruments": len(self.specs),
            "mappings": len(self.registry.mappings()),
            "market_scenarios": self.market.active_scenarios(),
            "armed_faults": [f.to_dict() for f in self.faults.armed()],
            "emergency_mode": self.state.emergency_mode,
            "trading_paused": self.state.trading_paused,
            "kill_switch_engaged": self.state.kill_switch_engaged,
            "paused_pairs": sorted(self.state.paused_pairs),
            "market_clock": self.market.clock.isoformat() if self.market.has_data else None,
            "market_data_source": self.market.source,
            "market_overlays": self.market.active_overlays(),
        }

    # ==================================================================
    # internals
    # ==================================================================
    def _guard_trading(self, mapping: HedgeMapping) -> None:
        if self.state.kill_switch_engaged:
            raise PermissionError("kill switch is engaged; trading is disabled")
        if self.state.trading_paused:
            raise PermissionError("trading is paused by a risk action")
        if mapping.name in self.state.paused_pairs:
            raise PermissionError(f"hedge pair {mapping.name!r} is paused")

    async def _position(self, spec: InstrumentSpec) -> Position:
        venue = self.venues[spec.venue]
        for position in await venue.get_positions():
            if position.symbol == spec.symbol:
                return position
        return Position(venue=spec.venue, symbol=spec.symbol)

    async def _persist_positions(self) -> None:
        async with self.database.session() as session:
            repo = PositionRepository(session)
            for venue in self.venues.values():
                engine = getattr(venue, "engine", None)
                if engine is None:
                    continue
                for position in engine.positions.values():
                    await repo.upsert(position)

    async def _currency_exposure(self) -> dict[str, Decimal]:
        exposure: dict[str, Decimal] = {}
        for position in await self.positions():
            spec = self.registry.find(position.key)
            if spec is None or position.is_flat:
                continue
            from .domain.quantity import QuantityConverter

            notional = QuantityConverter(spec).notional_quote(
                position.quantity, self.market.ticker(spec.key).mid
            )
            exposure[spec.settlement_asset] = exposure.get(spec.settlement_asset, ZERO) + notional
        return exposure

    async def _daily_pnl(self) -> Decimal:
        async with self.database.session() as session:
            return await ObservabilityRepository(session).daily_pnl()

    def _risk_thresholds(self) -> RiskThresholds:
        s = self.settings
        return RiskThresholds(
            warning_margin_level=s.warning_margin_level,
            danger_margin_level=s.danger_margin_level,
            emergency_margin_level=s.emergency_margin_level,
            kill_switch_margin_level=s.kill_switch_margin_level,
            max_daily_loss=s.max_daily_loss,
            warning_residual_bps=s.max_residual_exposure_bps,
            max_spread_bps=s.max_spread_bps_for_execution,
        )

    def _portfolio_limits(self) -> PortfolioLimits:
        s = self.settings
        return PortfolioLimits(
            max_total_notional=s.max_portfolio_notional,
            max_daily_loss=s.max_daily_loss,
            max_concentration_pct=s.max_concentration_pct,
        )

    async def _audit(
        self, *, action: str, entity_type: str, entity_id: str | None,
        before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> None:
        async with self.database.session() as session:
            await ObservabilityRepository(session).record_audit(
                actor=actor, action=action, entity_type=entity_type,
                entity_id=entity_id, before=before, after=after,
            )
