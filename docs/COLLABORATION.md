# Working with this codebase

This repository is an engineering foundation and a technical demonstration: a
cross-exchange delta-neutral hedging engine that runs on real market data and
trades on paper. It is a reasonable starting point for discussion or for paid
custom development. It is **not** a verified profitable strategy, and it is
**not** a turnkey or regulated trading system.

The categories below are kept honest: section A lists only what is implemented
and verified; B is work this codebase is shaped to accept; C is work that needs
research before anyone can promise an outcome.

---

## A. Already implemented (verified)

Each item is backed by tests and commands described in
[TESTING.md](TESTING.md) and the [case study](ENGINEERING_CASE_STUDY.md).

* Real public market data from Delta Exchange (India) and Hyperliquid, polled
  live or replayed from a recording; Kraken for stablecoin FX.
* Instrument specs generated from each venue's metadata; unit normalisation
  for quantities, funding (percent/8h vs fraction/hour) and settlement currency.
* Hedge sizing under nine objectives, with a full derivation; a lattice-aware
  quantity optimiser; volatility, correlation and β measured from real data.
* Supervised two-leg paper execution against the real order books, with
  recovery from rejection, partial fills, timeouts, disconnects and restarts.
* Risk: per-pair and portfolio ladders, Delta's size-based margin, a kill switch
  that verifies flat and persists across restarts.
* A live-readiness layer: hard limits in front of every venue, rate limiting
  with backoff, an order-independent fill ledger, venue-native dry-run payloads,
  fill- and funding-level reconciliation, and an independent watchdog process.
* Backtests on 30 days of real hourly closes and settled funding, optionally
  with price impact measured on recorded books.
* A React dashboard, a FastAPI service with OpenAPI, a CLI, Docker Compose.

## B. Extension work this codebase is shaped for

Estimates are deliberately left out — they depend on access, venue behaviour
and requirements nobody has stated yet.

### Authenticated exchange adapters (testnet first)
* **Work:** implement `DeltaAdapter` and `HyperliquidAdapter` in
  `venues/live_stubs.py` against the nine-method `TradingVenue` interface:
  Delta's signed REST orders; Hyperliquid's signed `exchange` actions through an
  agent wallet. Register them behind the existing guard.
* **Depends on:** testnet accounts and keys held outside the repository; a
  decision to relax the paper-only barriers in a controlled way.
* **Tests:** the dry-run payloads already encode each venue's order shape —
  assert the signed request matches them; run `hedgelab demo` and all ten
  scenarios on testnet.
* **Accepted when:** the acceptance run passes on both testnets, every order
  goes through the hard-limit guard, and the watchdog flattens a testnet book.

### Private order and fill streams
* **Work:** subscribe to each venue's private order/fill channels; feed fills
  into the existing [`FillLedger`](../backend/src/hedgelab/execution/fill_ledger.py).
* **Tests:** replay captured private messages, including reconnect duplicates
  and out-of-order delivery (the ledger's property test already covers the
  logic).
* **Accepted when:** positions from the stream match the venue's REST positions
  after a forced reconnect.

### Reconciliation against authenticated account state
* **Work:** parsers from each venue's private fills, funding and balance
  endpoints into the normalised records
  [`reconcile_records`](../backend/src/hedgelab/reconciliation/venue_records.py)
  already checks.
* **Accepted when:** a daily run over a testnet account is clean, and an
  injected discrepancy of each kind is reported.

### Shadow execution and operational alerts
* **Work:** on live data, compute and record the exact orders (the dry-run
  layer does this today) and compare them with what a live adapter would
  receive; route alerts (disconnect, stale data, margin breach, watchdog
  trigger, limit refusal) to a channel such as Telegram or email.
* **Accepted when:** a week of shadow running shows every intended order as
  sendable and every alert class firing in a drill.

### Additional exchanges
* **Work:** a public data client, a spec generator and a paper venue per
  exchange; the core needs no change. Binance, Bybit and OKX perpetuals are the
  natural next venues.
* **Tests:** captured real responses for the client, a committed recording, the
  acceptance run on a new pair.

### Funding and basis analytics
* **Work:** a historical cross-venue funding and basis dataset (the history
  module already fetches settled funding), realised-versus-displayed carry
  analysis, and the conditions under which the carry flips.
* **Note:** a displayed funding spread is not realised carry; this work should
  start by measuring that gap.

### Execution-quality analysis
* **Work:** slippage and impact attribution per order against the recorded book
  at submit time; extend the measured impact curves to live fills once they
  exist.

### Observability
* **Work:** export the existing structured logs and events to Prometheus/Grafana
  or a log stack; dashboards for feed freshness, rate-limit headroom, limit
  refusals and watchdog health.

### Multi-process coordination (only if justified)
* **Work:** leader election and per-pair locks so more than one coordinator can
  run; only worth it for redundancy or many pairs.

## C. Needs research before promising anything

* **Exchange-specific operational guarantees** — what each venue actually
  guarantees about order acknowledgement, cancel-on-disconnect and fill
  reporting under load.
* **Recovery under real outages** — behaviour during real venue incidents,
  which a recording cannot reproduce.
* **Live execution quality** — real fill rates, latency and slippage at size.
* **Regulatory, custody and deployment requirements** — what is permitted for
  whom, where, and how keys and funds must be held.

---

## Getting in touch

If you want to discuss using or extending this engine — a venue integration, a
testnet build-out, an analytics project, or a review of your own hedging
infrastructure — open an issue describing what you have in mind, or reach out
through the maintainer's GitHub profile:
[github.com/pranay123-stack](https://github.com/pranay123-stack).
