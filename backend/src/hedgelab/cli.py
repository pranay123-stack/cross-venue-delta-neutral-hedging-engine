"""Command line interface.

A thin shell over :class:`HedgeLabService` -- the CLI and the API cannot
disagree about behaviour because they call the same methods.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .backtest.engine import BacktestConfig, Backtester
from .config import Settings, get_settings
from .domain.enums import HedgeObjective, OptimizerMode
from .hedge.optimizer import OptimizerConfig
from .instruments.registry import InstrumentRegistry
from .logging_setup import configure_logging
from .scenarios.runner import ScenarioRunner
from .service import HedgeLabService

console = Console()
app = typer.Typer(
    add_completion=False,
    help="HedgeLab -- Delta Exchange / Hyperliquid paper hedging on real market data. PAPER TRADING ONLY.",
)
instruments_app = typer.Typer(help="Inspect the instrument catalogue.")
scenario_app = typer.Typer(help="Run the predefined demo scenarios.")
backtest_app = typer.Typer(help="Replay real venue history through the hedge engine.")
market_app = typer.Typer(help="Record and fetch real market data from the venues.")
app.add_typer(instruments_app, name="instruments")
app.add_typer(market_app, name="market")
app.add_typer(scenario_app, name="scenario")
app.add_typer(backtest_app, name="backtest")

D = Decimal


def _settings(verbose: bool = False) -> Settings:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else "WARNING", json_output=False)
    return settings


async def _with_service(fn: Any, *, create_schema: bool = True) -> Any:
    settings = get_settings()
    service = HedgeLabService(settings)
    try:
        await service.startup(
            create_schema=create_schema or settings.database_url.startswith("sqlite")
        )
        service.advance_market(20)
        return await fn(service)
    finally:
        await service.shutdown()


def _banner() -> None:
    console.print(Panel.fit(
        "[bold]HedgeLab[/bold]  --  Delta Exchange <-> Hyperliquid hedging, real market data\n"
        "[yellow]PAPER TRADING ONLY.[/yellow] No live adapter is registered; "
        "no order can reach a real venue.",
        border_style="yellow",
    ))


# ======================================================================
# instruments
# ======================================================================
@instruments_app.command("list")
def instruments_list(venue: str | None = typer.Option(None, help="Filter by venue")) -> None:
    """Show every configured instrument and how its quantity unit is sized."""
    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    table = Table(title="Instrument catalogue", show_lines=False)
    for column in ("Key", "Type", "Unit", "Sizing", "Quote", "Settle",
                   "Step", "Min", "Fees bps", "Financing"):
        table.add_column(column, overflow="fold")
    for spec in registry.all():
        if venue and spec.venue != venue:
            continue
        financing = (
            f"funding {spec.baseline_funding_rate}/{spec.funding_interval_hours}h"
            if spec.funding_model.value == "PERPETUAL_FUNDING"
            else f"swap {spec.swap_long_points}/{spec.swap_short_points} pts"
            if spec.funding_model.value == "SWAP_POINTS" else "none"
        )
        table.add_row(
            spec.key, spec.instrument_type.value, spec.quantity_unit.value,
            spec.describe_sizing(), spec.quote_asset, spec.settlement_asset,
            str(spec.quantity_step), str(spec.min_quantity),
            f"{spec.maker_fee_bps}/{spec.taker_fee_bps}", financing,
        )
    console.print(table)


@instruments_app.command("pairs")
def instruments_pairs() -> None:
    """Show the configured hedge pairs and their unit conversions."""
    from .domain.quantity import describe_conversion

    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    table = Table(title="Hedge pairs")
    for column in ("Pair", "Objective", "Tolerance", "Conversion", "Enabled"):
        table.add_column(column, overflow="fold")
    for mapping in registry.mappings():
        source = registry.get(mapping.source_key)
        hedge = registry.get(mapping.hedge_key)
        conversion = (
            "inverse sizing depends on price; see /api/instrument-mappings"
            if source.is_inverse or hedge.is_inverse
            # Linear sizing is price independent, so no quote is needed.
            else describe_conversion(source, hedge, D(1), D(1))
        )
        table.add_row(
            mapping.name, mapping.objective.value, f"{mapping.tolerance_bps} bps",
            conversion, "yes" if mapping.enabled else "no",
        )
    console.print(table)


@instruments_app.command("sync")
def instruments_sync(
    write: bool = typer.Option(False, "--write", help="Overwrite the catalogue YAML"),
) -> None:
    """Regenerate the catalogue from Delta's and Hyperliquid's published specs."""
    from .instruments.sync import fetch_catalogue, mappings_for, write_catalogue

    settings = _settings()
    delta_specs, hl_specs = asyncio.run(fetch_catalogue())
    table = Table(title="Specifications from the venues")
    for column in ("Key", "Sizing", "Tick", "Step", "Fees bps", "IM", "Funding"):
        table.add_column(column, overflow="fold")
    for row in delta_specs + hl_specs:
        sizing = f"{row['contract_size']} {row['base_asset']} per {row['quantity_unit'].lower()}"
        table.add_row(
            f"{row['venue']}:{row['symbol']}", sizing, row["tick_size"], row["quantity_step"],
            f"{row['maker_fee_bps']}/{row['taker_fee_bps']}", row["initial_margin_rate"],
            f"every {row['funding_interval_hours']}h",
        )
    console.print(table)
    if write:
        for path in write_catalogue(settings.instrument_spec_dir, delta_specs, hl_specs,
                                    mappings_for()):
            console.print(f"[green]wrote[/green] {path}")
    else:
        console.print("[dim]Dry run. Pass --write to update the catalogue.[/dim]")


# ======================================================================
# market data
# ======================================================================
@market_app.command("record")
def market_record(
    out: Path = typer.Option(..., help="Output .jsonl.gz file"),
    frames: int = typer.Option(300, help="Number of polls"),
    interval: float = typer.Option(2.0, help="Seconds between polls"),
    depth: int = typer.Option(10, help="Book levels kept per side"),
) -> None:
    """Record live frames from both venues for offline replay."""
    from .marketdata.collector import LiveCollector, record
    from .marketdata.frames import write_frames

    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    captured = asyncio.run(record(registry.all(), frames=frames, interval_seconds=interval,
                                  collector=LiveCollector(depth=depth)))
    count = write_frames(out, captured)
    gaps = sum(1 for f in captured if f.missing)
    console.print(f"[green]recorded[/green] {count} frames to {out} "
                  f"({gaps} with a venue missing)")


@market_app.command("history")
def market_history(
    out: Path = typer.Option(..., help="Output .json.gz file"),
    hourly_days: int = typer.Option(30, help="Days of hourly candles and funding"),
    five_minute_days: int = typer.Option(3, help="Days of 5-minute candles"),
) -> None:
    """Fetch real candles and settled funding for every instrument."""
    from .marketdata.history import fetch_history

    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    bundle = asyncio.run(fetch_history(registry.all(), hourly_days=hourly_days,
                                       five_minute_days=five_minute_days))
    bundle.save(out)
    for key, history in sorted(bundle.instruments.items()):
        console.print(f"{key}: {len(history.candles.get('1h', []))} hourly, "
                      f"{len(history.candles.get('5m', []))} 5-minute candles, "
                      f"{len(history.funding)} funding points")
    console.print(f"[green]saved[/green] {out}")


# ======================================================================
# hedge calculation
# ======================================================================
@app.command("calculate")
def calculate(
    source: str = typer.Option(..., help="Source instrument, VENUE:SYMBOL"),
    hedge: str = typer.Option(..., help="Hedge instrument, VENUE:SYMBOL"),
    quantity: str = typer.Option(..., help="Signed source quantity in venue units"),
    objective: str = typer.Option("QUOTE_PNL_NEUTRAL", help="Hedge objective"),
    ratio: str = typer.Option("1", help="Target hedge ratio"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Calculate a hedge and print the full derivation."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.calculate_hedge(
            source_key=source, hedge_key=hedge, source_quantity=D(quantity),
            objective=HedgeObjective(objective.upper()), target_ratio=D(ratio),
            use_live_positions=False,
        )

    result = asyncio.run(_with_service(run))
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        return

    _banner()
    table = Table(title=f"{source} -> {hedge}  ({objective})", show_lines=True)
    table.add_column("Step")
    table.add_column("Formula", overflow="fold")
    table.add_column("Value", overflow="fold")
    for step in result.steps:
        table.add_row(step.label, step.formula, step.value)
    console.print(table)

    summary = Table(title="Result", show_header=False)
    summary.add_column("Field")
    summary.add_column("Value")
    for label, value in (
        ("Required quantity", result.required_quantity),
        ("Rounded quantity", result.rounded_quantity),
        ("Hedge ratio", result.hedge_ratio),
        ("Residual (bps of source)", result.residual_bps),
        ("Execution cost", result.total_execution_cost),
        ("Funding per day", result.funding_impact_per_day),
        ("Margin required", result.margin_requirement),
        ("Expected P&L", result.expected_pnl),
        ("Worst case (99%)", result.worst_case_pnl),
        ("Break-even move %", result.break_even_move_pct),
        ("Executable", result.is_executable),
    ):
        summary.add_row(label, str(value))
    console.print(summary)
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")


@app.command("optimize")
def optimize(
    source: str = typer.Option(...), hedge: str = typer.Option(...),
    quantity: str = typer.Option(...),
    objective: str = typer.Option("BASE_ASSET_NEUTRAL"),
    mode: str = typer.Option("WEIGHTED", help="EXACT, STEP_SEARCH or WEIGHTED"),
    priority: str = typer.Option("MIN_RESIDUAL"),
    steps: int = typer.Option(5, help="Lattice points to search each side"),
) -> None:
    """Search the quantity lattice and show every candidate considered."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.optimize_hedge(
            OptimizerConfig(mode=OptimizerMode(mode.upper()), priority=priority.upper(),
                            search_steps=steps),
            source_key=source, hedge_key=hedge, source_quantity=D(quantity),
            objective=HedgeObjective(objective.upper()), use_live_positions=False,
        )

    result = asyncio.run(_with_service(run))
    table = Table(title=f"Optimiser candidates ({mode}/{priority})")
    for column in ("Quantity", "Residual bps", "Cost", "Funding/day",
                   "Margin", "Slippage", "Capital eff.", "Score"):
        table.add_column(column, justify="right")
    for candidate in result.candidates:
        marker = " <-" if candidate.quantity == result.best.quantity else ""
        table.add_row(
            f"{candidate.quantity}{marker}", f"{candidate.residual_bps:.2f}",
            f"{candidate.execution_cost:.2f}", f"{candidate.funding_per_day:.2f}",
            f"{candidate.margin:.0f}", f"{candidate.slippage:.2f}",
            f"{candidate.capital_efficiency:.2f}", f"{candidate.score:.4f}",
        )
    console.print(table)
    console.print(f"[bold]exact:[/bold] {result.exact_quantity}")
    console.print(f"[bold]chosen:[/bold] {result.best.quantity}")
    console.print(result.rationale)


# ======================================================================
# scenarios
# ======================================================================
@scenario_app.command("list")
def scenario_list() -> None:
    """List the predefined demo scenarios."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return ScenarioRunner(service).available()

    table = Table(title="Demo scenarios")
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Description")
    for definition in asyncio.run(_with_service(run)):
        table.add_row(definition["key"], definition["name"], definition["description"])
    console.print(table)


@scenario_app.command("run")
def scenario_run(
    key: str = typer.Argument(..., help="Scenario letter A-J, or its name"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run one scenario end to end against the real engine."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await ScenarioRunner(service).run(key)

    result = asyncio.run(_with_service(run))
    if as_json:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(0 if result.passed else 1)

    _banner()
    console.print(Panel.fit(
        f"[bold]{result.key} -- {result.name}[/bold]",
        border_style="green" if result.passed else "red",
    ))
    for index, step in enumerate(result.steps, 1):
        console.print(f"[bold cyan]{index:2}. {step.label}[/bold cyan]")
        console.print(f"    {step.detail}")
        for field, value in step.data.items():
            if isinstance(value, list | dict):
                value = json.dumps(value, default=str)[:400]
            console.print(f"      [dim]{field}[/dim] = {value}")
    console.print()
    console.print(Panel(result.summary or "(no summary)",
                        title="PASSED" if result.passed else "FAILED",
                        border_style="green" if result.passed else "red"))
    if result.error:
        console.print(f"[red]error:[/red] {result.error}")
    raise typer.Exit(0 if result.passed else 1)


@scenario_app.command("run-all")
def scenario_run_all() -> None:
    """Run every scenario and report a pass/fail table."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await ScenarioRunner(service).run_all()

    results = asyncio.run(_with_service(run))
    table = Table(title="Scenario results")
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Result")
    table.add_column("Summary", overflow="fold")
    for result in results:
        table.add_row(
            result.key, result.name,
            "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]",
            result.summary or result.error or "",
        )
    console.print(table)
    failures = [r for r in results if not r.passed]
    console.print(f"{len(results) - len(failures)}/{len(results)} scenarios passed")
    raise typer.Exit(1 if failures else 0)


# ======================================================================
# backtest
# ======================================================================
@backtest_app.command("run")
def backtest_run(
    source: str = typer.Option("PAPER_DELTA:BTCUSD"),
    hedge: str = typer.Option("PAPER_HYPERLIQUID:BTC"),
    quantity: str = typer.Option("1000", help="Signed source position, in source units"),
    objective: str = typer.Option("QUOTE_PNL_NEUTRAL"),
    ratio: str = typer.Option("1"),
    steps: int = typer.Option(0, help="Most recent hours to use (0 = all)"),
    tolerance_bps: str = typer.Option("25"),
    history: Path | None = typer.Option(None, help="Saved history (market history --out)"),
    days: int = typer.Option(30, help="Days to fetch when --history is not given"),
    books: Path | None = typer.Option(
        None, help="Recorded frames (market record --out) to measure book impact from"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Replay real hourly closes and settled funding through the hedge maths."""
    from .marketdata.fx import FxService
    from .marketdata.history import HistoryBundle, fetch_history
    from .marketdata.sources import KrakenFxClient

    settings = _settings()
    registry = InstrumentRegistry.from_directory(settings.instrument_spec_dir)
    specs = [registry.get(source), registry.get(hedge)]
    fx = FxService()
    if history is not None:
        bundle = HistoryBundle.load(history)
    else:
        bundle = asyncio.run(fetch_history(specs, hourly_days=days, five_minute_days=0))

    async def rates() -> dict[tuple[str, str], D]:
        client = KrakenFxClient()
        try:
            return await client.rates()
        finally:
            await client.close()

    for (base, quote), rate in asyncio.run(rates()).items():
        fx.set_rate(base, quote, rate, source="kraken")
    impact = None
    if books is not None:
        from .marketdata.frames import load_frames
        from .marketdata.impact import measure_impact

        recorded = load_frames(books)
        impact = {spec.key: measure_impact(spec, recorded) for spec in specs}
    report = Backtester(registry.all(), bundle, fx, impact).run(BacktestConfig(
        source_key=source, hedge_key=hedge, source_quantity=D(quantity),
        objective=HedgeObjective(objective.upper()), target_ratio=D(ratio),
        steps=steps, tolerance_bps=D(tolerance_bps),
    ))
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
        return

    table = Table(title="Backtest on real venue history", show_header=False)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Window", f"{report.start:%Y-%m-%d %H:%M} -> {report.end:%Y-%m-%d %H:%M} UTC"),
        ("Hours", len(report.points)),
        ("Execution cost model", report.execution_model),
        ("Trades beyond recorded depth", report.unmeasured_trades),
        ("Rebalances", report.rebalances),
        ("Funding settlements", report.funding_settlements),
        ("Net P&L", report.final_net_pnl),
        ("  funding", report.total_funding),
        ("  fees", -report.total_fees),
        ("  spread", -report.total_slippage),
        ("Residual mean (bps)", report.mean_residual_bps),
        ("Residual max (bps)", report.max_residual_bps),
        ("Price drawdown (hedged)", abs(report.gross_drawdown)),
        ("Price drawdown (unhedged)", report.unhedged_max_drawdown),
        ("Drawdown removed", f"{report.risk_reduction_pct:.1f}%"),
        ("Peak margin", report.peak_margin),
    ):
        table.add_row(label, str(value))
    console.print(table)
    console.print(
        "[dim]Prices are real hourly closes and funding is what each venue actually "
        "settled. Risk reduction compares price P&L only; carry and costs are separate.[/dim]"
    )


# ======================================================================
# operational
# ======================================================================
@app.command("status")
def status() -> None:
    """Show system status."""
    _settings()

    async def run(service: HedgeLabService) -> Any:
        return await service.status()

    payload = asyncio.run(_with_service(run))
    _banner()
    console.print_json(json.dumps(payload, default=str))


@app.command("demo")
def demo() -> None:
    """Run the full 25-step acceptance demonstration."""
    from .scenarios.acceptance import run_acceptance

    _settings()
    passed = asyncio.run(_with_service(run_acceptance))
    raise typer.Exit(0 if passed else 1)


@app.command("watchdog")
def watchdog(
    timeout: float = typer.Option(30.0, help="Seconds without a heartbeat before acting"),
    interval: float = typer.Option(5.0, help="Seconds between checks"),
    once: bool = typer.Option(False, help="Check once and exit"),
    kill_now: bool = typer.Option(False, "--kill-now",
                                  help="Flatten everything now, independently of the API"),
) -> None:
    """Run the independent kill switch (a separate process from the API)."""
    from .ops.watchdog import Watchdog

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    async def run() -> int:
        dog = Watchdog(settings, timeout_seconds=timeout)
        try:
            if once or kill_now:
                check = await dog.check_once(force=kill_now)
                console.print_json(json.dumps(check.to_dict(), default=str))
                flattened = check.kill_result.get("flattened", True)
                return 0 if flattened else 1
            await dog.run(interval_seconds=interval)
            return 0
        finally:
            await dog.database.dispose()

    raise typer.Exit(asyncio.run(run()))


@app.command("serve")
def serve(
    host: str | None = typer.Option(None), port: int | None = typer.Option(None),
    reload: bool = typer.Option(False, help="Auto-reload on source changes"),
) -> None:
    """Start the API server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "hedgelab.api.app:app",
        host=host or settings.api_host, port=port or settings.api_port,
        reload=reload, log_config=None,
    )


@app.command("init-db")
def init_db(drop: bool = typer.Option(False, help="Drop existing tables first")) -> None:
    """Create the database schema and load the instrument catalogue."""
    settings = _settings()

    async def run() -> None:
        service = HedgeLabService(settings)
        try:
            if drop:
                await service.database.drop_all()
            await service.database.create_all()
            await service.sync_reference_data()
            console.print(
                f"[green]schema ready[/green]: {len(service.specs)} instruments, "
                f"{len(service.registry.mappings())} hedge pairs"
            )
        finally:
            await service.shutdown()

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    app()
