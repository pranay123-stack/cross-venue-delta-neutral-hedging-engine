# Roadmap

Prioritised by what is actually missing. Each item has a deliverable and an
acceptance criterion; nothing here re-builds a feature that already exists.

Status key: ✅ done · 🔜 next · ⏳ later

---

## Priority 1 — Public-project quality

| Item | Deliverable | Accepted when | Status |
|---|---|---|---|
| Documentation consistency | Every figure in README and docs re-measured | No doc quotes a number the code no longer produces | ✅ |
| One-command setup | `docker compose up --build`; `make install` / `make check` | A fresh clone runs both without manual steps | ✅ |
| Reproducible demo | `make demo` on the recorded session | Same 25/25 result offline, every run | ✅ |
| CI | `.github/workflows/ci.yml`: ruff, mypy, pytest, tsc, build, Playwright, audit | A green run on `main` | ✅ |
| Verified screenshots | `make screenshots` from the running app | Images in `docs/images/` come from the real dashboard | ✅ |
| Licensing and security policy | MIT `LICENSE`, `SECURITY.md` | Both present and linked from the README | ✅ |
| Owner-side GitHub settings | See below | All applied | 🔜 owner action |

### Owner-side GitHub settings (cannot be done from the repository)

1. **Rename the repository** (optional): Settings → General → Repository name →
   `crypto-cross-exchange-hedging-engine`. GitHub redirects the old URL; then
   replace `cross-venue-delta-neutral-hedging-engine` in README, SECURITY.md,
   `backend/pyproject.toml` and `.github/ISSUE_TEMPLATE/config.yml`.
2. **About → Description:** "Open-source crypto cross-exchange delta-neutral
   hedging engine with funding-rate analytics, hedge sizing, paper execution,
   risk controls, and recovery testing for Delta Exchange and Hyperliquid."
3. **About → Topics:** `crypto`, `cryptocurrency`, `algorithmic-trading`,
   `quantitative-finance`, `delta-neutral`, `hedging`, `arbitrage`,
   `funding-rate`, `perpetual-futures`, `risk-management`,
   `trading-infrastructure`, `python`, `fastapi`, `hyperliquid`,
   `delta-exchange`.
4. **Settings → Code security → Private vulnerability reporting:** enable it
   (SECURITY.md points reporters there).
5. **Settings → Branches:** protect `main` — require the CI checks
   `Backend (ruff, mypy, pytest)`, `Frontend (tsc, build)` and
   `Browser tests (Playwright, Chrome)` to pass before merging.
6. **Social preview:** Settings → General → Social preview → upload
   `docs/images/overview.png`.

## Priority 2 — Demonstration quality

| Item | Deliverable | Accepted when | Status |
|---|---|---|---|
| Dashboard first screen | Data source, execution mode, overlays, venues, safety, demo commands | A first-time visitor can tell live from replay, and paper from real, without reading docs | ✅ |
| Funding comparability | Per-interval and annualised funding side by side | 8-hourly and hourly venues compare on one scale | ✅ |
| Backtest view in the dashboard | A page that runs and charts `backtest run`, labelled historical | Results show interval, data provenance, fee and slippage assumptions | 🔜 |
| Failure walkthroughs | One-click scenarios already exist; add per-scenario explanation panels | Each scenario's outcome is explained on screen | ⏳ |
| Architecture walkthrough | A short recorded tour of the demo | Covers a hedge, a failure and the watchdog | ⏳ |

## Priority 3 — Engineering extension points

| Item | Deliverable | Accepted when | Status |
|---|---|---|---|
| Testnet adapters | Signed Delta REST and Hyperliquid `exchange` adapters, testnet only | Acceptance run and ten scenarios pass on both testnets | 🔜 needs testnet accounts |
| Authenticated venue state | Parsers from private fills, funding and balances into `reconcile_records` | A daily testnet reconciliation is clean; injected discrepancies are caught | 🔜 |
| Private streams | Order/fill WebSocket into the fill ledger | Positions match REST after a forced reconnect | ⏳ |
| Shadow mode | Live data, orders computed and logged, never sent | A week of shadow running, every intended order sendable | ⏳ |
| Execution telemetry | Feed freshness, rate-limit headroom, limit refusals, watchdog health as metrics | Exported and alerting | ⏳ |
| More failure injection | Venue latency spikes, partial cancel failures, clock skew | Each has a scenario and a test | ⏳ |
| Volatile-market recording | A committed recording across a real event (CPI, FOMC) | Stress tests replay a real dislocation, not only an imposed one | 🔜 needs the event |

## Priority 4 — Production-oriented research

| Item | Question | Status |
|---|---|---|
| Real latency and fill quality | What do fills, slippage and leg-to-leg gaps look like at size? | ⏳ needs testnet then tiny live size |
| Recovery under real venue failures | How do the venues behave in a real incident? | ⏳ |
| Stronger reconciliation | End-of-day statements, not only live API state | ⏳ |
| Deployment threat model | Keys, agent wallets, network isolation, watchdog placement | ⏳ |

## Non-goals

* **High-frequency arbitrage.** The engine polls every 3 seconds and holds
  positions for hours to weeks; latency optimisation is out of scope.
* **A strategy that decides when to trade.** It makes a requested hedge correct
  and safe; choosing the trade is the user's.
* **Live trading by default.** Any live path will stay opt-in, behind the hard
  limits and the watchdog.
* **Synthetic market data.** Tests and demos use real recordings.
