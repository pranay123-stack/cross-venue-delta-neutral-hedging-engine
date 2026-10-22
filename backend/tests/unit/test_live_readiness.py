"""The safety layer a live deployment needs, tested with no money at risk.

* client-side rate limiting and retry with backoff;
* hard limits enforced in front of every venue;
* a fill ledger that is correct whatever order executions arrive in;
* dry-run: the exact venue-native order, checked against the venue's rules;
* reconciliation of fills, positions and funding against venue records;
* an independent watchdog that flattens the book when the API stops answering.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hedgelab.domain.enums import OrderType, Side, TimeInForce
from hedgelab.domain.orders import Fill, OrderRequest, Position
from hedgelab.execution.fill_ledger import FillLedger, FillOutcome
from hedgelab.marketdata.ratelimit import RetryPolicy, WeightBudget
from hedgelab.marketdata.sources import (
    DeltaPublicClient,
    HyperliquidPublicClient,
    MarketDataUnavailable,
)
from hedgelab.ops.watchdog import Verdict, Watchdog
from hedgelab.reconciliation.venue_records import (
    FillRecord,
    FundingRecord,
    RecordIssue,
    VenueRecords,
    reconcile_records,
    records_from_paper_engine,
)
from hedgelab.venues.base import OrderRejected
from hedgelab.venues.guard import GuardedVenue, LimitExceeded, VenueLimits
from hedgelab.venues.payloads import cloid, delta_order, hyperliquid_order, hyperliquid_price

D = Decimal
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
BTC_PAIR = "BTC Delta perp -> Hyperliquid perp"


# ======================================================================
# rate limiting and retry
# ======================================================================
class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_weight_budget_waits_for_the_window_instead_of_overspending() -> None:
    time = FakeTime()
    budget = WeightBudget(per_minute=100, clock=time.clock, sleep=time.sleep)
    for _ in range(5):
        assert await budget.acquire(20) == 0          # 100 spent, all at t=0
    time.now = 10.0
    waited = await budget.acquire(20)                  # must wait for t=60
    assert waited == pytest.approx(50.0)
    assert budget.used() == 20


async def test_a_request_heavier_than_the_whole_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="never fit"):
        await WeightBudget(per_minute=10).acquire(20)


def test_hyperliquid_requests_carry_their_published_weights() -> None:
    client = HyperliquidPublicClient(client=httpx.AsyncClient())
    assert client._weight("POST", "/info", {"json": {"type": "l2Book"}}) == 2
    assert client._weight("POST", "/info", {"json": {"type": "metaAndAssetCtxs"}}) == 20


def scripted(responses: list[httpx.Response | Exception]) -> tuple[httpx.AsyncClient, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


TICKER = {"success": True, "result": {"symbol": "BTCUSD", "mark_price": "1"}}


async def test_429_is_retried_with_backoff_and_retry_after_is_honoured() -> None:
    http, calls = scripted([
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429),
        httpx.Response(200, json=TICKER),
    ])
    time = FakeTime()
    client = DeltaPublicClient(client=http, sleep=time.sleep,
                               budget=WeightBudget(1000, time.clock, time.sleep))
    assert (await client.ticker("BTCUSD"))["symbol"] == "BTCUSD"
    assert len(calls) == 3 and client.retries == 2
    assert time.slept[0] == 3.0                       # the venue said when
    assert 0.5 <= time.slept[1] <= 1.5                # exponential backoff with jitter


async def test_a_persistent_outage_gives_up_and_says_so() -> None:
    http, calls = scripted([httpx.Response(503)])
    time = FakeTime()
    client = DeltaPublicClient(client=http, sleep=time.sleep, retry=RetryPolicy(attempts=4),
                               budget=WeightBudget(1000, time.clock, time.sleep))
    with pytest.raises(MarketDataUnavailable, match="gave up after 4 attempts"):
        await client.ticker("BTCUSD")
    assert len(calls) == 4
    assert time.slept == sorted(time.slept)            # backoff grows


async def test_dropped_connections_are_retried() -> None:
    http, calls = scripted([httpx.ConnectError("reset"), httpx.Response(200, json=TICKER)])
    time = FakeTime()
    client = DeltaPublicClient(client=http, sleep=time.sleep,
                               budget=WeightBudget(1000, time.clock, time.sleep))
    await client.ticker("BTCUSD")
    assert len(calls) == 2


async def test_a_client_error_is_not_retried() -> None:
    http, calls = scripted([httpx.Response(404, json={"success": False})])
    client = DeltaPublicClient(client=http)
    with pytest.raises(MarketDataUnavailable):
        await client.ticker("NOPE")
    assert len(calls) == 1


# ======================================================================
# hard limits
# ======================================================================
def order(venue: str, symbol: str, side: Side, qty: str, **kw) -> OrderRequest:
    return OrderRequest(venue=venue, symbol=symbol, side=side, quantity=D(qty), **kw)


def guarded(venues, name: str, **limits) -> GuardedVenue:
    venue = venues[name]
    base = dict(max_order_notional=D(1_000_000), max_position_notional=D(1_500_000),
                max_daily_loss=D(10_000), max_orders_per_minute=100)
    base.update(limits)
    return GuardedVenue(inner=venue.inner, instruments=venue.instruments, fx=venue.fx,
                        limits=VenueLimits(**base))


@pytest.fixture
def venues(service):
    return service.venues


def test_every_venue_the_factory_builds_is_guarded(venues) -> None:
    assert venues and all(isinstance(v, GuardedVenue) for v in venues.values())


async def test_an_order_above_the_size_limit_is_refused(venues) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID", max_order_notional=D(50_000))
    with pytest.raises(LimitExceeded, match="max_order_notional"):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "1"))   # ~$82k
    assert hl.breaches[-1].limit == "max_order_notional"


async def test_a_position_above_the_limit_is_refused_even_in_small_orders(venues) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID", max_position_notional=D(200_000))
    await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "2"))
    with pytest.raises(LimitExceeded, match="max_position_notional"):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "1"))


async def test_reducing_orders_always_pass_so_the_book_can_be_flattened(venues) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID")
    await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "10"))      # ~$820k
    tight = guarded(venues, "PAPER_HYPERLIQUID", max_order_notional=D(1),
                    max_position_notional=D(1))
    closing = await tight.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.SELL, "10"))
    assert closing.filled_quantity == D(10)


async def test_the_daily_loss_limit_blocks_new_risk_but_not_de_risking(venues, service) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID", max_daily_loss=D(5_000))
    await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "5"))
    service.market.apply_shock("BTC", D("-0.05"))                                # ~ -$20k
    with pytest.raises(LimitExceeded, match="max_daily_loss"):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "0.1"))
    await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.SELL, "5"))     # allowed


async def test_a_runaway_order_loop_is_stopped(venues) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID", max_orders_per_minute=3)
    for _ in range(3):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "0.01"))
    with pytest.raises(LimitExceeded, match="max_orders_per_minute"):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "0.01"))


async def test_a_limit_refusal_is_an_order_rejection_the_engine_understands(venues) -> None:
    hl = guarded(venues, "PAPER_HYPERLIQUID", max_order_notional=D(1))
    with pytest.raises(OrderRejected):
        await hl.place_order(order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "1"))
    assert (await hl.health())["hard_limits"]["breaches"] == 1


# ======================================================================
# fill ledger: order-independent positions
# ======================================================================
FILLS = [  # (side, qty, price, seconds) -- a long, a partial close, a flip, a re-open
    (1, "3", "100", 1), (1, "2", "110", 2), (-1, "4", "120", 3),
    (-1, "3", "115", 4), (1, "1", "105", 5), (1, "2", "100", 6),
]


def make_fills() -> list[tuple[int, Fill]]:
    return [
        (side, Fill(order_id=f"o{i}", quantity=D(q), price=D(p), fee=D("0.1"), is_maker=False,
                    timestamp=T0 + timedelta(seconds=t), exec_id=f"x{i}"))
        for i, (side, q, p, t) in enumerate(FILLS)
    ]


def in_order() -> Position:
    position = Position(venue="V", symbol="S")
    for side, fill in make_fills():
        position.apply(fill.quantity * side, fill.price, fill.fee)
    return position


@settings(max_examples=200, deadline=None)
@given(order_=st.permutations(range(len(FILLS))), dupes=st.lists(st.integers(0, len(FILLS) - 1),
                                                                  max_size=6))
def test_any_delivery_order_and_any_duplicates_give_the_venue_position(order_, dupes) -> None:
    fills = make_fills()
    ledger = FillLedger()
    for index in [*order_, *dupes]:
        side, fill = fills[index]
        ledger.add("V", "S", side, fill)
    expected = in_order()
    got = ledger.position("V", "S")
    assert (got.quantity, got.realized_pnl, got.fees_paid) == (
        expected.quantity, expected.realized_pnl, expected.fees_paid)
    assert abs(got.average_entry - expected.average_entry) < D("1e-20")


def test_late_and_duplicate_fills_are_reported() -> None:
    fills = make_fills()
    ledger = FillLedger()
    assert ledger.add("V", "S", *fills[1]).outcome is FillOutcome.APPLIED
    assert ledger.add("V", "S", *fills[0]).outcome is FillOutcome.REORDERED
    assert ledger.add("V", "S", *fills[0]).outcome is FillOutcome.DUPLICATE


def test_an_overfill_is_flagged() -> None:
    ledger = FillLedger()
    ledger.expect("o1", D(2))
    first = Fill(order_id="o1", quantity=D("1.5"), price=D(100), fee=D(0), is_maker=False,
                 exec_id="a")
    second = Fill(order_id="o1", quantity=D("1.5"), price=D(100), fee=D(0), is_maker=False,
                  exec_id="b", timestamp=first.timestamp + timedelta(seconds=1))
    assert ledger.add("V", "S", 1, first).overfilled_by == 0
    assert ledger.add("V", "S", 1, second).overfilled_by == D(1)


# ======================================================================
# dry-run payloads
# ======================================================================
def test_delta_payload_is_its_documented_order_shape(registry) -> None:
    spec = registry.get("PAPER_DELTA:BTCUSD")
    record = delta_order(spec, order("PAPER_DELTA", "BTCUSD", Side.BUY, "5000"),
                         D("81700"), T0, reducing=False)
    assert record.sendable
    assert record.endpoint == "POST /v2/orders"
    payload = record.payload
    assert payload["product_id"] == 27 and payload["size"] == 5000
    assert (payload["side"], payload["order_type"], payload["time_in_force"]) == (
        "buy", "market_order", "ioc")


def test_hyperliquid_payload_is_its_documented_action(registry) -> None:
    spec = registry.get("PAPER_HYPERLIQUID:BTC")
    request = order("PAPER_HYPERLIQUID", "BTC", Side.SELL, "5")
    record = hyperliquid_order(spec, request, D("81731.5"), T0, reducing=False)
    assert record.sendable, record.issues
    action = record.payload
    assert action["type"] == "order" and action["grouping"] == "na"
    leg = action["orders"][0]
    assert leg["a"] == 0 and leg["b"] is False and leg["s"] == "5"
    assert leg["t"] == {"limit": {"tif": "Ioc"}}
    assert re.fullmatch(r"0x[0-9a-f]{32}", leg["c"])
    # A market sell is an IOC limit no more than 5% through the mid, on the
    # five-significant-figure lattice.
    price = D(leg["p"])
    assert D("81731.5") * D("0.95") <= price < D("81731.5")
    assert hyperliquid_price(price, 5, round_up=False) == price


@pytest.mark.parametrize("venue,symbol,qty", [("PAPER_DELTA", "BTCUSD", "10"),
                                             ("PAPER_HYPERLIQUID", "BTC", "1")])
def test_fill_or_kill_is_flagged_as_unsendable_on_both_venues(registry, venue, symbol, qty) -> None:
    spec = registry.get(f"{venue}:{symbol}")
    request = order(venue, symbol, Side.BUY, qty, time_in_force=TimeInForce.FOK)
    build = delta_order if venue == "PAPER_DELTA" else hyperliquid_order
    record = build(spec, request, D("81700"), T0, reducing=False)
    assert not record.sendable
    assert any("fill-or-kill" in issue for issue in record.issues)


def test_venue_rules_are_checked_before_anything_is_sent(registry) -> None:
    spec = registry.get("PAPER_HYPERLIQUID:BTC")
    small = hyperliquid_order(spec, order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "0.0001"),
                              D("81700"), T0, reducing=False)
    assert any("$10 minimum" in i for i in small.issues)
    bad_price = hyperliquid_order(
        spec, order("PAPER_HYPERLIQUID", "BTC", Side.BUY, "1", order_type=OrderType.LIMIT,
                    price=D("81700.5")), D("81700"), T0, reducing=False)
    assert any("significant figures" in i for i in bad_price.issues)
    delta = registry.get("PAPER_DELTA:BTCUSD")
    off_tick = delta_order(delta, order("PAPER_DELTA", "BTCUSD", Side.BUY, "10",
                                        order_type=OrderType.LIMIT, price=D("81700.3")),
                           D("81700"), T0, reducing=False)
    assert any("tick" in i for i in off_tick.issues)


def test_cloids_are_stable_128_bit_hex() -> None:
    assert cloid("cli-abc") == cloid("cli-abc") != cloid("cli-abd")
    assert len(cloid("cli-abc")) == 34


async def test_every_paper_order_records_what_the_venue_would_have_received(service) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSD", D("2000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    records = [r for v in service.venues.values() for r in v.dry_runs]
    assert {r.venue for r in records} == {"PAPER_DELTA", "PAPER_HYPERLIQUID"}
    assert all(r.sendable for r in records)
    assert all(r.paper.get("status") for r in records)
    json.dumps([r.to_dict() for r in records])           # serialisable for the API


# ======================================================================
# reconciliation against venue records
# ======================================================================
async def test_records_reconcile_cleanly_after_a_hedge(service) -> None:
    await service.open_source_position("PAPER_DELTA:BTCUSD", D("2000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    await service.persist_funding(service.settle_funding())
    reports = await service.reconcile_records()
    assert reports and all(r.is_clean for r in reports), [r.to_dict() for r in reports]
    assert sum(r.fills_checked for r in reports) >= 2


async def hedged_records(service):
    await service.open_source_position("PAPER_DELTA:BTCUSD", D("2000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    engine = service.venues["PAPER_HYPERLIQUID"].engine
    venue = records_from_paper_engine(engine)
    internal_positions = list(engine.positions.values())
    return venue, internal_positions


async def test_a_venue_fill_we_never_recorded_is_critical(service) -> None:
    venue, positions = await hedged_records(service)
    report = reconcile_records(venue, internal_positions=positions, internal_fills=[],
                               expected_funding=[])
    assert report.has_critical
    assert {d.issue for d in report.discrepancies} == {RecordIssue.FILL_MISSING_INTERNALLY}


async def test_a_recorded_fill_the_venue_never_saw_is_critical(service) -> None:
    venue, positions = await hedged_records(service)
    ghost = FillRecord("ghost", "o", "BTC", 1, D(1), D(80000), D(0), T0)
    report = reconcile_records(venue, internal_positions=positions,
                               internal_fills=[*venue.fills, ghost], expected_funding=[])
    assert [d.issue for d in report.discrepancies] == [RecordIssue.FILL_UNKNOWN_TO_VENUE]


async def test_a_fill_with_a_different_price_is_a_mismatch(service) -> None:
    venue, positions = await hedged_records(service)
    altered = [FillRecord(f.exec_id, f.order_id, f.symbol, f.side_sign, f.quantity,
                          f.price + 1, f.fee, f.timestamp) for f in venue.fills]
    report = reconcile_records(venue, internal_positions=positions, internal_fills=altered,
                               expected_funding=[])
    assert {d.issue for d in report.discrepancies} == {RecordIssue.FILL_MISMATCH}


async def test_a_position_its_own_fills_do_not_explain_is_caught(service) -> None:
    venue, positions = await hedged_records(service)
    venue.positions["BTC"] += D(1)                       # e.g. a liquidation we missed
    report = reconcile_records(venue, internal_positions=positions,
                               internal_fills=venue.fills, expected_funding=[])
    issues = {d.issue for d in report.discrepancies}
    assert RecordIssue.FILLS_DONT_EXPLAIN_POSITION in issues
    assert RecordIssue.POSITION_MISMATCH in issues


async def test_funding_charged_is_compared_with_funding_expected(service) -> None:
    venue, positions = await hedged_records(service)
    venue.funding = [FundingRecord("BTC", D("-12.00"), T0)]
    close = reconcile_records(venue, internal_positions=positions, internal_fills=venue.fills,
                              expected_funding=[FundingRecord("BTC", D("-12.02"), T0)])
    assert close.is_clean                                 # within rounding tolerance
    off = reconcile_records(venue, internal_positions=positions, internal_fills=venue.fills,
                            expected_funding=[FundingRecord("BTC", D("-9.00"), T0)])
    assert [d.issue for d in off.discrepancies] == [RecordIssue.FUNDING_MISMATCH]


async def test_out_of_order_venue_fills_still_explain_the_position(service) -> None:
    venue, positions = await hedged_records(service)
    shuffled = VenueRecords(venue=venue.venue, positions=venue.positions,
                            fills=list(reversed(venue.fills)) + venue.fills[:1])
    report = reconcile_records(shuffled, internal_positions=positions,
                               internal_fills=venue.fills, expected_funding=[])
    assert report.is_clean


# ======================================================================
# the independent watchdog
# ======================================================================
async def test_watchdog_leaves_a_live_process_alone(service, settings, database) -> None:
    await service.heartbeat()
    check = await Watchdog(settings, database=database).check_once()
    assert check.verdict is Verdict.HEALTHY


async def test_watchdog_does_nothing_before_anything_has_run(settings, database) -> None:
    check = await Watchdog(settings, database=database).check_once()
    assert check.verdict is Verdict.NOT_STARTED


async def test_watchdog_never_flattens_a_process_it_has_never_seen_alive(
    service, settings, database,
) -> None:
    """Regression: in Docker the watchdog flattened the book while the backend
    was still starting -- positions existed, the first heartbeat did not yet.
    Never-seen-alive is not dead; only a heartbeat that stopped triggers."""
    await service.open_source_position("PAPER_DELTA:BTCUSD", D("2000"))
    await service._persist_positions()
    check = await Watchdog(settings, database=database).check_once()
    assert check.verdict is Verdict.NOT_STARTED
    assert check.open_positions == 1


async def test_watchdog_flattens_the_book_when_the_api_goes_quiet(service, settings, database) -> None:
    from hedgelab.db.repositories import PositionRepository
    from hedgelab.service import HedgeLabService

    await service.open_source_position("PAPER_DELTA:BTCUSD", D("2000"))
    await service.execute_hedge(BTC_PAIR, D("0"))
    await service.heartbeat()
    await service._persist_positions()
    # The API "dies": no more heartbeats.  Two minutes later the watchdog looks.
    later = datetime.now(UTC) + timedelta(minutes=2)
    dog = Watchdog(settings, database=database, timeout_seconds=30, clock=lambda: later)
    check = await dog.check_once()
    assert check.verdict is Verdict.TRIGGERED
    assert check.kill_result["flattened"] is True, check.kill_result

    async with database.session() as session:
        positions = await PositionRepository(session).to_domain()
    assert all(p.is_flat for p in positions)

    # The API comes back: the persisted kill switch keeps it from trading.
    revived = HedgeLabService(settings, database=database)
    await revived.startup(seed_reference_data=False, rehydrate=True, seed_stats=False)
    assert revived.state.kill_switch_engaged is True
    with pytest.raises(PermissionError, match="kill switch"):
        await revived.execute_hedge(BTC_PAIR, D("1000"))

    # And the watchdog does not keep firing at a book that is already safe.
    again = await dog.check_once()
    assert again.verdict is Verdict.KILLED_AND_FLAT


async def test_an_operator_can_kill_independently_of_the_api(service, settings, database) -> None:
    await service.heartbeat()
    check = await Watchdog(settings, database=database).check_once(force=True)
    assert check.verdict is Verdict.TRIGGERED
    assert "operator" in check.detail


def test_watchdog_settings_are_independent_of_trading_settings(settings) -> None:
    # The watchdog needs no trading credentials and no API process: only the
    # database the API writes its heartbeat to.
    assert Watchdog(settings, timeout_seconds=12).timeout == timedelta(seconds=12)
