# Security

## Scope

This repository trades **on paper only**. It holds no exchange credentials,
signs nothing, and can place no real order: the live adapters raise on every
method and the venue factory refuses to build them
([PAPER_TRADING.md](docs/PAPER_TRADING.md)). Its only network traffic is
public, unauthenticated market data from Delta Exchange (India), Hyperliquid and
Kraken.

Issues that matter here regardless:

* anything that could make an order leave the process, or bypass the hard
  limits, risk gate, kill switch or watchdog;
* a path that leaks secrets from the environment, or logs that could;
* injection or authorisation flaws in the API. It uses role-based API keys
  (`VIEWER`, `TRADER`, `ADMIN`, compared in constant time) when
  `HEDGELAB_API_KEYS` is set. With no keys it is open only in the `local` and
  `test` environments; in any other environment it refuses every request that
  needs an identity until keys are configured;
* dependency vulnerabilities (CI runs `pip-audit` and `npm audit`).

## Reporting

Please report vulnerabilities **privately** through GitHub's
[private vulnerability reporting](https://github.com/pranay123-stack/cross-venue-delta-neutral-hedging-engine/security/advisories/new)
rather than a public issue. Include steps to reproduce and the commit you
tested. You should get an acknowledgement within a few days.

## If you extend it towards live trading

Treat that as a different system with a different threat model:

* keep keys out of the repository and the image — a secrets manager, separate
  keys per environment;
* on Hyperliquid, sign with an **agent wallet** that can trade but not withdraw;
  on Delta, a trade-only API key restricted to your IP;
* keep the hard limits and the watchdog *below* any strategy code, and run the
  watchdog where a crash of the trading process cannot take it down;
* reconcile against the venues' own statements daily before trusting any P&L.

## Responsible use

The software is provided under the MIT licence, without warranty. Backtest and
paper results describe the past on the recorded data; they are not a forecast,
and nothing here is investment advice. Trading derivatives on either venue may
be restricted where you live.
