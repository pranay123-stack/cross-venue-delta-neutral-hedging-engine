# PROJECT STATUS

**Complete.** The end-to-end paper workflow works on real market data from
Delta Exchange (India) and Hyperliquid, and everything below was produced by
running the system rather than asserted.

```
 599 tests passing in ~145s           (unit, property, integration, API contract; SQLite, offline)
  10 browser tests passing            Playwright on Chrome, real API + dashboard, replay data
   0 errors under load                219 req/s at 50 clients, 154 at 200 (one process, laptop in powersave)
  25/25 acceptance steps passing      hedgelab demo — on replay and on live data in Docker
  10/10 demo scenarios passing        hedgelab scenario run-all — on replay and live
  88% line coverage overall           93–100% on the calculation core
 ruff: all checks passed
 mypy: strict, 86 source files, no issues
 tsc:  no issues; frontend builds
 22 tables on PostgreSQL 16, 4 Alembic migrations
   1 crash drill passed                watchdog flattened a hedged book 35s after the API died
```

---

## 1. Implemented

### Real market data
`marketdata/sources.py` holds three public, read-only clients — Delta Exchange
(India), Hyperliquid's `info` endpoint, and Kraken's ticker for USDC/USD and
USDT/USD. `collector.py` polls them into synchronised frames;
`feed.py` (`MarketFeed`) applies them live (every 3 seconds) or replays a
recording (`frames.py`). A venue that fails to answer has its instruments
listed as missing, keeps its last real quote, and ages into staleness. An
instrument with no real quote raises `NoMarketData` (HTTP 503) rather than
being priced from a default. There is no price simulator and no synthetic
fallback.

`history.py` fetches real candles and settled funding (`hedgelab market
history`), used to seed the statistics and to drive the backtest.
`hedgelab market record` captures new live sessions.

Funding units are normalised at this edge: Delta publishes a **percent** per
8-hour interval (4 hours on PAXG), Hyperliquid a **fraction** per hour, and both
leave as a fraction per the instrument's own interval.

### The generic instrument model
Forty-field `InstrumentSpec` covering venue, assets, quantity unit, contract
size and multiplier, linear/inverse settlement, tick and quantity lattice,
minimum order value (`min_notional`), precision, leverage, margin model and
rates, maker/taker fees, spread, funding model and interval, swap points and
triple day, trading sessions, direction permissions, price source and FX path.
Loaded from YAML **and** editable at runtime through the API.

The shipped YAML is **generated**, not transcribed: `hedgelab instruments sync
--write` builds `delta_exchange.yaml`, `hyperliquid.yaml` and `mappings.yaml`
from the venues' own metadata — contract value, tick, size decimals, fees,
margin, funding interval — and marks the few fields neither venue publishes
with the reason.

The engine contains no hardcoded symbol. Two tests prove it by registering
instruments the code has never seen — a wheat future quoted in EUR at 5000
units per lot, and an AVAX perpetual added over HTTP — with no source change.
An instrument added through the API must name a real `price_source`
(`delta_india` or `hyperliquid`), or it is refused with a 422.

Eight instruments ship across two venues:

| | Delta Exchange (India) — `PAPER_DELTA` | Hyperliquid — `PAPER_HYPERLIQUID` |
|---|---|---|
| Symbols | `BTCUSD`, `ETHUSD`, `SOLUSD`, `PAXGUSD` | `BTC`, `ETH`, `SOL`, `PAXG` |
| Unit | contracts: 0.001 BTC / 0.01 ETH / 1 SOL / 0.001 PAXG | coins, step `10^-szDecimals`: 0.00001 / 0.0001 / 0.01 / 0.001 |
| Minimum | 1 contract | $10 order value |
| Tick | fixed per product | five significant figures: BTC $1, ETH 0.1, SOL 0.01, PAXG 0.1 |
| Fees | 2 / 5 bps maker/taker (PAXG 1 / 1) | 1.5 / 4.5 bps, base tier |
| Margin | BTC 0.5% initial (200×), 0.25% maintenance | `1/maxLeverage` (BTC 40×), maintenance half |
| Funding | every 8 h (PAXG 4 h), published in percent | hourly, published as a fraction |
| Settlement | USD | USDC |

Four pairs: BTC (`QUOTE_PNL_NEUTRAL`), ETH and SOL (`BASE_ASSET_NEUTRAL`), PAXG
(`NOTIONAL_NEUTRAL`), each Delta perp hedged with the same coin's Hyperliquid
perp. Both venues charge commission; no shipped venue uses MT5-style swap
points, though the `SWAP_POINTS` model remains in the domain for generic CFD
specs.

### Quantity conversion
Every conversion routes through base units. The four pairs produce three
distinct contract-to-coin ratios (0.001, 0.01, 1) — and BTC and PAXG share
`0.001` while one contract is worth ~$82 against ~$4. Inverse contracts are
handled properly: notional is price-independent, base units move with the mark,
and quote delta depends on the **entry** price — a distinction worth 25% of
hedge size on a position in profit. Neither venue lists a live inverse
perpetual, so no shipped pair uses that maths; it is unit-tested.

### Nine hedge objectives
Base-asset-, notional-, quote-P&L- and account-currency-P&L-neutral; custom
ratio; partial; funding-adjusted and cost-adjusted (mean-variance,
`h* = β − k/(λσh²)`, using the *marginal* cost of hedging); risk-weighted
(beta). Every result carries its derivation steps with formula and value.

### Estimated statistics
Volatility, correlation and beta are measured from real prices. The estimator
is seeded at startup from real 5-minute candles and keeps sampling the feed.
On the committed three days of history it measures Delta↔Hyperliquid BTC
correlation at **0.9953**, BTC↔PAXG at 0.274, and β at **0.985 ± 0.0033**
(863 samples) — distinguishable from 1 on three days, not on the first 216
samples, and the engine reports both facts. Its estimates equal the realised
volatility computed directly from the same closes to within 1e-8 (BTC ~1.77%
daily, SOL ~2.8%, PAXG ~1.0%), independently of the feed rate. The objectives
fall back to configured assumptions, with an explicit warning, until there is
enough data.

### Optimiser
Exact, step-search over seven named priorities, and weighted scoring with
range-normalised metrics. Cross-checked against brute force. On Hyperliquid's
fine lattice the stakes are smaller than on a lot-based broker, but the choice
is still real: on 3737 PAXGUSD contracts, rounding gives 3.85 bps of residual
and the neighbouring lattice point 1.18 bps.

### Paper execution
Two paper adapters (`PaperDeltaAdapter`, `PaperHyperliquidAdapter`) over a
shared matching engine that walks the **real** book in the current frame, so
slippage is the depth that was actually there. Models commission, IOC/FOK/GTC
semantics, Hyperliquid's $10 minimum order value, margin checks that never
block de-risking, funding on each venue's own clock, and venue stop-out.

### Two-leg execution
Sixteen states, declared transitions, structural validation, write-ahead
persistence of every transition with its payload. The hedge is recalculated
from what leg 1 **actually** filled. Timeouts are resolved against venue state,
never blindly retried.

### Risk
Per-pair and portfolio, four-level ladder with prescribed actions. Closed-form
margin and liquidation for linear and inverse instruments. Portfolio
aggregation across venue, underlying and settlement currency (USD and USDC
reported separately). Kill switch with confirmation, verification and full
audit.

### Costs
Funding, fees, spread, slippage and FX attributed separately, asserted to sum
to the net. FX restatement uses the measured USDC/USD rate (≈0.99985), not a
hardcoded default. Funding is settled for a **span of hours** (default 8) on
both venues — one Delta interval is eight Hyperliquid ones. Forward projection
with annualised carry.

### Stress overlays
Seven profiles — high volatility, spread widening, price gap, stale data,
liquidity reduction, exchange disconnect, normal — plus price shocks and
funding overrides, all applied *on top of* the real book, reported in
`GET /api/market` and `/system/status`, and removed with one call. Real BTC
spreads are ~0.1 bps, so spread widening has a 100 bps floor.

### Backtest
`hedgelab backtest run` replays **real hourly closes and settled funding**
through the hedge engine. Candles carry no depth, so it charges the spread
measured at sync time.

### Reconciliation and recovery
Three-way comparison (database, Delta, Hyperliquid) with six issue classes and
a suggested action for each. Restart recovery that reconstructs state and
refuses to resume when anything is ambiguous. Paper venues rehydrate from the
database, so restart recovery tests something real.

### Fault injection
Fourteen faults, armed with scope and count for reproducibility, seeded by
`HEDGELAB_RANDOM_SEED` (which touches faults only, never prices). Ten scenarios
that drive the real engine.

### Infrastructure
FastAPI with 60 paths (68 operations) and OpenAPI with a recorded real example per operation; WebSocket with twelve topics
and bounded per-client queues; PostgreSQL with 22 tables and four Alembic migrations; React
+ TypeScript dashboard with eight pages; Docker Compose with four
healthchecked services and the recordings bundled in the image for offline
replay; structured JSON logging with correlation IDs; role-based access with
confirmation on irreversible actions.

---

## 2. Test results

```
tests/unit/test_quantity_conversion.py      37 passed
tests/unit/test_instruments.py              27 passed
tests/unit/test_hedge_calculator.py         38 passed
tests/unit/test_optimizer.py                20 passed
tests/unit/test_risk.py                     30 passed
tests/unit/test_state_machine.py            28 passed
tests/unit/test_paper_venues.py             53 passed
tests/unit/test_marketdata_and_costs.py     52 passed
tests/unit/test_statistics.py               29 passed
tests/unit/test_real_market_data.py         38 passed
tests/unit/test_inverse_real_data.py         5 passed
tests/unit/test_properties.py                9 passed
tests/unit/test_live_readiness.py           37 passed
tests/integration/test_api_contract.py      97 passed
tests/integration/test_api.py               46 passed
tests/integration/test_workflow.py          25 passed
tests/integration/test_websocket_and_scenarios.py  24 passed
tests/integration/test_acceptance.py         3 passed
                                           ---
                                           599 passed in ~145s
```

All offline: the inputs are committed real data —
`tests/data/live_frames.jsonl.gz` (500 clean frames, both venues, polled every
2s, 2026-10-22 12:34:25 → 12:51:06 UTC), `tests/data/history.json.gz` (30 days
hourly + 3 days 5-minute candles and settled funding, all eight instruments,
2026-09-22 12:00 → 2026-10-22 11:00 UTC) and `tests/data/raw/*.json` (captured
API responses). An autouse fixture fails any test that reaches the network.

Coverage on the modules that matter:

| Module | Coverage |
|---|---|
| `costs/funding.py`, `venues/factory.py` | 100% |
| `costs/pnl.py`, `hedge/objectives.py` | 99% |
| `execution/state_machine.py`, `risk/portfolio.py` | 98% |
| `risk/engine.py`, `marketdata/stats.py` | 97% |
| `hedge/calculator.py` | 96% |
| `risk/margin.py`, `marketdata/frames.py` | 95% |
| `hedge/optimizer.py` | 94% |
| `backtest/engine.py` | 92% |
| `venues/paper_engine.py` | 90% |
| `marketdata/feed.py` | 89% |
| `instruments/sync.py` | 85% |
| `marketdata/sources.py` | 83% |
| **Overall** | **88%** |

The lowest-covered modules are the network edge — `marketdata/collector.py`
(70%) and `marketdata/history.py` (60%) — because the paths that only run
against a live venue are exercised by the demo, not by the offline suite.

### Bugs the tests and the acceptance run actually found

Listed because they are the reason to trust the rest. The first fourteen
predate the switch to real data — they were found on the simulated MT5 broker
and price simulator, and are kept as history; the fixes and regression tests
carried over.

1. `Subscriber` was an unhashable dataclass — the WebSocket hub raised on every
   connection.
2. A log call with `extra={"name": ...}` collided with a reserved `LogRecord`
   field and raised at INFO level only, so it hid in development. Fixed at the
   call site *and* by making the logger adapter rename colliding keys.
3. The risk gate compared a **stale source position** against the projected
   hedge, inventing a 40% residual and blocking the trade.
4. The kill switch could be **defeated by a partial fill** while reporting
   success.
5. The kill switch **raised** on an unreachable venue — the case where it
   matters most.
6. IOC orders were left `PARTIALLY_FILLED` forever, producing a permanent
   `ORDER_MISMATCH` after every restart.
7. FOK was checked against the fault-capped quantity, letting a partial fill
   satisfy a fill-or-kill order.
8. Concentration warned at 45% when a two-asset portfolio has a 50% floor.
9. Two mappings shared a hedge instrument, double-counting the same broker
   position (−615,208 of "net exposure" on a flat book).
10. The acceptance run was not repeatable — 25/25 on a clean database, 18/25
    after a scenario sweep.
11. **The mean-variance objectives divided by the wrong variance.** The carry
    penalty was scaled by the *residual* variance instead of the hedge leg's,
    making it ~300× too large and returning a hedge ratio of **zero** for a
    pair whose hedge removed almost all of the drawdown. Invisible against the
    shipped assumptions; unmissable the moment correlation was measured rather
    than assumed.
12. **The volatility estimator scaled by the configured sampling interval**
    rather than the observed one, overstating volatility by
    `sqrt(actual/configured)` — a silent 3.4× error whenever the feed ticked
    more slowly than configured.
13. **Simulated time was welded to real time**, so an 8-hour funding interval
    took 8 real hours. Moot now: the clock is the venues'.
14. **A parameterised route shadowed its literal siblings.** FastAPI matches in
    registration order, and `/risk/{mapping_name}` was declared before
    `/risk/statistics`, so the new endpoint returned
    `404 unknown hedge mapping named 'statistics'`. Found by calling it.

Five more were found by switching to real data:

15. **Settling "one interval" on each venue compared 8 hours of Delta funding
    with 1 hour of Hyperliquid's.** Funding is now settled for a span of hours,
    charged on both venues' clocks.
16. **A ×20 spread stress could never trip the 50 bps execution limit** on a
    0.12 bps real BTC spread. The overlay gained a 100 bps floor.
17. **Test fixtures silently fell back to live polling.** Removed settings were
    ignored rather than rejected, so two fixtures ran against the real venues
    and drew HTTP 429s that put gaps in a concurrent recording. Tests now fail
    loudly on any network access.
18. **Delta's `annualized_funding` is a cap, not the current rate.** It equals
    `0.01 % × 3 × 365` on BTC by coincidence; on PAXG it is 0.219.
19. **The simulator flattered the estimator.** It implied a residual volatility
    ~2% of the hedge leg's; the real pair is ~10%, and the cost-adjusted
    objective trims the hedge 2.4% on real fees.

Details for all nineteen are in [TESTING.md](docs/TESTING.md).

### Backtest

30 days of real hourly closes and settled funding (2026-09-22 12:00 →
2026-10-22 11:00 UTC), long 1000 BTCUSD contracts (1 BTC) on Delta, hedged on
Hyperliquid:

| Hedge ratio | Price drawdown removed | Net P&L | Funding |
|---|---|---|---|
| 1.00 | 98.3% | +$275.87 | +$302.84 |
| 0.50 | 50.6% | −$2,165.74 | +$21.00 |
| ~0.00 | 0.0% | −$4,606.87 | −$260.78 |

Risk reduction tracks the hedge ratio linearly — the correctness check. At
ratio 1.0 the costs were $81.73 in fees and $0.79 in spread, over 809 funding
settlements (90 on Delta, 719 on Hyperliquid).

The full hedge was *paid* to hold over this month, and the reason is specific:
Delta's BTC funding averaged +0.0035% per 8 h, so the long Delta leg **paid
$260.83**, while Hyperliquid's sat at its 0.00125%/h baseline 59% of the
hours, so the short leg **received $563.75**. That is one month of one pair; it
is a measured carry, not a forecast.

Reproduce: `hedgelab backtest run --history backend/tests/data/history.json.gz`
(add `--ratio 0.5` for the partial hedge).

---

## 3. Known limitations

Stated plainly. None of these are hidden behind a passing test.

### By design

* **No live trading.** Five structural barriers, documented in
  [PAPER_TRADING.md](docs/PAPER_TRADING.md).
* **Single process.** One process owns the book. Two would need leader election
  and a per-pair lock, because two coordinators would both see the same
  residual and both trade it.
* **No automatic trading loop.** The background task polls the venues (or
  steps the replay) and streams prices; it does not trade. Rebalancing is
  operator- or scenario-driven.
* **Redis is optional.** It is in the stack and healthy, but the event bus
  currently runs in-process; Redis would only be load-bearing with more than
  one process.

### Simplifications

* **Paper fills.** They walk the real book at the moment of the order, but
  nothing models queue position, other participants reacting, or the venue's
  matching latency. Fine for a hedger crossing the spread; wrong for a market
  maker.
* **Latency is simulated `asyncio.sleep`.** It demonstrates ordering, not
  throughput.
* **Delta's size-based margin uses ccxt's formula.** Above
  `max_leverage_notional` the rate rises linearly (BTC: 200× to $100k, 7.8× at
  $5M); Delta's own contracts page renders client-side and was not read
  directly. Hyperliquid's tiered margin tables are flattened to the base tier.
* **Hyperliquid's tick is fixed at sync time.** It is five significant figures,
  so it depends on the price level; re-sync if a price crosses a power of ten.
* **Hyperliquid fees are the base tier.** Volume tiers and staking discounts
  are not modelled.
* **The backtest's book is the recording.** With `--books` it charges impact
  measured on the 500 recorded books; larger orders than any recorded book are
  counted as unmeasured. Without it, half the spread measured at sync time.
* **One process saturates under heavy load.** 0 errors at 200 clients, but
  database-backed endpoints reach ~3 s p99 and the market loop slows, so prices
  age toward staleness. See [TESTING.md](docs/TESTING.md).
* **Statistics describe the window they were measured on.** The committed
  history is three days of 5-minute candles and one month of hours. The numbers
  quoted are measurements of that window, not of the market in general.
  `risk_aversion` and `horizon_days` remain policy inputs and always will be.
* **Live polling shares an IP budget.** Hyperliquid allows 1200 request-weight
  a minute per IP; the default 3-second poll uses about half. Anything else on
  the same IP polling Hyperliquid can push it into HTTP 429s, which show up as
  missing instruments and then staleness.
* **Worst-case P&L is a normal-quantile estimate**, not a fat-tailed one. It
  will understate a real gap.
* **The FX graph triangulates one hop** through a pivot. Enough for
  USDC→USD and USDT→USD; not a general cross-currency engine.

### Not built

* **No data retention or partitioning.** `market_data` grows fastest; the
  audit tables are the ones to keep and partition rather than prune.
* **No recording of a volatile market.** Crashes are imposed overlays on a
  calm session; recording a real event (CPI, FOMC) is the next data step.
* **PAXG's 4-hourly funding schedule is unconfirmed** — its rate never moved in
  the committed month.
* **`hedge_configs` is persisted but not yet the source of truth** for
  per-pair optimiser settings; those come from the request or the mapping. The
  table and API exist; wiring them as the default is unfinished.
* **`SPOT_EXCHANGE` and `FX` instrument types are modelled but unused**, as are
  lot-based CFD sizing and swap points — the domain supports them, no shipped
  venue uses them.

---

## 4. Demo instructions

```bash
cp .env.example .env
docker compose up --build                                    # live data
HEDGELAB_MARKET_DATA_SOURCE=replay docker compose up --build # offline, bundled recording
```

Dashboard <http://localhost:5174> · API docs <http://localhost:8001/redoc>

```bash
docker compose exec backend hedgelab demo             # 25 steps, a few seconds
docker compose exec backend hedgelab scenario run-all # 10 scenarios
docker compose exec backend hedgelab instruments pairs
docker compose exec backend hedgelab backtest run
```

No Docker: `cd backend && .venv/bin/python -m pytest tests/ -q`, then
`.venv/bin/hedgelab demo` against SQLite — see the README.

[DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) has a ten-minute guided tour. The three
things worth showing:

1. **Configuration → sizing column.** Four contract sizes on Delta, coins on a
   per-asset lattice on Hyperliquid, funding per 8 hours against per hour.
2. **Hedge Calculator, `QUOTE_PNL_NEUTRAL` against `ACCOUNT_CCY_PNL_NEUTRAL`**
   on 5000 BTCUSD contracts: −5 BTC against −5.00075 BTC, and the 1.50 bps
   between them is the measured USDC/USD rate.
3. **Fault Injection.** Arm a leg-2 rejection, hedge, and watch the cycle go
   `RECOVERY_REQUIRED → REBALANCING → COMPLETED`.

---

## 5. Live-adapter extension points

The shape is deliberate even though live is not implemented.

**Where the code goes.** `venues/live_stubs.py` already defines `DeltaAdapter`
and `HyperliquidAdapter` against the `TradingVenue` interface — nine methods.
Replacing the raising bodies is the whole integration; nothing above `venues/`
changes, because nothing above it knows what a venue is.

* **Delta Exchange (India)** — the authenticated REST order endpoints, signed
  with an API key and secret, plus Delta's private WebSocket channels for order
  and position updates. The public client in `marketdata/sources.py` already
  speaks the same base URL and response envelope.
* **Hyperliquid** — signed `exchange` actions (order, cancel, modify) posted to
  `/exchange`, with positions and fills read back from `info`
  (`clearinghouseState`, `userFills`). The signing key should be an **agent
  wallet** approved by the main account: it can trade but cannot withdraw, so
  a leaked key cannot move funds out. Hyperliquid's `cloid` is the natural home
  for the client order id the coordinator already uses to resolve timeouts.

**What must change to reach live**, in order:

1. Implement the adapter methods.
2. Add credential settings and load them from the environment. There are none
   today.
3. Register the class in `LIVE_ADAPTERS` in `venues/factory.py` — currently an
   empty dict.
4. Relax the factory's mode guard behind `allow_live`.
5. Remove the entrypoint's refusal to start outside `PAPER`.

Steps 3–5 are the point of no return, and all three are one-line, obvious edits
a reviewer cannot miss. That is intentional.

**Already built for it, and tested without money** (see
[TESTING.md](docs/TESTING.md#the-live-readiness-layer-testsunittest_live_readinesspy)):
rate limiting with backoff, hard limits below the strategy, an order-independent
fill ledger, dry-run payloads checked against each venue's rules, fill- and
funding-level reconciliation, and an independent watchdog kill switch.

**Still needed, and not to be improvised later:**

* Parsers for each venue's private fills, funding and balance endpoints, so
  reconciliation runs on real statements (testnet).
* Clock-skew detection between venues.
* Real credential management, separate keys per environment, and agent-wallet
  keys rotated independently of the main account.
* Hyperliquid's tiered margin tables and both venues' fee tiers, which the
  paper specs flatten to the base tier.

**What carries over unchanged**: the instrument model, quantity conversion, all
nine objectives, the optimiser, the risk and cost engines, the state machine,
reconciliation, the market-data layer and the entire audit trail. That is the
point of keeping `domain/`, `hedge/`, `risk/` and `costs/` free of I/O — they
are already the parts that would matter most with real money, and they are
already the best-tested parts of the system.
