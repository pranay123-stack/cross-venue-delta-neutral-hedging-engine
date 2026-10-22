# Demo script

Two options: the automated 25-step run, or a guided tour of the dashboard.

Everything below runs on real market data — live from both venues by default,
or from the bundled recording with `HEDGELAB_MARKET_DATA_SOURCE=replay`. The
numbers quoted here are from the replay, so they are reproducible; on live data
the prices, funding and basis are whatever the venues are printing, and the
pass/fail outcomes are the same.

## Option 1 — the automated acceptance run

```bash
docker compose exec backend hedgelab demo
# or locally:  cd backend && .venv/bin/hedgelab demo
```

Twenty-five steps, each asserting an observable outcome, exiting non-zero if
any fails. Takes a few seconds. Currently **25/25**, on replay and on live data.

Worth pausing on:

* **Step 3** confirms all eight instruments are quoting real two-sided books
  and prints Delta's and Hyperliquid's BTC mids side by side — they are not the
  same number.
* **Step 6** prints the full 14-step derivation of the hedge quantity: 5000
  contracts of 0.001 BTC → −5 BTC on Hyperliquid's 0.00001 lattice.
* **Step 7** shows the unhedged pair sitting at `EMERGENCY` with ~10,000 bps of
  residual — and step 8 hedges it anyway, because risk is evaluated against the
  post-trade book.
* **Step 11** reports a 1.50 bps residual on a "perfect" hedge, and says why:
  the legs settle in USD and USDC.
* **Step 13** shows a 4% shock overlay producing almost no P&L change on a
  hedged book (−404.19 → −456.73 on the replay, against a move worth ~16,333
  unhedged).
* **Step 15** states plainly that the hedge *loses money* once Delta's funding
  is overridden to 15 bps per 8 hours.
* **Steps 20–22** wipe the in-memory venues, detect the gap, rehydrate and
  confirm `resumable=True`.
* **Step 24** flattens everything and then verifies it actually reached flat.

## Option 2 — the guided dashboard tour (about 10 minutes)

`docker compose up --build`, then <http://localhost:5174>.

### 1. Overview (30s)

Two venues, both connected, both paper, quoting real books. Note the permanent
**PAPER TRADING ONLY** banner and `0 live adapters registered` in the header —
that is not a label, it is the actual registry count.

### 2. Configuration → the instrument catalogue (2 min)

Eight instruments across two venues, generated from the venues' own metadata.
Look at the **Sizing** and **Step** columns:

```
PAPER_DELTA:BTCUSD       1 contract  = 0.001 BTC    step 1
PAPER_DELTA:ETHUSD       1 contract  = 0.01 ETH     step 1
PAPER_DELTA:PAXGUSD      1 contract  = 0.001 PAXG   step 1
PAPER_DELTA:SOLUSD       1 contract  = 1 SOL        step 1
PAPER_HYPERLIQUID:BTC    1 base_unit = 1 BTC        step 0.00001
PAPER_HYPERLIQUID:SOL    1 base_unit = 1 SOL        step 0.01
```

Four different contract sizes on one venue, coins on a per-asset lattice on the
other, and funding quoted per 8 hours (4 on PAXG) against per hour. This is the
problem the platform exists to solve.

Scroll to **Add an instrument** and paste the placeholder AVAX perpetual. It
appears in the catalogue immediately, and on live data starts quoting on the
next poll — no deploy, no code change. On replay it has no quote, because the
recording predates it, and the platform says so (503) rather than inventing a
price. The change log at the bottom records it.

### 3. Hedge Calculator (3 min)

Source `PAPER_DELTA:BTCUSD`, hedge `PAPER_HYPERLIQUID:BTC`, quantity `5000`.

Run it once with **`QUOTE_PNL_NEUTRAL`**: 5000 contracts → −5 BTC, and the
derivation table shows every step including the USDC/USD conversion.

Now switch to **`ACCOUNT_CCY_PNL_NEUTRAL`**: −5.00075 BTC. The difference is the
measured USDC/USD rate (0.99985). A hedge that is perfectly delta-neutral in
quote terms is not neutral in account terms, and the platform makes you choose
which one you meant.

Then **`RISK_WEIGHTED`**: −4.8913 BTC. The "Statistical inputs" step says the
volatilities and correlation were *measured* from 500 real paired samples, and
β came out at 0.978 — Hyperliquid's BTC moves slightly more than Delta's. (In
replay mode this needs `HEDGELAB_MARKET_HISTORY_PATH` set; without it the step
says the objective fell back to configured assumptions. See
[DEPLOYMENT.md](DEPLOYMENT.md).)

Finally tick **Search the quantity lattice**, set source `PAPER_DELTA:PAXGUSD`,
hedge `PAPER_HYPERLIQUID:PAXG`, quantity `3737`, objective `NOTIONAL_NEUTRAL`,
mode `STEP_SEARCH`, priority `MIN_RESIDUAL`. The candidate table shows rounding
gives −3.739 at 3.85 bps of residual while the neighbouring lattice point
−3.738 gives 1.18 bps.

### 4. Execute a hedge (1 min)

Hedge Pairs → **BTC Delta perp -> Hyperliquid perp** → *Hedge existing
position*. Then Hedge Cycles: the cycle walked nine states, and each one has its
payload, including the full calculation stored on the `CALCULATED` transition.
The fills walked the real book in the current frame.

### 5. Break it (3 min)

Fault Injection is the page worth spending time on.

* **Simulate Hyperliquid disconnect** → try to hedge → the cycle fails *before*
  taking exposure, and says so.
* **Reconnect**, then **Arm leg-2 partial fill** → hedge → the cycle goes
  `LEG_2_PARTIAL → REBALANCING → COMPLETED` and the shortfall is traded away.
* **Arm order rejection** → hedge → `RECOVERY_REQUIRED → REBALANCING →
  COMPLETED`. Leg 1 was on the book and the system unwound the imbalance.
* **Create an unexpected venue position**, then look at the reconciliation
  panel: `UNKNOWN_POSITION`, critical, with a suggested action.
* **Run scenario G** (large spread): real BTC spreads are ~0.1 bps, so the
  overlay forces 100 bps, past the 50 bps execution limit, and the hedge is
  refused before taking exposure.
* **Run scenario J** (restart recovery) and read the steps: state discarded,
  gap detected, rehydrated, `resumable=True`.
* **Engage kill switch** → every position flattened, further trading refused
  with a 409.

### 6. Risk Dashboard (1 min)

Margin utilisation, liquidation distance per leg, exposure by venue,
underlying and settlement currency (USD and USDC are reported separately), and
the recorded risk events with the threshold each one breached.

## Option 3 — CLI only

```bash
hedgelab instruments pairs        # the conversions
hedgelab calculate --source PAPER_DELTA:BTCUSD --hedge PAPER_HYPERLIQUID:BTC \
                   --quantity 5000 --objective ACCOUNT_CCY_PNL_NEUTRAL
hedgelab optimize --source PAPER_DELTA:PAXGUSD --hedge PAPER_HYPERLIQUID:PAXG \
                  --quantity 3737 --objective NOTIONAL_NEUTRAL \
                  --mode STEP_SEARCH --priority MIN_RESIDUAL
hedgelab scenario run-all
hedgelab backtest run --history backend/tests/data/history.json.gz
hedgelab backtest run --history backend/tests/data/history.json.gz --ratio 0.5
```

The backtest replays 30 days of real hourly closes and the funding each venue
actually settled: at ratio 1.0 it removes 98.3% of the price drawdown and nets
+$275.87, at 0.5 it removes 50.6%.

## The three points worth making

1. **`1 contract ≠ 1 coin`, and the ratio differs per pair.** Quantity matching
   is a 1000× position error on the BTC pair, not a rounding issue.
2. **The objective you pick changes the answer**, for legs settling in
   different currencies, for legs whose prices differ by a real basis, and —
   in the engine, though no live pair uses one — for inverse contracts. The
   platform shows the difference instead of choosing for you.
3. **The second leg is where hedging actually goes wrong**, and every failure
   mode has a specific, demonstrable, tested response — including the ones that
   only appear after a restart.
