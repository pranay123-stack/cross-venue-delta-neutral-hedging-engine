# Cross-Venue Delta-Neutral Hedging Engine

Holds a crypto perpetual position on **Delta Exchange** and the exactly
offsetting position on **Hyperliquid**, so price moves cancel out — and handles
everything that makes "exactly offsetting" hard when two venues measure,
charge and settle positions differently.

It runs on **real market data** from both venues and trades **on paper only**.

```
 long 5 BTC on Delta  (5000 contracts × 0.001 BTC)
short 5 BTC on Hyperliquid  (5.00000 coins)
──────────────────────────────────────────────
 BTC falls 8%:  Delta −$32,700   Hyperliquid +$32,700   →  book moves ~$36
```

Python 3.12 · FastAPI · PostgreSQL · React + TypeScript · Docker.
**501 backend tests** and **6 browser tests**, all offline against committed
recordings of the real venues; every number in this README was produced by
running the system.

---

## Contents

1. [What it is, and why](#1-what-it-is-and-why)
2. [Quick start](#2-quick-start)
3. [How a hedge works](#3-how-a-hedge-works)
4. [Why two venues are hard](#4-why-two-venues-are-hard)
5. [System flow](#5-system-flow)
6. [Architecture](#6-architecture)
7. [Real market data](#7-real-market-data)
8. [The hedge engine](#8-the-hedge-engine)
9. [Execution and recovery](#9-execution-and-recovery)
10. [Risk, margin and costs](#10-risk-margin-and-costs)
11. [The live-readiness safety layer](#11-the-live-readiness-safety-layer)
12. [Results on real data](#12-results-on-real-data)
13. [Testing](#13-testing)
14. [Reference: CLI, API, configuration](#14-reference-cli-api-configuration)
15. [Limitations, and what is left before live](#15-limitations-and-what-is-left-before-live)
16. [Documentation](#16-documentation)

---

## 1. What it is, and why

A **cross-venue, delta-neutral hedging engine**: it keeps a position on one
exchange and the equal-and-opposite position on another, so the combined book
has (almost) no exposure to the price.

Why hold the same coin long on one venue and short on another, instead of just
closing the position?

* **Protect a position that has to stay where it is** — a strategy, a client or
  an inventory lives on Delta, and its price risk has to go away without
  closing it.
* **Earn the funding difference (carry).** Every perpetual charges *funding*
  between longs and shorts, and each venue sets its own rate. Long where you
  pay less, short where you are paid more, and you collect the difference with
  the price risk cancelled. Over 30 real days that was **+$303 per BTC** — see
  [Results](#12-results-on-real-data).
* **Capture the basis** between venues: Delta's BTC sat ~$30 (≈3–4 bps) below
  Hyperliquid's throughout the recording.

What it is **not**: not high-frequency arbitrage (it polls every 3 seconds and
holds positions for hours to weeks), not a market maker (it does not quote),
and not a signal generator (you choose what to hedge; it makes the hedge
correct and safe).

> **Paper trading only.** No live order path exists. The live adapters raise on
> every method, the venue factory builds nothing but paper venues, and no
> setting holds a credential. Every order is a paper fill against the venue's
> *real* order book.
>
> **Real market data only.** Prices, books, funding and contract specs come
> from Delta Exchange (India) and Hyperliquid public APIs — polled live or
> replayed from a recording of a live session. There is no price simulator and
> no synthetic fallback: an instrument with no real quote is refused, not
> priced.

---

## 2. Quick start

```bash
git clone https://github.com/pranay123-stack/cross-venue-delta-neutral-hedging-engine
cd cross-venue-delta-neutral-hedging-engine
cp .env.example .env
docker compose up --build
```

| | |
|---|---|
| Dashboard | <http://localhost:5174> |
| API docs (OpenAPI) | <http://localhost:8001/docs> |

The backend polls both venues every 3 seconds. No API key is needed — the data
is public. A separate **watchdog** container guards the book (see §11).

Offline, or if a venue is unreachable, replay the bundled recording of a real
session instead:

```bash
HEDGELAB_MARKET_DATA_SOURCE=replay docker compose up --build
```

**See it work.** The 25-step acceptance run drives the whole engine on the live
books and exits non-zero if any step fails; then the ten failure scenarios, the
contract conversions, and a month of real history through the hedge maths:

```bash
docker compose exec backend hedgelab demo
docker compose exec backend hedgelab scenario run-all
docker compose exec backend hedgelab instruments pairs
docker compose exec backend hedgelab backtest run
```

**Without Docker** — the test suite needs nothing but Python, and no network:

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q                 # 501 tests, ~2 min, offline

export HEDGELAB_DATABASE_URL="sqlite+aiosqlite:///$PWD/hedgelab.db"
.venv/bin/hedgelab demo                              # the 25 steps, on live data

cd ../frontend && npm ci && npm run test:e2e         # browser tests (uses system Chrome)
```

---

## 3. How a hedge works

You hold **5000 BTCUSD contracts long on Delta**.

1. **Convert through coins.** One Delta contract is 0.001 BTC, so the position
   is 5 BTC. Hyperliquid sizes in coins, so the hedge is **short 5 BTC**.
   Matching raw quantities ("sell 5000") would be a 1000× error.
2. **Choose what "equal" means** — one of nine objectives: the same coins, the
   same dollar value, the same sensitivity to price, or adjusted for funding,
   costs or the measured β between the venues (§8).
3. **Round to what the venue accepts** — Hyperliquid's 0.00001 BTC size step,
   its five-significant-figure price rule and its $10 minimum order; Delta's
   whole contracts and 0.5 tick.
4. **Check it is safe to trade** — margin on both venues, fresh data, a sane
   spread, no kill switch, and the hard limits (§11).
5. **Execute two legs.** Leg 1 on Delta; then size leg 2 from what *actually*
   filled (3000 filled → hedge 3 BTC, not 5); then leg 2 on Hyperliquid. Every
   step is written to the database *before* it happens.
6. **Recover if a leg fails** — rejected, partially filled, timed out or
   disconnected each have a specific, tested response (§9).
7. **Keep it hedged.** Funding is charged on each venue's own clock, leftover
   exposure beyond the pair's tolerance is rebalanced, and risk is watched.
8. **Account for it** — price P&L per leg, fees, spread, funding per venue and
   FX, asserted to add up to the net; and reconciled against the venues'
   records.

---

## 4. Why two venues are hard

| | Delta Exchange (India) | Hyperliquid |
|---|---|---|
| Quantity unit | contracts | coins |
| One unit is | `contract_value` base units (0.001 BTC) | 1 coin, sized to `szDecimals` |
| Funding | every 8 h (4 h for PAXG), published in **percent** | every **hour**, published as a **fraction** |
| Fees | 2 / 5 bps maker / taker (PAXG 1 / 1) | 1.5 / 4.5 bps (base tier) |
| Margin | 0.5 % initial on BTC (200×), **rising with size** | 1 / `maxLeverage` (40× BTC) |
| Settlement | USD | USDC |
| Price tick | fixed per product (0.5 on BTC) | five significant figures — **depends on the price** |
| Minimum order | 1 contract | **$10** of notional |
| Order types | `gtc`, `ioc` | `Alo`, `Ioc`, `Gtc`; no plain market order |

From the catalogue generated from the venues' own metadata:

```
1 BTCUSD  contract (0.001 BTC)  = 0.001 BTC  on Hyperliquid
1 ETHUSD  contract (0.01 ETH)   = 0.01 ETH
1 PAXGUSD contract (0.001 PAXG) = 0.001 PAXG
1 SOLUSD  contract (1 SOL)      = 1 SOL
```

BTC and PAXG share a ratio of 0.001 while one contract is worth ~$82 against
~$4 — equal ratios do not mean equal economics, which is why the engine converts
through base units on every call rather than caching a ratio.

**The funding-unit trap.** Delta's `funding_rate` is a *percent per interval*
(`0.01` = 0.01 % per 8 h); Hyperliquid's is a *fraction per hour*
(`0.0000125`). Compared raw, one venue looks 100× wrong on units and 8× wrong
on interval. Both are normalised at the edge of the system, and funding is
always settled for the same span of hours on both venues — one Delta interval
is eight Hyperliquid ones.

**The inverse-contract trap.** An inverse perpetual is sized in dollars and
settled in coin, so its price sensitivity is `N / entry` while the coins it
represents are `N / mark`. For $500k entered at 100,000 and marked at 125,000,
matching coins says 4 BTC and matching sensitivity says 5 — a 25 % difference.
Neither shipped venue lists a live inverse perp, so this is verified on a real
one, Bybit's BTCUSD (§13).

**Size-based margin.** Delta raises the margin rate as a position grows:
BTCUSD is 200× up to $100k, 66.7× at $500k and 7.8× at $5M. A flat 0.5 % would
make an $8M position look like it needs $41k of margin; it needs $1.69M.

---

## 5. System flow

```
            Delta /v2/tickers, /v2/l2orderbook        Hyperliquid metaAndAssetCtxs, l2Book        Kraken USDC/USD
                              └───────────────────────────┬──────────────────────────────────────┘
                                                          ▼
                              LiveCollector  ──▶  MarketFrame (books, funding, FX; units normalised)
                                                          │         (or: replay a recorded session)
                                                          ▼
                                       MarketFeed  (+ labelled stress overlays)
                                ┌─────────────┬───────────┴──────────┬────────────────┐
                                ▼             ▼                      ▼                ▼
                          statistics      FX service           paper venues      WebSocket → dashboard
                          (vol, ρ, β)                     (fills walk the real book)
                                │                                    ▲
                                ▼                                    │
      convert units ─▶ size the hedge ─▶ risk gate ─▶ hard limits ─▶ execute two legs ─▶ recover
                                                          │                                  │
                                                     dry-run payload                         ▼
                                                  (venue-native order)      persist ─▶ reconcile ─▶ P&L
                                                                                       ▲
                                     watchdog (separate process): heartbeat stops ─▶ flatten + kill switch
```

---

## 6. Architecture

```
   React + TS dashboard ─── FastAPI (REST + WebSocket, OpenAPI)        hedgelab CLI
                                       │                                    │
            ┌──────────────────────────▼────────────────────────────────────▼──┐
            │ HedgeLabService — the composition root; API and CLI are thin     │
            │ shells over it, so they cannot disagree about behaviour.          │
            └──┬────────┬────────┬───────────┬───────────────┬──────────┬──────┘
             hedge/   risk/   costs/     execution/     reconciliation/  faults/
             objectives margin funding   16-state cycle  positions,       14 faults
             optimiser  ladder P&L       fill ledger     fills, funding
               └────────┴────────┴─────┬─────┴───────────────┴──────────┘
            ┌──────────────────────────▼─────────────────────────────────────┐
            │ domain/ — InstrumentSpec · QuantityConverter · Exposure ·       │
            │ Position · Order · Decimal maths.  Pure, no I/O.                │
            └──┬──────────────────────────────────────────┬──────────────────┘
             venues/                                    marketdata/
             paper Delta + Hyperliquid over one          sources → collector → frames
             matching engine; hard-limit guard;          → feed (+ overlays); history,
             dry-run payloads; live stubs (raise)        stats, impact, FX, rate limits
               │
            db/ — SQLAlchemy 2 async · PostgreSQL / SQLite · Alembic (22 tables)        ops/watchdog.py
```

**The dependency rule.** `domain/`, `hedge/`, `risk/` and `costs/` are pure
functions of their arguments — no network, no database — so the same code runs
live, on a replay, in the backtest and in tests. Only
`marketdata/sources.py` touches the network, through public read-only
endpoints with no credentials.

**Decimal everywhere, strings on the wire.** Binary floats break exchange
rounding rules. The one exception is the statistics module, where sampling error
is many orders of magnitude larger than float precision.

```
backend/src/hedgelab/
  domain/            instrument spec, quantity conversion, exposure, orders, positions
  instruments/       registry, generated YAML catalogue, `sync` from venue metadata
  marketdata/        sources (Delta, Hyperliquid, Kraken), collector, frames, feed,
                     history, stats, impact, scenarios (overlays), FX, rate limits
  hedge/             calculator (14-step derivation), nine objectives, optimiser
  execution/         coordinator (two-leg state machine), rebalancer, fill ledger
  risk/              per-pair and portfolio risk, margin and liquidation
  costs/             funding projection, P&L attribution
  venues/            paper Delta + Hyperliquid, matching engine, guard, payloads, live stubs
  reconciliation/    three-way positions; fill / funding records against venue records
  faults/            fault injector (14 kinds)
  scenarios/         the 25-step acceptance run and ten failure scenarios
  backtest/          replay of real hourly history
  ops/               the independent watchdog
  api/ · cli.py · service.py · config.py · db/
frontend/            React dashboard (8 pages) + Playwright tests
backend/tests/       unit, property, integration; data/ holds the real recordings
```

---

## 7. Real market data

* **Venue clients** (`marketdata/sources.py`) — Delta Exchange (India)
  `/v2/products`, `/v2/tickers`, `/v2/l2orderbook`, `/v2/history/candles`;
  Hyperliquid `info` (`metaAndAssetCtxs`, `l2Book`, `candleSnapshot`,
  `fundingHistory`); Kraken's USDC/USD and USDT/USD so FX is measured, not an
  assumed peg. Units are normalised here and nowhere else.
* **Frames** — one synchronised poll of every instrument. A venue that fails to
  answer keeps its last *real* quote, which ages into staleness and blocks
  execution.
* **Recording and replay** — `hedgelab market record` writes frames to gzipped
  JSON lines; tests, scenarios and the offline demo replay them deterministically.
* **History** — `hedgelab market history` fetches real candles and *settled*
  funding; it seeds the volatility/correlation estimator at startup and drives
  the backtest.
* **Specs are generated, not transcribed** — `hedgelab instruments sync` builds
  the catalogue from each venue's metadata (contract value, tick, size
  decimals, fees, margin and its scaling, funding interval, venue ids) and marks
  the few fields neither venue publishes.
* **Stress as labelled overlays** — gaps, spread blow-outs, thin depth, frozen
  feeds and outages are applied *on top of* the real book, reported in
  `/api/market`, and removed with one call. Real BTC spreads are ~0.1 bps, so
  the spread stress has a 100 bps floor; a ×20 multiplier alone never gets near
  a dislocated market.

---

## 8. The hedge engine

**Nine objectives** — base-asset-, notional-, quote-P&L- and
account-currency-P&L-neutral; custom ratio; partial; funding-adjusted and
cost-adjusted (mean-variance, `h* = β − k/(λσ_h²)`); and risk-weighted (β).
Every result carries a 14-step derivation (formula and value at each stage),
rendered in the dashboard and stored in the audit log.

**A quantity optimiser** searches the venue's lattice instead of blindly
rounding: exact, step search over seven named priorities, or a weighted score,
cross-checked against brute force.

**Statistics measured from the market.** The estimator is seeded at startup
from real 5-minute candles and keeps sampling the live feed. On the committed
history it measures Delta↔Hyperliquid BTC correlation at **0.9953** and
**β = 0.985 ± 0.003**: Hyperliquid's BTC moves about 1.5 % more than Delta's —
distinguishable from 1 on three days of data, not on the first eighteen hours,
and the engine reports both.

---

## 9. Execution and recovery

A supervised two-leg cycle: **sixteen states**, transitions declared as data and
validated on every move, each persisted *before* the side effect it describes —
which is what lets restart recovery tell "submitted, outcome unknown" from
"never submitted".

| What goes wrong | What the engine does |
|---|---|
| Leg 1 fills partially | Hedge only what filled |
| Leg 2 is rejected | `RECOVERY_REQUIRED` → rebalance → `COMPLETED` |
| Leg 2 fills partially | Detect the shortfall and trade it away |
| A request times out | Ask the venue for the client order id — never retry blindly (that is how a book gets double-hedged) |
| A venue disconnects | Refuse the cycle before taking exposure; resume after reconnect |
| Spread blows out / data goes stale | Refuse on execution-quality grounds |
| The process restarts | Rebuild from the database, reconcile, resume only if nothing is ambiguous |

Paper fills walk the real recorded or live order book, so slippage comes from
the depth that was actually there.

---

## 10. Risk, margin and costs

* **Risk at two levels** — per pair and portfolio, a four-level ladder (normal →
  warning → danger → emergency/kill) with prescribed actions: pause, reduce,
  flatten. The **kill switch** verifies the book actually reached flat, and is
  persisted so it survives a restart.
* **Margin and liquidation** in closed form for linear and inverse contracts,
  with Delta's size-based margin schedule.
* **Cost attribution that adds up** — the components are asserted to sum to the
  net:

```
source_price_pnl   price P&L on the Delta leg
hedge_price_pnl    price P&L on the Hyperliquid leg (nearly cancels the first)
fees               5 bps Delta + 4.5 bps Hyperliquid, taker
spread / slippage  measured against the mid at submit
funding            each venue's real rate, on its own clock
fx                 restating USDC at the measured USDC/USD rate
───────────────
NET
```

---

## 11. The live-readiness safety layer

Everything a live deployment needs between the strategy and a venue — built and
tested with no money at risk.

| Layer | What it does |
|---|---|
| **Hard limits** (`venues/guard.py`) | Every venue is wrapped: max order notional, max position notional, max daily loss per venue, max orders per minute. Orders that only reduce exposure pass the size and loss limits, so the book can always be flattened. Refusals are recorded and shown in `/api/system/status`. |
| **Rate limiting and backoff** (`marketdata/ratelimit.py`) | A process-wide per-host weight budget (Hyperliquid: 900 of its 1200 weight a minute, with its published per-request weights); 429/5xx and dropped connections retried with exponential backoff, jitter and `Retry-After`. |
| **Fill ledger** (`execution/fill_ledger.py`) | Positions from execution reports that are correct whatever order they arrive in: duplicates ignored, late fills replayed in the venue's sequence, overfills flagged. |
| **Dry-run payloads** (`venues/payloads.py`) | Every paper order is also built in the exact form Delta's `POST /v2/orders` and Hyperliquid's `POST /exchange` take — unsigned, never sent — and checked against each venue's rules. All 31 orders from the demo and scenarios would be accepted. It found that **neither venue supports fill-or-kill**. |
| **Record reconciliation** (`reconciliation/venue_records.py`) | Fills missing on either side, mismatched fills, venue positions their own fills do not explain, funding charged versus expected, equity drift. |
| **Independent watchdog** (`ops/watchdog.py`) | A separate process sharing only the database. When the API's heartbeat stops, it builds its own venue connections, flattens, verifies flat, and leaves the kill switch engaged so a restarted API refuses to trade. |

**Crash drill (Docker).** A hedged book — 2000 Delta contracts long, 2 BTC
Hyperliquid short — was left open and the backend container stopped. **35
seconds later** the watchdog had closed both legs and verified flat; the
restarted backend came up with the kill switch engaged and refused a new hedge
(HTTP 409).

---

## 12. Results on real data

**Backtest — 30 days of real hourly closes and settled funding**
(2026-09-22 → 2026-10-22), long 1 BTC on Delta, hedged on Hyperliquid:

| Hedge ratio | Price drawdown removed | Net P&L | Funding |
|---|---|---|---|
| 1.00 (full) | **98.3 %** | +$275.87 | +$302.84 |
| 0.50 (partial) | **50.6 %** | −$2,165.74 | +$21.00 |
| ~0.00 (none) | **0.0 %** | −$4,606.87 | −$260.78 |

Risk reduction tracks the hedge ratio linearly — that is the correctness check.
The full hedge was *paid* to hold this month, for a specific reason: Delta's BTC
funding averaged +0.0035 % per 8 h, so the long Delta leg **paid $261**, while
Hyperliquid's rate sat at its 0.00125 %/h baseline 59 % of the time, so the
short leg **received $564**. One month of one pair — a measured carry, not a
forecast.

Reproduce: `hedgelab backtest run --history backend/tests/data/history.json.gz`
(add `--books backend/tests/data/live_frames.jsonl.gz` to charge price impact
measured on the recorded books).

**Price impact measured on 500 real recorded books** (median sweep cost):
Delta BTC 0.03 bps up to $250k and 0.34 bps at $5M; Hyperliquid SOL 4.8 bps at
$2.5M; Hyperliquid PAXG only about $50k deep at ten levels.

**Failure scenarios on real books** — a 15 % drop on an 18 BTC position takes
Delta's margin level from 432 % to 68 % and the risk engine to KILL_SWITCH; an
8 % gap that would cost ~$37k unhedged moves the hedged book ~$36.

---

## 13. Testing

```
501 backend tests, offline     unit · property-based · integration · API · WebSocket
  6 browser tests              Playwright on Chrome, real API + dashboard on replay data
 25/25 acceptance steps        on replay, and live in Docker
 10/10 failure scenarios       on replay, and live in Docker
 87 % line coverage            ruff clean · mypy strict clean · tsc clean
  0 errors under load          716 req/s at 50 clients; 579 req/s at 200 (one process)
```

**The data is real and committed** (`backend/tests/data/`): 500 frames of a
live Delta + Hyperliquid session, 30 days of real candles and settled funding
for all eight instruments, 30 days of Bybit's inverse BTCUSD, and captured raw
API responses for the clients' parsing tests. An autouse fixture fails any test
that touches the network.

| Kind | What it covers |
|---|---|
| Unit | quantity conversion, objectives, optimiser, risk, margin scaling, state machine, paper venues, costs |
| Property-based (Hypothesis) | rounding never over-hedges, conversions round-trip, P&L always balances, books stay valid under any stress sequence, ledger is order-independent — each mutation-checked |
| Real-data | specs vs venue metadata, client unit conversions on captured responses, statistics vs direct computation, funding settlement times vs the venue's own history, inverse maths on a real inverse perp |
| Integration | full workflow on SQLite, API surface, WebSocket, all ten scenarios, the acceptance run |
| Browser | every page renders, the calculator, a hedge from the dashboard, an outage injected and cleared |
| Load | `scripts/load_test.py` against a running API |
| Crash drill | the watchdog flattening a book after the backend is killed |

**Bugs the tests and real data actually found** — among them: the
mean-variance objective dividing by the wrong variance (hedge ratio → 0); the
volatility estimator scaling by the configured rather than observed interval (a
silent 3.4× error); a kill switch defeatable by a partial fill; funding settled
as "one interval" per venue (8 hours of Delta against 1 of Hyperliquid); a
backtest rebalance sizing an inverse hedge from the mark instead of the entry
(drifting across 699 sizes in a month); a scenario that asked for a position no
real account could open; and a watchdog that flattened a backend still
starting up. All are pinned by tests — the full list is in
[TESTING.md](docs/TESTING.md).

---

## 14. Reference: CLI, API, configuration

**CLI** (`hedgelab --help`)

| Command | |
|---|---|
| `demo` | the 25-step acceptance run |
| `scenario list \| run <A-J> \| run-all` | the ten failure scenarios |
| `calculate`, `optimize` | size a hedge, with its derivation |
| `backtest run [--history] [--books] [--ratio]` | replay real history |
| `instruments list \| pairs \| sync [--write]` | the catalogue; regenerate it from the venues |
| `market record \| history` | record live frames; fetch real history |
| `watchdog [--once] [--kill-now]` | the independent kill switch |
| `serve`, `init-db`, `status` | operations |

**API** — 57 paths, full OpenAPI at `/docs`. The main groups: `/api/hedge/*`
(calculate, optimize, execute, cycles), `/api/risk/*` and `/api/portfolio`,
`/api/pnl`, `/api/funding/*`, `/api/market*` (feed state and stress overlays),
`/api/fault-injection`, `/api/reconciliation*`, `/api/dry-run-orders`,
`/api/scenarios`, `/api/system/status`, and the `/ws` stream. Details in
[API.md](docs/API.md).

**Configuration** — environment variables with the `HEDGELAB_` prefix:

| Setting | Default | |
|---|---|---|
| `MARKET_DATA_SOURCE` | `live` | `live` polls the venues; `replay` steps a recording |
| `MARKET_POLL_SECONDS` | `3` | live poll interval |
| `MARKET_REPLAY_PATH` / `MARKET_HISTORY_PATH` | — | recording to replay; history to seed statistics |
| `HARD_MAX_ORDER_NOTIONAL` | `3500000` | largest single order |
| `HARD_MAX_POSITION_NOTIONAL` | `5000000` | largest position an order may leave |
| `HARD_MAX_DAILY_LOSS_PER_VENUE` | `100000` | last-resort backstop per venue |
| `HARD_MAX_ORDERS_PER_MINUTE` | `60` | stops a runaway order loop |
| `STALE_DATA_THRESHOLD_SECONDS` | `15` | a quote older than this blocks execution |
| `PAPER_STARTING_BALANCE` | `250000` | per paper venue |

The full list is in [DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## 15. Limitations, and what is left before live

Stated plainly:

* **It has never traded real money, and cannot.** The safety layer a live
  deployment needs is built and tested; the **signed order adapters are not** —
  Delta's authenticated REST and private WebSocket, Hyperliquid's signed
  `exchange` actions through a trade-only agent wallet.
* **Paper fills are instant.** They walk the real book at the moment of the
  order, but nothing models matching latency, queue position or other traders.
* **No volatile market recorded.** The recording is a calm 17 minutes; crashes
  are imposed overlays, never a real event replayed.
* **Delta's margin scaling follows ccxt's formula**, not Delta's page directly;
  Hyperliquid's tiered margin and both venues' fee tiers are flattened to the
  base tier. PAXG's 4-hourly funding schedule could not be confirmed from
  history.
* **Hyperliquid's tick is fixed at sync time** — it depends on the price level;
  re-sync if a price crosses a power of ten.
* **One process.** Under 4× load nothing fails, but database-backed pages slow
  to ~3 s and the price loop refreshes less often.

**The path to live:** testnet adapters for both venues → run the demo and
scenarios on testnet → parse each venue's private fills, funding and balance
into the reconciliation → shadow mode on live data (orders computed, not sent)
→ tiny size with tight hard limits → scale only after reconciliation matches
every day.

---

## 16. Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | design decisions and why each one |
| [HEDGE_ENGINE.md](docs/HEDGE_ENGINE.md) | the objectives, the maths, the estimator |
| [EXECUTION_ENGINE.md](docs/EXECUTION_ENGINE.md) | the sixteen-state machine and its recovery paths |
| [RISK_ENGINE.md](docs/RISK_ENGINE.md) | margin, liquidation, thresholds, kill switch |
| [FAILURE_RECOVERY.md](docs/FAILURE_RECOVERY.md) | the fourteen faults and their responses |
| [PAPER_TRADING.md](docs/PAPER_TRADING.md) | why no real order can be placed |
| [DATABASE.md](docs/DATABASE.md) · [API.md](docs/API.md) | schema and every endpoint |
| [TESTING.md](docs/TESTING.md) | what is covered, what is not, and every bug found |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) · [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | running it, and a guided tour |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | what is done, what is not, the live extension points |
