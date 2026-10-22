# Paper trading — why no real order can be placed

This platform paper-trades two real venues — Delta Exchange (India) and
Hyperliquid — on their real public market data. Prices, books and funding are
real; the orders and fills are not. It has **no live order path**, and that is
enforced structurally rather than by a runtime flag anyone could flip.

## The five barriers

### 1. Only paper adapters are registered

`venues/factory.py` holds the complete list of constructible adapters:

```python
PAPER_ADAPTERS: dict[str, Callable[..., TradingVenue]] = {
    DELTA_VENUE:       PaperDeltaAdapter,        # "PAPER_DELTA"
    HYPERLIQUID_VENUE: PaperHyperliquidAdapter,  # "PAPER_HYPERLIQUID"
}

LIVE_ADAPTERS: dict[str, Callable[..., TradingVenue]] = {}
```

`LIVE_ADAPTERS` is empty. Registering something in it is the single change that
would make live trading reachable, and it is a deliberate, reviewable edit —
not something a misconfiguration can cause.

### 2. The factory refuses any non-paper mode

```python
if mode is not TradingMode.PAPER:
    raise LiveTradingDisabledError(...)
```

This fires **before** the adapter lookup, so `HEDGELAB_TRADING_MODE=LIVE`
raises even with `allow_live=True` set. Both are covered by tests:
`test_factory_refuses_live_mode` and
`test_factory_refuses_live_even_with_allow_live_set`.

### 3. The live adapter classes cannot even be constructed

`venues/live_stubs.py` defines `DeltaAdapter` and `HyperliquidAdapter` so the
extension point is visible and typed. Their `__init__` raises, and every method raises:

```python
def _refuse(operation: str, venue: str) -> NoReturn:
    raise LiveTradingDisabledError(
        f"{venue}.{operation}() is not implemented: this platform is "
        f"paper-trading only. See PAPER_TRADING.md."
    )
```

### 4. There is nothing to authenticate with

`config.py` has no field for an API key, secret, passphrase, wallet private key
or agent-wallet key for either venue. Nothing in the codebase reads one, and
none is needed: everything the platform reads is public market data.
The only credential-shaped setting is `HEDGELAB_API_KEYS`, which authenticates
callers *to this platform* and is stored as a SHA-256 hash.

### 5. Fills can only be created in one place

`PaperMatchingEngine._book_fill` is the only function in the codebase that
creates a `Fill`. It has no network client and cannot acquire one — verifying
"no order escapes" is a single-file audit. The paper fills walk the real book
captured in the current market frame, so slippage is the depth that was
actually there, but the order itself never leaves the process.

## What the container does

`backend/entrypoint.sh` refuses to start otherwise:

```bash
if [[ "${HEDGELAB_TRADING_MODE:-PAPER}" != "PAPER" ]]; then
  echo "refusing to start: ... this image has no live trading implementation."
  exit 78   # EX_CONFIG
fi
```

## What is visible at runtime

* Every HTTP response carries `X-Paper-Only: true` and `X-Trading-Mode: PAPER`.
* `/api/system/status` reports `paper_only: true` and
  `live_adapters_registered: 0`.
* Every `Order`, `OrderRequest`, position row and cycle row carries
  `is_paper=True`, persisted.
* The dashboard shows a permanent **PAPER TRADING ONLY** banner.

## Real market data, read only

`HEDGELAB_MARKET_DATA_SOURCE` is `live` (the default) or `replay`; nothing else
is accepted, and there is no synthetic source to fall back to.

The only module that touches the network is `marketdata/sources.py`, and every
call it makes is a public, unauthenticated read:

| Client | Endpoint | Used for |
|---|---|---|
| `DeltaPublicClient` | `api.india.delta.exchange` `/v2/products`, `/v2/tickers`, `/v2/l2orderbook`, `/v2/history/candles` | Specs, books, funding |
| `HyperliquidPublicClient` | `api.hyperliquid.xyz/info` (`metaAndAssetCtxs`, `l2Book`, `candleSnapshot`, `fundingHistory`) | Specs, books, funding |
| `KrakenFxClient` | `api.kraken.com/0/public/Ticker` | USDC/USD and USDT/USD |

Hyperliquid's trading surface is a separate endpoint (`/exchange`) that takes
signed actions; nothing in the codebase calls it, and there is no key that
could sign for it. Delta's order endpoints need an API key and signature; the
same applies.

`replay` reads a recording of a live session and makes no network call at
all. The test suite goes further: an autouse fixture makes any network access
fail the test.

## Dangerous actions still require confirmation

Paper money is not a reason to make destructive actions casual — the habits
should be the ones you would want on a live system. The kill switch and
`POST /api/reconciliation/adopt` both require an explicit
`X-Confirm-Action: CONFIRM` header and an `ADMIN` role, and both write to
`audit_logs` and `emergency_actions`.

## How you would add a real adapter

For completeness, because "extensible to live" is a design goal even though
live is not implemented:

1. Implement `TradingVenue` against the venue's real API in a new module —
   Delta's authenticated REST order endpoints, or Hyperliquid's signed
   `exchange` actions, ideally through an agent wallet that can trade but not
   withdraw.
2. Add credential settings, and load them from the environment.
3. Register the class in `LIVE_ADAPTERS`.
4. Relax the factory guard behind `allow_live`.
5. Add pre-trade limits that do not exist here: rate limiting, per-order
   notional caps, a mandatory dry-run mode, and independent reconciliation
   against the venue's own statements.

Steps 3 and 4 are the point of no return, and both are one-line, obvious edits
that a reviewer cannot miss.
