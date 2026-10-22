"""One ordered walk through the whole API, used twice.

* ``scripts/capture_api_examples.py`` runs it against the real app on the
  recorded Delta/Hyperliquid session and stores every request and response in
  ``openapi_examples.json``; :mod:`.openapi` injects them into the OpenAPI
  document, so ReDoc shows a real example request, a real success response
  and real error responses for every operation.
* ``tests/integration/test_api_contract.py`` runs it again and fails if any
  response's status or shape no longer matches what the documentation shows.

The order matters: later calls depend on state earlier ones create (a position
to hedge, a cycle to look up, an instrument to delete).  Error calls are
included deliberately -- an API reference that only shows the happy path
teaches the reader nothing about what goes wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PAIR = "BTC Delta perp -> Hyperliquid perp"
PAIR_PATH = "BTC%20Delta%20perp%20-%3E%20Hyperliquid%20perp"
CONFIRM = {"X-Confirm-Action": "CONFIRM"}


@dataclass(frozen=True)
class Call:
    #: Short label shown as the example's title in ReDoc.
    title: str
    method: str
    #: The route template, as it appears in the OpenAPI document.
    route: str
    #: The concrete path; ``{name}`` placeholders are filled from ``saved``.
    path: str = ""
    body: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    expect: int = 200
    #: Values to remember from the response for later calls.
    save: dict[str, Callable[[Any], Any]] = field(default_factory=dict)

    @property
    def operation(self) -> str:
        return f"{self.method.upper()} {self.route}"


CALLS: list[Call] = [
    # ------------------------------------------------------------ system
    Call("Service banner", "GET", "/"),
    Call("Liveness", "GET", "/api/health"),
    Call("Readiness", "GET", "/api/ready"),
    Call("System status", "GET", "/api/system/status"),
    # ---------------------------------------------------- reference data
    Call("All instruments", "GET", "/api/instruments"),
    Call("Delta BTC perpetual", "GET", "/api/instruments/{key}",
         "/api/instruments/PAPER_DELTA:BTCUSD"),
    Call("Unknown instrument", "GET", "/api/instruments/{key}",
         "/api/instruments/NOPE:NOPE", expect=404),
    Call("Hedge pairs", "GET", "/api/instrument-mappings"),
    Call("Effective limits and pair settings", "GET", "/api/hedge-configs"),
    Call("FX rates", "GET", "/api/fx-rates"),
    # -------------------------------------------------------- market data
    Call("Feed state", "GET", "/api/market"),
    Call("Top of book, every instrument", "GET", "/api/market-data"),
    Call("Hyperliquid BTC book, 5 levels", "GET", "/api/market-data/{key}/book",
         "/api/market-data/PAPER_HYPERLIQUID:BTC/book", params={"depth": 5}),
    Call("Step the replay one frame", "POST", "/api/market/advance", body={"steps": 1}),
    # ------------------------------------------------------------ hedging
    Call("Size a hedge for 5000 Delta contracts", "POST", "/api/hedge/calculate", body={
        "source_key": "PAPER_DELTA:BTCUSD", "hedge_key": "PAPER_HYPERLIQUID:BTC",
        "source_quantity": "5000", "objective": "QUOTE_PNL_NEUTRAL",
        "use_live_positions": False}),
    Call("Invalid objective", "POST", "/api/hedge/calculate", body={
        "source_key": "PAPER_DELTA:BTCUSD", "hedge_key": "PAPER_HYPERLIQUID:BTC",
        "source_quantity": "5000", "objective": "MAKE_MONEY"}, expect=422),
    Call("Search the lattice", "POST", "/api/hedge/optimize", body={
        "source_key": "PAPER_DELTA:ETHUSD", "hedge_key": "PAPER_HYPERLIQUID:ETH",
        "source_quantity": "3737", "objective": "BASE_ASSET_NEUTRAL",
        "use_live_positions": False, "mode": "WEIGHTED", "search_steps": 3}),
    Call("Open 5000 contracts on Delta", "POST", "/api/hedge/open-position", body={
        "instrument_key": "PAPER_DELTA:BTCUSD", "quantity": "5000"}),
    Call("Hedge the open position", "POST", "/api/hedge/execute", body={
        "mapping_name": PAIR, "source_quantity": "0"},
         save={"cycle_id": lambda r: r["cycle"]["cycle_id"]}),
    Call("Hedge-cycle history", "GET", "/api/hedge/cycles", params={"limit": 5}),
    Call("One cycle with its transitions", "GET", "/api/hedge/cycles/{cycle_id}",
         "/api/hedge/cycles/{cycle_id}"),
    Call("Unknown cycle", "GET", "/api/hedge/cycles/{cycle_id}",
         "/api/hedge/cycles/cyc-does-not-exist", expect=404),
    Call("The state machine", "GET", "/api/hedge/state-machine"),
    Call("Does the pair need rebalancing?", "GET", "/api/hedge/rebalance/{mapping_name}",
         f"/api/hedge/rebalance/{PAIR_PATH}"),
    Call("Rebalance the pair", "POST", "/api/hedge/rebalance/{mapping_name}",
         f"/api/hedge/rebalance/{PAIR_PATH}"),
    # ------------------------------------------------------------ trading
    Call("Orders", "GET", "/api/orders", params={"limit": 5}),
    Call("Fills", "GET", "/api/orders/fills", params={"limit": 5}),
    Call("Positions", "GET", "/api/positions"),
    Call("Accounts", "GET", "/api/accounts"),
    Call("Orders as the venues would receive them", "GET", "/api/dry-run-orders",
         params={"limit": 2}),
    # --------------------------------------------------------------- risk
    Call("Risk, every pair", "GET", "/api/risk"),
    Call("Measured volatility, correlation, beta", "GET", "/api/risk/statistics"),
    Call("Risk, one pair", "GET", "/api/risk/{mapping_name}", f"/api/risk/{PAIR_PATH}"),
    Call("Unknown pair", "GET", "/api/risk/{mapping_name}", "/api/risk/no-such-pair",
         expect=404),
    Call("Portfolio", "GET", "/api/portfolio"),
    Call("Persist a snapshot", "POST", "/api/risk/snapshot"),
    Call("Tighten the warning margin level", "PATCH", "/api/risk/thresholds",
         body={"warning_margin_level": "200"}),
    Call("Portfolio limits", "PATCH", "/api/portfolio/limits",
         body={"max_concentration_pct": "60"}),
    Call("Funding projection, 7 days", "GET", "/api/funding", params={"horizon_days": 7}),
    Call("Settle 8 hours of funding", "POST", "/api/funding/settle", body={"hours": "8"}),
    Call("Applied funding", "GET", "/api/funding/history", params={"limit": 5}),
    Call("P&L attribution", "GET", "/api/pnl"),
    Call("P&L snapshots", "GET", "/api/pnl/history", params={"limit": 5}),
    Call("Margin snapshots", "GET", "/api/margin/history", params={"limit": 5}),
    Call("Apply prescribed actions", "POST", "/api/emergency/apply",
         params={"mapping_name": PAIR}),
    Call("Emergency action history", "GET", "/api/emergency/actions", params={"limit": 5}),
    Call("Risk events", "GET", "/api/risk/events/log", params={"limit": 5}),
    # ---------------------------------------------------- stress overlays
    Call("Widen spreads (hypothetical)", "POST", "/api/market/stress",
         body={"scenario": "SPREAD_WIDENING"}),
    Call("Unknown scenario", "POST", "/api/market/stress", body={"scenario": "PANIC"},
         expect=422),
    Call("Drop BTC 5% (hypothetical)", "POST", "/api/market/stress/shock",
         body={"underlying": "BTC", "pct_move": "-0.05"}),
    Call("Override a funding rate (hypothetical)", "POST", "/api/market/stress/funding-rate",
         body={"instrument_key": "PAPER_DELTA:BTCUSD", "rate": "0.0005"}),
    Call("Back to the real market", "DELETE", "/api/market/stress"),
    # ------------------------------------------------------ fault injection
    Call("Faults", "GET", "/api/fault-injection"),
    Call("Arm a Hyperliquid rejection", "POST", "/api/fault-injection", body={
        "kind": "ORDER_REJECTION", "venue": "PAPER_HYPERLIQUID", "count": 1,
        "reason": "API example"}),
    Call("Disconnect Hyperliquid", "POST", "/api/fault-injection",
         body={"kind": "HYPERLIQUID_DISCONNECT"}),
    Call("Reconnect Hyperliquid", "POST", "/api/fault-injection/reconnect/{venue}",
         "/api/fault-injection/reconnect/PAPER_HYPERLIQUID"),
    Call("Disarm everything", "DELETE", "/api/fault-injection"),
    # ------------------------------------------------------ reconciliation
    Call("Three-way reconciliation", "GET", "/api/reconciliation"),
    Call("Fills and funding against venue records", "GET", "/api/reconciliation/records"),
    Call("Restart recovery verdict", "POST", "/api/reconciliation/recover"),
    Call("Adopt venue state without confirmation", "POST", "/api/reconciliation/adopt",
         expect=428),
    Call("Adopt venue state", "POST", "/api/reconciliation/adopt", headers=CONFIRM),
    # --------------------------------------------- instruments and pairs
    Call("Add a Hyperliquid coin", "POST", "/api/instruments", body={
        "symbol": "AVAX", "venue": "PAPER_HYPERLIQUID", "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL", "base_asset": "AVAX", "quote_asset": "USDC",
        "settlement_asset": "USDC", "underlying_key": "AVAX", "quantity_unit": "BASE_UNIT",
        "tick_size": "0.001", "min_quantity": "0.01", "quantity_step": "0.01",
        "min_notional": "10", "price_precision": 3, "quantity_precision": 2,
        "max_leverage": "10", "funding_model": "PERPETUAL_FUNDING",
        "funding_interval_hours": "1", "price_source": "hyperliquid"}),
    Call("Instrument without a real feed", "POST", "/api/instruments", body={
        "symbol": "WHEAT", "venue": "PAPER_DELTA", "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL", "base_asset": "WHEAT", "quote_asset": "USD",
        "settlement_asset": "USD", "quantity_unit": "CONTRACT", "price_source": "made_up"},
         expect=422),
    Call("No real quote yet for the new coin", "GET", "/api/market-data/{key}/book",
         "/api/market-data/PAPER_HYPERLIQUID:AVAX/book", expect=503),
    Call("Remove the coin", "DELETE", "/api/instruments/{key}",
         "/api/instruments/PAPER_HYPERLIQUID:AVAX"),
    Call("Instrument still used by a pair", "DELETE", "/api/instruments/{key}",
         "/api/instruments/PAPER_HYPERLIQUID:BTC", expect=409),
    Call("Create or replace a pair", "POST", "/api/instrument-mappings", body={
        "name": PAIR, "source_key": "PAPER_DELTA:BTCUSD", "hedge_key": "PAPER_HYPERLIQUID:BTC",
        "objective": "QUOTE_PNL_NEUTRAL", "target_ratio": "1.0", "tolerance_bps": "25",
        "max_notional": "2000000", "enabled": True}),
    # ------------------------------------------------------------- audit
    Call("Audit trail", "GET", "/api/audit", params={"limit": 3}),
    Call("System events", "GET", "/api/audit/system-events", params={"limit": 3}),
    Call("Configuration changes", "GET", "/api/audit/configuration-changes",
         params={"limit": 3}),
    # --------------------------------------------------------- analytics
    Call("Backtest the BTC pair on real history", "POST", "/api/backtest/run", body={
        "mapping_name": PAIR, "source_quantity": "1000", "target_ratio": "1",
        "steps": 0, "book_impact": True}),
    Call("Backtest an unknown pair", "POST", "/api/backtest/run",
         body={"mapping_name": "no-such-pair"}, expect=404),
    # --------------------------------------------------------- scenarios
    Call("Scenarios", "GET", "/api/scenarios"),
    Call("Run scenario D: second-leg rejection", "POST", "/api/scenarios/run",
         body={"scenario": "D"}),
    Call("Unknown scenario", "POST", "/api/scenarios/run", body={"scenario": "Z"},
         expect=404),
    Call("Scenario history", "GET", "/api/scenarios/history", params={"limit": 3}),
    Call("Run all ten scenarios", "POST", "/api/scenarios/run-all"),
    # --------------------------------------------------------- emergency
    Call("Kill switch without confirmation", "POST", "/api/emergency/kill-switch",
         body={"reason": "API example"}, expect=428),
    Call("Engage the kill switch", "POST", "/api/emergency/kill-switch",
         body={"reason": "API example"}, headers=CONFIRM),
    Call("Hedging while the kill switch is on", "POST", "/api/hedge/execute",
         body={"mapping_name": PAIR, "source_quantity": "1000"}, expect=409),
    Call("Clear the kill switch", "DELETE", "/api/emergency/kill-switch"),
]


def resolve(call: Call, saved: dict[str, Any]) -> str:
    return (call.path or call.route).format(**saved)


@dataclass
class Exchange:
    """One call as it actually happened."""

    call: Call
    path: str
    status: int
    response: Any


async def run_calls(client: Any, calls: list[Call] = CALLS) -> list[Exchange]:
    """Run ``calls`` in order on an httpx-style async client."""
    saved: dict[str, Any] = {}
    exchanges: list[Exchange] = []
    for call in calls:
        path = resolve(call, saved)
        response = await client.request(
            call.method, path, json=call.body, params=call.params, headers=call.headers or None,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if response.status_code == call.expect:
            for name, pick in call.save.items():
                saved[name] = pick(payload)
        exchanges.append(Exchange(call, path, response.status_code, payload))
    return exchanges


def trim(value: Any, *, items: int = 2, text: int = 400) -> Any:
    """Shorten a real response for display: long lists keep their first items."""
    if isinstance(value, list):
        kept = [trim(v, items=items, text=text) for v in value[:items]]
        if len(value) > items:
            kept.append(f"... {len(value) - items} more")
        return kept
    if isinstance(value, dict):
        return {k: trim(v, items=items, text=text) for k, v in value.items()}
    if isinstance(value, str) and len(value) > text:
        return value[:text] + "..."
    return value
