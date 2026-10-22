# Engineering case study: hedging one position across two crypto exchanges

A short account of the problem this engine solves, how it is built, one failure
traced end to end, how it is verified, and what it deliberately does not do.
Every claim links to the code or test that backs it.

---

## The problem

Hold BTC exposure on **Delta Exchange (India)** and the offsetting position on
**Hyperliquid**. Economically it is one coin; operationally the two venues
disagree on almost everything that sizes, costs and settles the trade:

| Concern | Delta Exchange | Hyperliquid | What goes wrong if ignored |
|---|---|---|---|
| Quantity | contracts of 0.001 BTC | coins, step 0.00001 | "sell 5000" is a 1000× over-hedge |
| Funding | percent per 8 h (4 h for PAXG) | fraction per hour | raw comparison is off by 100× in units and 8× in time |
| Settlement | USD | USDC | a 1.5 bps residual that never closes (USDC/USD 0.99985) |
| Price tick | fixed (0.5) | five significant figures | orders rejected once the price crosses a power of ten |
| Margin | 0.5 % to $100k, rising with size | 1 / max leverage | an $8M position looks like it needs $41k; it needs $1.69M |
| Minimum order | 1 contract | $10 notional | dust rebalances rejected |
| Order types | `gtc`, `ioc` | `Alo`, `Ioc`, `Gtc` | fill-or-kill is unsendable on both |

And the execution problem on top: two venues cannot be traded atomically. Leg 1
can fill and leg 2 fail, a request can time out with an *unknown* outcome, and
the process can die between the two.

## The design

```
  venue metadata ──▶ InstrumentSpec (YAML, generated)        real books ──▶ MarketFeed
                          │                                                   │
                          ▼                                                   ▼
                 QuantityConverter ──▶ objectives ──▶ optimiser ──▶ risk gate ──▶ hard limits
                 (pure, Decimal)        (pure)         (pure)        (pure)        (venue guard)
                                                                                     │
                     database (write-ahead)  ◀── 16-state coordinator ──▶ paper venues (real books)
                                                        │                            │
                                         reconciliation ◀─────────── records ◀───────┘
                                                        ▲
                         watchdog (separate process): heartbeat stops ─▶ flatten + kill switch
```

* **A normalised domain model.** Every instrument is an
  [`InstrumentSpec`](../backend/src/hedgelab/domain/instrument.py) generated
  from the venue's own metadata by
  [`instruments/sync.py`](../backend/src/hedgelab/instruments/sync.py). The
  engine never branches on a symbol; a new coin is configuration.
* **Pure calculation modules.**
  [`domain/quantity.py`](../backend/src/hedgelab/domain/quantity.py) converts
  through base units; [`hedge/`](../backend/src/hedgelab/hedge/) sizes the hedge
  under nine objectives with a 14-step derivation;
  [`risk/`](../backend/src/hedgelab/risk/) and
  [`costs/`](../backend/src/hedgelab/costs/) price margin, liquidation, funding
  and P&L. None of them does I/O.
* **Adapters at the edge.**
  [`marketdata/sources.py`](../backend/src/hedgelab/marketdata/sources.py) is the
  only module that touches the network, and the only place units are
  normalised. Paper venues ([`venues/`](../backend/src/hedgelab/venues/)) fill
  against the real book; every venue is wrapped in a
  [hard-limit guard](../backend/src/hedgelab/venues/guard.py) that also builds
  the exact venue-native payload ([dry-run](../backend/src/hedgelab/venues/payloads.py)).
* **A persistent state machine.**
  [`execution/state_machine.py`](../backend/src/hedgelab/execution/state_machine.py)
  declares sixteen states and their legal transitions as data; the
  [coordinator](../backend/src/hedgelab/execution/coordinator.py) persists each
  transition *before* the side effect it describes.
* **Reconciliation and a watchdog.**
  [Positions](../backend/src/hedgelab/reconciliation/engine.py) and
  [fills, funding and equity](../backend/src/hedgelab/reconciliation/venue_records.py)
  are reconciled against venue records; an independent
  [watchdog](../backend/src/hedgelab/ops/watchdog.py) flattens the book if the
  API stops heartbeating.
* **One composition root.**
  [`service.py`](../backend/src/hedgelab/service.py) wires everything; the
  FastAPI app and the CLI are thin shells over it, and the React dashboard
  reads the API.

## A representative failure: the second leg is rejected

Scenario D (`hedgelab scenario run D`, replay data) arms one rejection on
Hyperliquid and hedges 5000 BTCUSD contracts:

```
VALIDATED → AWAITING_MARKET_DATA → CALCULATED → RISK_APPROVED →
LEG_1_SUBMITTED → LEG_1_FILLED → LEG_2_SUBMITTED → RECOVERY_REQUIRED →
REBALANCING → COMPLETED
final residual 0.00075 BTC (1.50 bps)
```

What happens, and where:

1. **Leg 1 fills** — 5 BTC long on Delta. The `LEG_1_FILLED` event, with the
   fill, is already in the database.
2. **Leg 2 is rejected.** The coordinator does not raise and walk away with a
   naked position; it routes to `RECOVERY_REQUIRED`
   ([`_recover`](../backend/src/hedgelab/execution/coordinator.py)), because
   exposure is on the book without a complete hedge.
3. **Residual is measured, not assumed** — the source position as the venue
   reports it, against the hedge it holds, converted through base units.
4. **The rebalancer trades the difference** on Hyperliquid and the cycle
   completes. The remaining 0.00075 BTC is not an error: it is exactly
   5 BTC × (1 − 0.99985), the measured USDC/USD rate, and sits far inside the
   pair's 25 bps tolerance.

The same machinery handles the neighbours of this failure, each with a test in
[`tests/integration/test_workflow.py`](../backend/tests/integration/test_workflow.py):
leg 1 partially filled (`test_leg_one_partial_fill_resizes_the_hedge` — the hedge
is sized from what *filled*), leg 2 partially filled
(`test_leg_two_partial_fill_triggers_a_rebalance`), a timeout with an unknown
outcome (`test_timeout_does_not_double_hedge` — the coordinator asks the venue
for its client order id instead of retrying), and a restart mid-cycle
(`test_restart_reconstructs_state`).

## Verification

| What | How | Where |
|---|---|---|
| The second-leg recovery above | regression test | `test_leg_two_rejection_recovers_automatically` |
| Every transition persisted | integration test | `test_every_transition_is_persisted` |
| Rounding never over-hedges, P&L always balances, ledger order-independent | Hypothesis properties, each mutation-checked | [`test_properties.py`](../backend/tests/unit/test_properties.py), [`test_live_readiness.py`](../backend/tests/unit/test_live_readiness.py) |
| Funding units and intervals | captured real API responses | [`test_real_market_data.py`](../backend/tests/unit/test_real_market_data.py) |
| Inverse-contract maths | a real inverse perpetual (Bybit BTCUSD) | [`test_inverse_real_data.py`](../backend/tests/unit/test_inverse_real_data.py) |
| The whole system in sequence | 25-step acceptance run | `hedgelab demo` |
| Ten failure modes | scenario runner | `hedgelab scenario run-all` |
| The dashboard | Playwright on Chrome | [`frontend/e2e`](../frontend/e2e/dashboard.spec.ts) |
| The watchdog | Docker crash drill | [TESTING.md](TESTING.md#the-live-readiness-layer-testsunittest_live_readinesspy) |

All of it runs offline against committed recordings of the real venues; a test
that touches the network fails.

## Trade-offs

* **Decimal arithmetic.** Exchange lattices are decimal; `0.1 + 0.2 != 0.3` in
  binary floating point is a rejected order here. The statistics module is the
  one deliberate float boundary — sampling error dwarfs float error there.
* **Write-ahead persistence.** Recording "leg 2 submitted" *before* submitting
  is what lets restart recovery tell "outcome unknown" from "never sent". It
  costs a database write per transition; a hedger that holds positions for
  hours can afford it.
* **Recorded market data instead of a simulator.** The engine originally ran on
  a seeded price process. Replacing it with recordings of the real venues
  exposed bugs the simulator hid: a funding settlement that compared 8 hours on
  one venue with 1 hour on the other, a spread stress that could never trip the
  execution limit on a real 0.12 bps spread, an estimator assumption 5× too
  optimistic about residual volatility. Recordings are reproducible *and* true.
* **Offline tests.** Deterministic, fast, and they cannot spend a venue's rate
  budget — which an early version did, getting HTTP 429s that put holes in a
  concurrent recording.
* **A single-process coordinator.** Two coordinators would both see the same
  residual and both trade it. Multi-process operation needs leader election and
  per-pair locks; until there is a reason, one process owns the book, and the
  watchdog is the one thing deliberately *outside* it.

## Limitations

* **No signed live order adapters.** Delta's authenticated REST and private
  WebSocket and Hyperliquid's signed `exchange` actions are extension points
  that raise. The dry-run payloads show what they would send.
* **The paper-fill model** crosses the real book instantly: no matching latency,
  no queue position, no other participant reacting to the order.
* **Available history** is one 17-minute recording of a calm market and 30 days
  of hourly candles and settled funding. Statistics and backtest results
  describe that window only; crashes are imposed overlays, not a replayed event.
* **No multi-process coordination**, by design (above).
* **Venue rules are flattened in places** — Hyperliquid's tiered margin and both
  venues' fee tiers use the base tier.
