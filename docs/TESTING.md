# Testing

```bash
cd backend
.venv/bin/python -m pytest tests/ -q          # 501 tests, ~135s, offline
.venv/bin/python -m pytest --cov=src/hedgelab --cov-report=term-missing
.venv/bin/hedgelab demo                       # the 25-step acceptance run
.venv/bin/hedgelab scenario run-all           # the ten demo scenarios
.venv/bin/python scripts/load_test.py --base http://localhost:8012   # against a running API
cd ../frontend && npm run test:e2e             # browser tests (system Chrome)
```

The whole suite runs on SQLite with no Docker, no PostgreSQL and **no
network**. That is a consequence of the layering: `domain/`, `hedge/`, `risk/`
and `costs/` are pure functions over their arguments, so there is no I/O to
mock, and the one module that does talk to the venues is fed committed
recordings of them.

## Real data, offline

Every price, book and funding rate the tests see is real, captured from the
venues' public APIs and committed:

| File | What it is |
|---|---|
| `tests/data/live_frames.jsonl.gz` | 500 clean frames of a live session, both venues, polled every 2s — 2026-10-22 12:34:25 → 12:51:06 UTC |
| `tests/data/history.json.gz` | 30 days of hourly and 3 days of 5-minute candles plus settled funding, all eight instruments — 2026-09-22 12:00 → 2026-10-22 11:00 UTC |
| `tests/data/raw/*.json` | Raw API responses, for the client parsing tests |

An autouse fixture (`_no_network` in `conftest.py`) makes any outbound HTTP
request fail the test. A test that silently fell back to live polling would be
non-reproducible and would spend the venues' per-IP rate budget — which is
exactly what happened before the fixture existed (see finding 3 below).

## What is covered

| Area | Tests | Approach |
|---|---|---|
| Quantity conversion | 37 | Table-driven over many size × step × unit combinations, including inverse and non-decimal steps |
| Instruments & registry | 27 | Validation rules, the shipped catalogue, the generic-instrument guarantee |
| Hedge calculator | 38 | All nine objectives, the inverse divergence, cost decomposition, auditability |
| Optimiser | 20 | Every priority, weight sensitivity, brute-force cross-check |
| Risk & margin | 30 | Closed-form liquidation, each threshold boundary, portfolio aggregation |
| State machine | 28 | Legal/illegal matrix, reachability, failure routing |
| Paper venues | 49 | Order lifecycle, TIF semantics, margin, funding clocks, the $10 minimum, faults, stop-out, live-mode refusal |
| Market data & costs | 52 | Replay, stress overlays, FX triangulation, P&L attribution |
| Statistics | 29 | Estimates equal directly computed realised vol, feed-rate invariance, correlation pairing, beta uncertainty |
| Real market data | 30 | Venue client parsing from raw responses, funding units, spec generation, collector, recordings, history, the backtest |
| API | 46 | Real app over an in-process ASGI transport |
| WebSocket & scenarios | 24 | Streaming, subscription, backpressure; all ten scenarios |
| Workflow & recovery | 25 | Full pipeline, every failure mode, restart reconstruction, estimated statistics |
| Acceptance run | 3 | The 25-step demo, twice, and from a deliberately dirty state |

Coverage is 87% overall; the calculation core is 94–100%.

## Tests that assert something specific

Rather than listing all 438, these are the ones that encode the hard parts:

**`test_quantity_matching_is_not_the_same_as_exposure_matching`**
1000 contracts of 0.001 BTC hedged as "1000" of a 1-BTC unit is a 1000×
over-hedge. The test asserts the correct answer is 1 and that the naive one is
1000× larger — the same arithmetic as 1000 BTCUSD contracts against 1 BTC on
Hyperliquid.

**`test_inverse_base_neutral_and_delta_neutral_diverge_when_in_profit`**
A $500k inverse position entered at 100,000 and marked at 125,000 needs 4 BTC
to be base-neutral and 5 to be delta-neutral. Both numbers are asserted. No
shipped pair is inverse; the engine still has to be right about them.

**`test_delta_funding_is_converted_from_percent_to_a_fraction`** and
**`test_hyperliquid_funding_is_already_a_fraction_per_hour`**
Delta publishes `0.01` meaning 0.01 % per 8 h; Hyperliquid publishes
`0.0000125` meaning a fraction per hour. Parsed from captured raw responses,
both must leave the client as a fraction per the instrument's own interval.

**`test_eight_hours_on_hyperliquid_is_eight_hourly_settlements`**
The same span of time must cost eight Hyperliquid intervals, not one — the
regression for finding 1 below.

**`test_hyperliquid_tick_is_five_significant_figures`**
Hyperliquid's tick depends on the price level: $1 on BTC, 0.1 on ETH and PAXG,
0.01 on SOL at current prices.

**`test_orders_under_ten_dollars_are_rejected_on_hyperliquid`**
The minimum is an order *value*, enforced on top of the size lattice.

**`test_adaptive_objectives_ignore_the_source_leg_funding`**
Two source instruments with funding rates 500× apart must produce the *same*
optimal hedge ratio, because source funding is sunk.

**`test_rounding_never_promotes_up_to_the_minimum`**
A required 0.4 against a minimum of 1 rounds to zero and is flagged — never to
1, which would execute more than was asked for.

**`test_closing_orders_are_never_blocked_by_margin`**
An account with almost no free margin can still close. If de-risking could be
blocked by margin, a breached account could never be recovered.

**`test_leg_one_partial_fill_resizes_the_hedge`**
Leg 1 fills 40% → the hedge is −2 BTC, not the −5 originally calculated.

**`test_timeout_does_not_double_hedge`**
After a timeout the final position is exactly one hedge of the right size.

**`test_duplicate_execution_reports_do_not_double_count`** and
**`test_duplicate_fills_are_rejected_by_the_database`**
Both layers of protection, in memory and at the unique constraint.

**`test_restart_with_an_unfinished_cycle_requires_attention`**
A cycle frozen in `LEG_1_FILLED` blocks automatic resumption after a restart.

**`test_step_search_matches_brute_force_on_a_small_grid`**
The optimiser's search is cross-checked against exhaustive evaluation.

**`test_estimate_equals_the_realised_volatility_of_the_feed`**
There is no configured "true" volatility on real data, so the estimate is
checked against the realised volatility computed directly from the same closes,
to within 1e-8, for six instruments.

**`test_a_slower_feed_than_configured_is_not_overstated`**
Real hourly candles fed to an estimator configured for five minutes must give
the hourly realised volatility. Scaling by the configured interval instead of
the observed one would overstate it by `sqrt(3600/300)` = 3.46×.

**`test_carry_penalty_scales_with_hedge_variance_not_residual_variance`**
With correlation near 1 the residual vol is a hundredth of the hedge vol — the
case that exposed the wrong denominator. A realistic carry must trim the ratio
slightly, not annihilate it.

**`test_beta_is_not_called_significant_on_thin_data`** and
**`test_beta_becomes_significant_with_enough_data`**
On the first ~18 hours of real 5-minute data (216 samples) β's distance from 1
is inside two standard errors and must be reported as noise; on three days
(863 samples) Hyperliquid BTC's returns run ~1.5% larger than Delta's and β is
measurably below 1.

**`test_spread_widening_overlay_reaches_its_floor`**
Real BTC spreads are ~0.1 bps; the stress overlay must still produce a spread
wide enough to trip the execution limit — the regression for finding 2.

**`test_the_committed_recording_is_a_clean_live_session`**
500 frames, nothing missing, no gap over 10 seconds, both venues present.

**`test_risk_reduction_tracks_the_hedge_ratio`**
On a month of real hourly closes, drawdown removed scales with the hedge ratio.

**`test_literal_risk_routes_are_not_shadowed`**
FastAPI matches routes in registration order, so a parameterised path declared
before its literal siblings swallows them. `/api/risk/statistics` resolved to
`/api/risk/{mapping_name}` and 404'd.

**`test_graph_has_no_structural_problems`**
Every non-terminal state reaches a terminal one; every state is reachable.

**`test_components_sum_to_the_net`**
P&L attribution adds up, to within 1e-9.

**`test_registering_a_brand_new_instrument_needs_no_code_change`** and
**`test_a_new_instrument_can_be_added_through_the_api`**
A wheat future in EUR is registered with no source change; an AVAX perpetual
the catalogue has never listed is added over HTTP — and, because the recording
predates it, the platform reports that it has no real quote rather than
inventing one.

**`test_only_paper_adapters_are_registered`**, **`test_factory_refuses_live_mode`**,
**`test_factory_refuses_live_even_with_allow_live_set`**,
**`test_live_adapters_cannot_be_constructed`**
The paper-only guarantee, asserted four ways.

## Bugs these tests actually caught

Not hypothetical — each of these was a real defect found during development.

### Before the switch to real data

These fourteen were found while the platform hedged against a simulated MT5
CFD broker on a seeded price simulator. They are kept as history: the fixes
and their regression tests carried over, but some of the specifics (lots, the
broker, simulated time) describe a setup that no longer ships.

1. **`Subscriber` was an unhashable dataclass**, so the WebSocket hub raised on
   every connection. Found by the first WS test.
2. **A log call with `extra={"name": ...}`** collided with a reserved
   `LogRecord` attribute and raised `KeyError` — but only once the level was low
   enough for the call to be evaluated, so it hid in development. Found when
   another test happened to configure logging at INFO. Fixed at the call site
   *and* by making the logger adapter rename colliding keys, because a log
   statement must never be able to take down a trading engine.
3. **The risk gate compared a stale source position** against the projected
   hedge, reporting a 40% residual that did not exist and blocking the trade.
   Found by the acceptance run.
4. **The kill switch could be defeated by a partial fill** — it reported
   success while leaving −2.5 lots open. Found by the acceptance run.
5. **IOC orders were left `PARTIALLY_FILLED` forever**, stranding terminal
   orders in the open set and producing a permanent `ORDER_MISMATCH` after
   every restart. Found by the acceptance run's reconciliation step.
6. **FOK was checked against the fault-capped quantity** rather than the
   requested one, letting a partial fill satisfy an FOK order.
7. **Concentration warned at 45%** when a two-asset portfolio has a 50% floor,
   so every balanced two-asset book was permanently in breach.
8. **Two mappings shared a hedge instrument**, double-counting the same broker
   position in portfolio exposure (reported net exposure of −615,208 on a flat
   book). Fixed by adding a mini contract and a registry invariant.
9. **The kill switch raised on an unreachable venue** — the one situation where
   it most needs to do whatever it can. It now flattens every reachable venue
   and reports the rest as unconfirmed.
10. **The acceptance run was not repeatable**: after a scenario sweep it
    dropped from 25/25 to 18/25 because it inherited positions, armed faults
    and an engaged kill switch. It now resets to a known state first, and a
    test deliberately dirties the platform before running it.
11. **The mean-variance objectives divided by the wrong variance.** The carry
    penalty was scaled by the *residual* variance instead of the hedge leg's,
    making it ~300× too large and returning a hedge ratio of **zero** for a pair
    whose hedge removed almost all of the drawdown. Invisible against the
    shipped assumptions; unmissable the moment correlation was measured rather
    than assumed.
12. **The volatility estimator scaled by the configured sampling interval**
    rather than the observed one, overstating volatility by
    `sqrt(actual/configured)` — a silent 3.4× error whenever the feed ticked
    more slowly than configured.
13. **Simulated time was welded to real time**, so an 8-hour funding interval
    took 8 real hours and the estimator needed hours of data before it could
    say anything. (Moot now: the clock is the venues'.)
14. **A parameterised route shadowed its literal siblings.** `/risk/{mapping_name}`
    was declared before `/risk/statistics`, so the new endpoint returned
    `404 unknown hedge mapping named 'statistics'`. Found by calling it.

### Found by switching to real data

Each of these was found by running against the real venues, not by reading the
code:

15. **Settling "one interval" on each venue compared 8 hours of Delta funding
    with 1 hour of Hyperliquid's.** The API's default did exactly that. Funding
    is now settled for a span of hours (default 8), charged on both venues'
    clocks.
16. **A ×20 spread stress could never trip the 50 bps execution limit** on a
    0.12 bps real BTC spread. The `SPREAD_WIDENING` overlay gained a 100 bps
    floor.
17. **Test fixtures silently fell back to live polling.** Removed settings were
    ignored rather than rejected, so two fixtures ran against the real venues —
    and spent enough of Hyperliquid's per-IP budget to get HTTP 429s that put
    gaps in a recording running at the same time. Tests now fail loudly on any
    network access, and the committed recording is the first 500 clean frames.
18. **Delta's `annualized_funding` is a cap, not the current rate.** It happened
    to equal `0.01 % × 3 × 365` on BTC, which made it look like confirmation of
    the funding unit; on PAXG it is 0.219. The unit is established instead by
    the 0.01 % baseline and by ccxt's own parser.
19. **The simulator flattered the estimator.** It implied a residual volatility
    ~2% of the hedge leg's; the real Delta/Hyperliquid pair is ~10%, and the
    cost-adjusted objective trims the hedge 2.4% on real fees. Assertions that
    encoded the simulator's optimism were rewritten to what the market shows.
20. **Backtest rebalances sized an inverse hedge from the current price.** The
    target used the mark as if it were the entry — exactly the `N/S` versus
    `N/E` trap — so with rebalancing on, an inverse hedge drifted across 699
    sizes in a month. Found by replaying a real inverse perpetual; the target
    now uses the position's entry.
21. **Scenario I asked for a position no real account could open.** It bought
    100 BTC (~$8.2M) on $250k. At Delta's flat base margin that looked like
    $41k; under Delta's size-based margin it needs $1.69M and is refused. The
    scenario now uses 18 BTC, which real margin allows and a 15% drop breaks.

## Beyond the unit tests

### Property-based (`tests/unit/test_properties.py`, Hypothesis)
Nine invariants searched over hundreds of generated inputs each, against the
real generated catalogue and the real recorded books:

* rounding always lands on the venue's lattice, inside min/max, never flips the
  trade's direction, and never trades more than asked;
* base-unit conversion round-trips for every instrument;
* a coin-matched hedge never over-hedges and misses by under one step — for
  every pair, **in both directions** (see the note below);
* realised + unrealised P&L equals the cash flows for any sequence of
  increases, reductions and flips; inverse P&L equals `N(S/E − 1)`;
* a sweep of any real recorded book is bounded by its levels and never beats
  the touch;
* any sequence of shocks, spread/liquidity/volatility overlays and frame
  advances leaves every book two-sided and on its tick lattice, and clearing
  returns exactly the real recorded quote;
* correlation stays in [−1, 1] and volatility is non-negative.

Each was checked by mutation: making rounding round *up*, or making clearing
keep price shocks, fails the relevant property. The first version of the
over-hedge property passed that mutation anyway — Delta contracts always land
exactly on Hyperliquid's lattice, so the Delta → Hyperliquid direction never
rounds. It now also hedges Hyperliquid positions with whole Delta contracts,
where rounding does bite.

### Browser (`frontend/e2e`, Playwright on system Chrome)
`npm run test:e2e` starts the real API on the replay recording and the real
dashboard, then drives Chrome through every page (no console errors, no failed
requests), the calculator (5000 contracts → −5 BTC with its derivation), a hedge
executed from the Hedge Pairs page and visible as a COMPLETED cycle, and a
Hyperliquid outage injected and cleared from the Fault Injection page. 6 tests;
24/24 over four repeats. One run out of nine early on logged a 404 that could
not be reproduced; the page test now names the URL of any failed request.

### Load (`backend/scripts/load_test.py`)
Against a single replay-mode process on SQLite:

```
 50 HTTP clients + 20 WebSocket subscribers, 30s:  716 req/s, 0 errors
     market-data / positions / mappings  p50 16 ms   p99 66–71 ms
     hedge/calculate                     p50 20 ms   p99 79 ms
     system/status, hedge/cycles         p50 ~95 ms  p99 ~380–410 ms
     portfolio risk                      p50 174 ms  p99 473 ms
     every subscriber received every price frame (30/30)

200 HTTP clients + 100 WebSocket subscribers, 20s: 579 req/s, 0 errors
     database-backed endpoints p99 ~3 s; price frames 18 per 20 s
```

Nothing fails under 4× load, but one process saturates: database-backed
endpoints slow by an order of magnitude, and the market loop shares the event
loop, so prices refresh less often — under heavy dashboard load, data ages
toward staleness. Splitting the market loop from the API is the fix if it
matters.

### Inverse contracts on a real inverse perpetual (`test_inverse_real_data.py`)
Neither shipped venue lists one, so Bybit's BTCUSD (1 contract = 1 USD,
BTC-settled, 8-hourly funding) is used: its spec from Bybit's instruments-info,
its 30 days of hourly closes and settled funding from Bybit's public history
(`scripts/capture_bybit_inverse.py`). A $500k inverse long hedged on
Hyperliquid removes >95% of the real drawdown. Opened at 85,939 and hedged at
80,056.7 after a real 6.84% fall, sizing by entry (`N/E`) holds while sizing
by current holding (`N/S`) is off by exactly the move.

### Measured price impact (`marketdata/impact.py`)
The backtest can charge the cost of sweeping the real recorded books instead of
a flat half-spread (`hedgelab backtest run --books <recording>`). Medians over
the 500 recorded books, e.g. Delta BTC 0.03 bps to $250k and 0.34 bps at $5M;
Hyperliquid SOL 4.8 bps at $2.5M; Hyperliquid PAXG only ~$50k deep at 10
levels. Orders larger than any recorded book are counted as unmeasured.

### Venue rules checked against the venues' own data
* **Funding settlement times:** over 30 days Delta's BTC and ETH rates changed
  88 and 89 times, every time at 00:00, 08:00 or 16:00 UTC — the schedule the
  backtest assumes. SOL and PAXG sat at their baseline all month, so the
  4-hourly PAXG schedule is still unconfirmed.
* **Delta's size-based margin:** above `max_leverage_notional` each extra
  dollar adds `*_margin_scaling_factor` percent (the formula ccxt implements
  from Delta's contract pages; Delta's page renders it client-side and could
  not be read directly). BTCUSD: 200× to $100k, 66.7× at $500k, 7.8× at $5M.

## The live-readiness layer (`tests/unit/test_live_readiness.py`)

Everything a live deployment needs between the strategy and a venue, built and
tested with no money at risk. 37 tests; the two load-bearing ones were
mutation-checked (an arrival-order ledger, and a kill switch that is not
persisted, each fail them).

* **Rate limiting and retry** (`marketdata/ratelimit.py`). A process-wide
  per-host weight budget keeps polling under each venue's limit (Hyperliquid:
  900 of its 1200 weight a minute, with its published per-request weights);
  429/5xx and dropped connections are retried with exponential backoff and
  jitter, honouring `Retry-After`; other client errors are not retried.
* **Hard limits** (`venues/guard.py`). Every venue the factory builds is
  wrapped: max order notional, max position notional, max daily loss per venue,
  max orders per minute. Orders that only reduce exposure pass the size and
  loss limits so the book can always be flattened. Refusals are recorded and
  shown in `/api/system/status`.
* **Fill ledger** (`execution/fill_ledger.py`). Duplicates ignored by
  execution id, fills kept in the venue's own sequence, history replayed when a
  late fill lands in the past, overfills flagged. A property test shows any
  delivery order with any duplicates gives the same position as in-order
  application.
* **Dry-run payloads** (`venues/payloads.py`, `GET /api/dry-run-orders`). Every
  paper order is also built in the exact form Delta's `POST /v2/orders` and
  Hyperliquid's `POST /exchange` take — unsigned, never sent — and checked
  against each venue's rules. All 31 orders the demo and the ten scenarios
  produce would be accepted. It found that **neither venue supports
  fill-or-kill**, which the paper engine does; FOK is flagged as unsendable.
* **Record reconciliation** (`reconciliation/venue_records.py`,
  `GET /api/reconciliation/records`). Fills missing on either side, mismatched
  fills, venue positions their own fills do not explain, funding charged versus
  expected, and equity drift. Parsing each venue's *private* statement format is
  the testnet step; the checks run on normalised records today.
* **Independent watchdog** (`ops/watchdog.py`, `hedgelab watchdog`, its own
  Compose service). Reads the heartbeat the API writes every poll; when it
  stops, the watchdog builds its own venue connections, flattens, verifies flat,
  and leaves the kill switch engaged *in the database*, so a restarted API
  refuses to trade.

**Crash drill in Docker:** a hedged book (2000 Delta contracts long, 2 BTC
Hyperliquid short) was left open and the backend container stopped. 35 seconds
later the watchdog had flattened both legs and verified flat; the restarted
backend came up with the kill switch engaged and refused a new hedge (HTTP 409).

The first run of that drill found a real bug: the watchdog treated "no
heartbeat has ever been written" as dead, and flattened positions while the
freshly rebuilt backend was still starting. Never-seen-alive is now
`NOT_STARTED`; only a heartbeat that *stopped* triggers, and the API beats
before its first poll. A regression test pins it.

## The acceptance run

`hedgelab demo` runs the 25-step end-to-end demonstration and exits non-zero if
any step fails. Each step asserts an observable outcome; nothing is printed
that the system did not produce. Currently **25/25**, on the replay and on live
data in Docker; `hedgelab scenario run-all` is **10/10** on both.

It is the highest-value test in the repository — four of the first ten bugs
above were found by it and by nothing else, because it is the only thing that
runs the whole system in sequence with state carried between steps.

## What is not tested

Stated plainly rather than implied:

* **No live adapter tests**, because there is no live adapter.
* **The suite never touches the live venues.** It proves the platform handles
  the recorded responses; a venue that changes its API shape tomorrow is caught
  by the live demo or by `hedgelab instruments sync`, not by `pytest`.
* **The recordings are one 17-minute session and one month.** The statistics
  and backtest numbers are measurements of that window, not of the market in
  general.
* **No volatile market recorded.** The recording is a calm 17 minutes
  (spreads ~0.1 bps); crashes and blow-outs are imposed overlays, never a real
  session replayed.
* **Venue-side latency and queue position.** Paper fills cross the real book
  instantly; nothing models matching latency, queue position or other traders.
* **Delta's margin formula is ccxt's**, not read from Delta directly, and
  Hyperliquid's margin tables (tiered by size) are flattened to the base tier.
* **PAXG's 4-hourly settlement schedule** could not be confirmed from history.
* **No multi-process concurrency tests**, because the system is single-process
  by design.
* **PostgreSQL is verified by migration and by running the demo against it**,
  but the test suite itself runs on SQLite.
