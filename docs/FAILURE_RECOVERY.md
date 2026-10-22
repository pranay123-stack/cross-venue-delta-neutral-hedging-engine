# Failure and recovery

The interesting part of cross-venue hedging is not the happy path. Fourteen
failure modes are injectable and each has a tested response.

## The fault injector

Faults are **armed**, not triggered: an operator arms one with a scope (venue,
symbol, leg) and a count, and it fires the next time the matching code path
runs. That makes failures reproducible in tests and demonstrable from the
dashboard without racing the engine.

Every fault site calls `FaultInjector.should_fire(...)`, so there is exactly
one way for a fault to influence behaviour and it is greppable.

Disconnects, stale data, wide spreads, price gaps and unexpected positions are
*states* rather than events, so they apply immediately instead of being armed.
The market-side ones (stale data, wide spreads, gaps) are overlays on the real
feed, listed in `GET /api/market` and removed by `DELETE /api/market/stress`.

The injector's probabilistic draws are seeded by `HEDGELAB_RANDOM_SEED`. That
seed touches faults only — never a price.

## The fourteen faults and what happens

| Fault | Response |
|---|---|
| `LEG1_PARTIAL_FILL` | Hedge is re-solved from the **actual** fill, not the pre-trade estimate |
| `LEG2_PARTIAL_FILL` | `LEG_2_PARTIAL` → residual detected → `REBALANCING` → `COMPLETED` |
| `ORDER_REJECTION` | If leg 1 traded: `RECOVERY_REQUIRED` → rebalance → `COMPLETED` |
| `API_TIMEOUT` | Venue queried for the client order id; no blind retry |
| `DELTA_DISCONNECT` | Cycle fails before taking exposure |
| `HYPERLIQUID_DISCONNECT` | Cycle fails; unhedged source exposure is reported honestly |
| `STALE_MARKET_DATA` | Execution refused; risk level goes to `DANGER` |
| `WIDE_SPREAD` | Execution refused past the bps limit. The overlay has a 100 bps floor, because a multiplier on a ~0.1 bps real BTC spread never reaches the 50 bps limit |
| `HIGH_SLIPPAGE` | Fill prints worse; slippage recorded against the reference price |
| `UNEXPECTED_POSITION` | Reconciliation reports `UNKNOWN_POSITION` (critical) |
| `DUPLICATE_EXECUTION_REPORT` | De-duplicated on `exec_id`; position does not move |
| `DATABASE_RESTART` | Short per-call transactions; nothing is held across a cycle |
| `APPLICATION_RESTART` | State rebuilt from the database; see below |
| `PRICE_GAP` | Hedged book absorbs it; residual and P&L both recomputed |

## A failure the platform does not need injecting

A real venue can simply fail to answer a poll — a timeout, a 5xx, an HTTP 429
from Hyperliquid's per-IP budget. The collector does not invent a substitute:
the venue's instruments are listed in the frame's `missing`, the last real
quote is kept, and it ages. Once it is older than
`stale_data_threshold_seconds` (15s) relative to the newest poll it is stale,
execution against it is refused, and the risk level goes to `DANGER` — the same
path as the injected `STALE_MARKET_DATA`. An instrument that has never had a
real quote raises `NoMarketData` and the API returns 503.

## Worked example: leg 2 rejected

Recorded by the platform (scenario D, on the committed recording), not written
by hand:

```
 1. CREATED                -> VALIDATED              validated
 2. VALIDATED              -> AWAITING_MARKET_DATA   awaiting_market_data
 3. AWAITING_MARKET_DATA   -> CALCULATED             hedge_calculated
 4. CALCULATED             -> RISK_APPROVED          risk_approved
 5. RISK_APPROVED          -> LEG_1_SUBMITTED        leg_1_submitted
 6. LEG_1_SUBMITTED        -> LEG_1_FILLED           leg_1_filled
 7. LEG_1_FILLED           -> LEG_2_SUBMITTED        leg_2_submitted
 8. LEG_2_SUBMITTED        -> RECOVERY_REQUIRED      cycle_failed
 9. RECOVERY_REQUIRED      -> REBALANCING            recovery_rebalance
10. REBALANCING            -> COMPLETED              recovered
```

with messages:

```
venue error: PAPER_HYPERLIQUID rejected order: injected rejection: scenario D: Hyperliquid rejects the hedge order
recovery succeeded: hedge restored, residual 1.50 bps
```

The recovery policy, in order of preference: complete the hedge, then unwind
leg 1 to restore a flat book, then escalate to `EMERGENCY`. Leaving a
half-hedge and reporting success is the one outcome that is never acceptable.

## Three-way reconciliation

The database says one thing, Delta says another, Hyperliquid says a third. Any disagreement means the platform's view of its own exposure is
wrong, which is the precondition for every bad outcome in this system.

| Issue | Severity | Suggested action |
|---|---|---|
| `UNKNOWN_POSITION` | CRITICAL | Adopt it and recompute the hedge, or flatten it |
| `MISSING_POSITION` | CRITICAL | Closed outside the platform; clear it and re-evaluate |
| `QUANTITY_MISMATCH` | CRITICAL | Trust the venue, update the database, rebalance |
| `PRICE_MISMATCH` | WARNING | Adopt the venue's entry — inverse delta depends on it |
| `ORDER_MISMATCH` | WARNING | Query the venue for the final state and close it out |
| `STALE_STATE` | CRITICAL | Venue unreachable; do not trade it |

Quantity differences below half a quantity step are treated as clean — venues
round, and a difference smaller than the smallest tradable increment is noise,
not a discrepancy.

## Restart recovery

The rule is simple: **resume only if nothing is ambiguous.**

```
1. read paper positions            (the venue is authoritative)
2. read open orders
3. read database positions and unfinished cycles
4. compare all three
5. reconstruct each unfinished cycle and classify it by has_exposure
6. recompute risk
7. decide whether a rebalance is needed
8. resume only if there are no required actions
```

Any of the following blocks automatic resumption:

* a cycle that stopped in a state with exposure on the book,
* a critical reconciliation discrepancy,
* a venue that cannot be reached, so its exposure cannot be confirmed.

A cycle that stopped *before* taking exposure is noted as safe to abandon
rather than being flagged.

### Paper venues are rehydrated

The paper venues live in memory. Without rehydration every restart would report
the venue as flat and reconciliation would — correctly, but uselessly — flag
every position as `MISSING`. A real venue remembers what you hold when your
client process dies; `rehydrate_paper_venues()` makes the paper one behave the
same way, so restart recovery tests something meaningful.

Restoration is deliberate and audited (`VENUE_REHYDRATED` in `system_events`).
It is not a back door for creating positions: only `_book_fill` can do that.

The acceptance run demonstrates the whole loop — 2 discrepancies before
rehydration, 0 after, `resumable=True`, positions identical to their
pre-restart values.

## Adopting venue state

When the database and the venue disagree, the venue is right — it holds the
money. But adoption overwrites the platform's record, so it requires `ADMIN`,
an explicit `X-Confirm-Action: CONFIRM` header, and writes an `ADOPT_VENUE_STATE`
audit entry with the full before and after position sets.
