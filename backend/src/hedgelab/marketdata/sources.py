"""Public, unauthenticated market-data clients for the real venues.

Read-only by construction: every request here is a public GET (Delta,
Kraken) or a public ``info`` POST (Hyperliquid).  None of them can place,
cancel or even see an order, and none of them carries a credential -- there is
no setting anywhere that could hold one.

Units differ between venues, and each normalisation below was verified
against the live endpoints rather than taken from the docs:

* **Delta Exchange** publishes ``funding_rate`` as a *percent per funding
  interval*: SOLUSD sits at ``0.01`` -- the conventional 0.01 % per 8h
  baseline, where a fraction would mean 1,095 % a year -- and ccxt divides
  the field by 100 for the same reason.  The ticker value is the live
  *predicted* rate; ``FUNDING:<symbol>`` candles carry the last *settled*
  one, in the same unit.  (The product's ``annualized_funding`` is the
  *maximum allowed* annualised rate, not the current one.)  Book sizes are
  in contracts.
* **Hyperliquid** publishes ``funding`` as a *fraction per hour*, and book
  sizes in coins.

Comparing the raw fields would overstate one venue by 100x on units and 8x on
interval, so everything leaves this module as a fraction per the instrument's
own interval.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from ..domain.numeric import dec
from ..logging_setup import get_logger
from .ratelimit import (
    RETRYABLE_STATUSES,
    RetryPolicy,
    Sleep,
    WeightBudget,
    budget_for,
    parse_retry_after,
)

log = get_logger(__name__)

DELTA_INDIA_URL = "https://api.india.delta.exchange"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz"
KRAKEN_URL = "https://api.kraken.com"

HUNDRED = Decimal(100)


class MarketDataUnavailable(RuntimeError):
    """A venue's public endpoint could not be read."""

    def __init__(self, venue: str, detail: str) -> None:
        super().__init__(f"{venue} market data unavailable: {detail}")
        self.venue = venue


@dataclass(frozen=True, slots=True)
class BookSide:
    """One side of a book as ``(price, size)`` pairs, best first."""

    levels: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True, slots=True)
class VenueQuote:
    """Everything one poll learns about one instrument."""

    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    mark: Decimal
    #: Fraction per the instrument's own funding interval.
    funding_rate: Decimal | None
    volume_24h: Decimal
    venue_timestamp: datetime


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time: datetime
    #: Fraction per the instrument's own funding interval.
    rate: Decimal


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _from_ms(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, UTC)


def _from_us(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1_000_000, UTC)


class _Client:
    venue = ""

    def __init__(self, base_url: str, *, timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None,
                 retry: RetryPolicy | None = None, sleep: Sleep = asyncio.sleep,
                 budget: WeightBudget | None = None, seed: int = 0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._budget = budget
        self._rng = random.Random(seed)
        #: Retries performed and weight-limit waits, for observability.
        self.retries = 0
        self.throttled_seconds = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _weight(self, method: str, path: str, kwargs: dict[str, Any]) -> int:
        return 1

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        budget = self._budget or budget_for(httpx.URL(url).host)
        weight = self._weight(method, path, kwargs)
        last: str = ""
        for attempt in range(1, self.retry.attempts + 1):
            if budget is not None:
                self.throttled_seconds += await budget.acquire(weight)
            retry_after: float | None = None
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code not in RETRYABLE_STATUSES:
                    response.raise_for_status()
                    return response.json()
                last = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
            except httpx.HTTPStatusError as exc:
                # A non-retryable client error: asking again will not help.
                raise MarketDataUnavailable(self.venue, f"{method} {path}: {exc}") from exc
            except (httpx.TransportError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt == self.retry.attempts:
                break
            self.retries += 1
            delay = self.retry.delay(attempt, retry_after, self._rng)
            log.warning("venue request retried", extra={
                "venue": self.venue, "path": path, "attempt": attempt,
                "reason": last, "delay_s": round(delay, 3)})
            await self._sleep(delay)
        raise MarketDataUnavailable(
            self.venue, f"{method} {path}: gave up after {self.retry.attempts} attempts ({last})")


class DeltaPublicClient(_Client):
    """Delta Exchange (India) public REST API."""

    venue = "DELTA"

    def __init__(self, base_url: str = DELTA_INDIA_URL, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)

    async def _result(self, path: str, **params: Any) -> Any:
        payload = await self._request("GET", path, params=params or None)
        if not payload.get("success", False):
            raise MarketDataUnavailable(self.venue, f"{path}: {payload.get('error')}")
        return payload["result"]

    async def product(self, symbol: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._result(f"/v2/products/{symbol}")
        return result

    async def ticker(self, symbol: str) -> dict[str, Any]:
        result = await self._result(f"/v2/tickers/{symbol}")
        if not result:
            raise MarketDataUnavailable(self.venue, f"no ticker for {symbol}")
        return dict(result)

    async def orderbook(self, symbol: str, depth: int = 20) -> dict[str, Any]:
        result: dict[str, Any] = await self._result(f"/v2/l2orderbook/{symbol}", depth=depth)
        return result

    async def quote(self, symbol: str, depth: int = 20) -> VenueQuote:
        ticker, book = await asyncio.gather(self.ticker(symbol), self.orderbook(symbol, depth))
        raw_funding = ticker.get("funding_rate")
        return VenueQuote(
            symbol=symbol,
            bids=tuple((dec(lvl["price"]), dec(lvl["size"])) for lvl in book.get("buy", [])),
            asks=tuple((dec(lvl["price"]), dec(lvl["size"])) for lvl in book.get("sell", [])),
            mark=dec(ticker["mark_price"]),
            funding_rate=dec(raw_funding) / HUNDRED if raw_funding is not None else None,
            volume_24h=dec(ticker.get("volume") or 0),
            venue_timestamp=_from_us(book.get("last_updated_at") or ticker["timestamp"]),
        )

    async def candles(self, symbol: str, resolution: str, start: datetime,
                      end: datetime) -> list[Candle]:
        """OHLC candles, oldest first.  ``symbol`` may carry a ``MARK:`` prefix."""
        rows = await self._result(
            "/v2/history/candles", resolution=resolution, symbol=symbol,
            start=int(start.timestamp()), end=int(end.timestamp()),
        )
        candles = [
            Candle(
                time=datetime.fromtimestamp(row["time"], UTC),
                open=dec(row["open"]), high=dec(row["high"]), low=dec(row["low"]),
                close=dec(row["close"]), volume=dec(row.get("volume") or 0),
            )
            for row in rows
        ]
        return sorted(candles, key=lambda c: c.time)

    async def funding_history(self, symbol: str, start: datetime,
                              end: datetime) -> list[FundingPoint]:
        """Settled funding, one point per hour, as a fraction per interval.

        Delta exposes this only as ``FUNDING:<symbol>`` candles; each hourly
        candle holds the last settled rate, so consecutive points repeat until
        the next settlement.
        """
        candles = await self.candles(f"FUNDING:{symbol}", "1h", start, end)
        return [FundingPoint(time=c.time, rate=c.close / HUNDRED) for c in candles]


class HyperliquidPublicClient(_Client):
    """Hyperliquid public ``info`` endpoint."""

    venue = "HYPERLIQUID"

    def __init__(self, base_url: str = HYPERLIQUID_URL, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)

    #: Request weight of each ``info`` type, from Hyperliquid's rate-limit
    #: rules; anything not listed costs 20.
    WEIGHTS = {"l2Book": 2, "allMids": 2, "clearinghouseState": 2, "orderStatus": 2,
               "spotClearinghouseState": 2, "exchangeStatus": 2}

    def _weight(self, method: str, path: str, kwargs: dict[str, Any]) -> int:
        body = kwargs.get("json") or {}
        return self.WEIGHTS.get(str(body.get("type")), 20)

    async def info(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/info", json=body)

    async def meta_and_contexts(self) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """``{coin: (universe entry, asset context)}`` for every perp."""
        meta, contexts = await self.info({"type": "metaAndAssetCtxs"})
        return {
            asset["name"]: (asset, ctx)
            for asset, ctx in zip(meta["universe"], contexts, strict=True)
        }

    async def l2_book(self, coin: str) -> dict[str, Any]:
        book: dict[str, Any] = await self.info({"type": "l2Book", "coin": coin})
        return book

    async def quotes(self, coins: Sequence[str]) -> dict[str, VenueQuote]:
        contexts, *books = await asyncio.gather(
            self.meta_and_contexts(), *(self.l2_book(coin) for coin in coins)
        )
        result: dict[str, VenueQuote] = {}
        for coin, book in zip(coins, books, strict=True):
            if coin not in contexts:
                raise MarketDataUnavailable(self.venue, f"unknown coin {coin}")
            _, ctx = contexts[coin]
            bids, asks = book["levels"]
            result[coin] = VenueQuote(
                symbol=coin,
                bids=tuple((dec(lvl["px"]), dec(lvl["sz"])) for lvl in bids),
                asks=tuple((dec(lvl["px"]), dec(lvl["sz"])) for lvl in asks),
                mark=dec(ctx["markPx"]),
                funding_rate=dec(ctx["funding"]),
                volume_24h=dec(ctx.get("dayBaseVlm") or 0),
                venue_timestamp=_from_ms(book["time"]),
            )
        return result

    async def candles(self, coin: str, interval: str, start: datetime,
                      end: datetime) -> list[Candle]:
        rows = await self.info({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": _ms(start), "endTime": _ms(end)},
        })
        return [
            Candle(time=_from_ms(row["t"]), open=dec(row["o"]), high=dec(row["h"]),
                   low=dec(row["l"]), close=dec(row["c"]), volume=dec(row["v"]))
            for row in rows
        ]

    async def funding_history(self, coin: str, start: datetime,
                              end: datetime) -> list[FundingPoint]:
        """Settled hourly funding.  The endpoint pages at 500 rows."""
        points: list[FundingPoint] = []
        cursor = start
        while cursor < end:
            rows = await self.info({
                "type": "fundingHistory", "coin": coin,
                "startTime": _ms(cursor), "endTime": _ms(end),
            })
            if not rows:
                break
            batch = [FundingPoint(time=_from_ms(r["time"]), rate=dec(r["fundingRate"]))
                     for r in rows]
            points.extend(batch)
            last = batch[-1].time
            if last <= cursor:
                break
            cursor = datetime.fromtimestamp(last.timestamp() + 1, UTC)
            if len(rows) < 500:
                break
        return points


class KrakenFxClient(_Client):
    """Stablecoin/USD rates from Kraken's public ticker.

    The hedge legs settle in USD (Delta India) and USDC (Hyperliquid).  The
    rate between them is close to one but is *measured*, not assumed.
    """

    venue = "KRAKEN"
    PAIRS = {("USDC", "USD"): "USDCUSD", ("USDT", "USD"): "USDTUSD"}

    def __init__(self, base_url: str = KRAKEN_URL, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)

    async def rates(self) -> dict[tuple[str, str], Decimal]:
        names = ",".join(self.PAIRS.values())
        payload = await self._request("GET", "/0/public/Ticker", params={"pair": names})
        if payload.get("error"):
            raise MarketDataUnavailable(self.venue, str(payload["error"]))
        result = payload["result"]
        rates: dict[tuple[str, str], Decimal] = {}
        for pair, name in self.PAIRS.items():
            # Kraken renames some pairs in its response (USDTUSD -> USDTZUSD).
            row = next((v for k, v in result.items() if k.startswith(name[:4])), None)
            if row is None:
                continue
            bid, ask = dec(row["b"][0]), dec(row["a"][0])
            rates[pair] = (bid + ask) / Decimal(2)
        return rates
