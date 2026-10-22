"""Real price and funding history from the venues' public endpoints.

Two consumers:

* the **backtest** replays hourly candles and *settled* funding of both legs;
* the **statistics estimator** is seeded at startup from 5-minute candles, so
  volatility, correlation and beta are measured from the real market from the
  first request instead of waiting hours for live samples to accumulate.

A :class:`HistoryBundle` can be saved and committed, which is how the test
suite and the offline demo use real history without network access.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..domain.instrument import InstrumentSpec
from ..domain.numeric import dec
from .collector import SOURCE_DELTA, SOURCE_HYPERLIQUID
from .sources import Candle, DeltaPublicClient, FundingPoint, HyperliquidPublicClient

FORMAT_VERSION = 1

#: Candle resolutions in the venues' own spelling.
RESOLUTIONS = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1)}
#: Delta returns at most this many candles per request.
DELTA_PAGE = 2000
HYPERLIQUID_PAGE = 5000


@dataclass
class InstrumentHistory:
    key: str
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    funding: list[FundingPoint] = field(default_factory=list)

    def closes(self, resolution: str) -> list[tuple[datetime, Decimal]]:
        return [(c.time, c.close) for c in self.candles.get(resolution, [])]

    def funding_at(self, at: datetime) -> Decimal | None:
        """Last settled rate at or before ``at``."""
        rate: Decimal | None = None
        for point in self.funding:
            if point.time > at:
                break
            rate = point.rate
        return rate


@dataclass
class HistoryBundle:
    fetched_at: datetime
    instruments: dict[str, InstrumentHistory] = field(default_factory=dict)

    def __getitem__(self, key: str) -> InstrumentHistory:
        return self.instruments[key]

    # --- persistence --------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        def candle(c: Candle) -> list[str]:
            return [c.time.isoformat(), str(c.open), str(c.high), str(c.low), str(c.close),
                    str(c.volume)]

        return {
            "v": FORMAT_VERSION,
            "fetched_at": self.fetched_at.isoformat(),
            "instruments": {
                key: {
                    "candles": {res: [candle(c) for c in rows] for res, rows in h.candles.items()},
                    "funding": [[p.time.isoformat(), str(p.rate)] for p in h.funding],
                }
                for key, h in sorted(self.instruments.items())
            },
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> HistoryBundle:
        if raw.get("v") != FORMAT_VERSION:
            raise ValueError(f"unsupported history format {raw.get('v')!r}")
        bundle = cls(fetched_at=datetime.fromisoformat(raw["fetched_at"]))
        for key, body in raw["instruments"].items():
            history = InstrumentHistory(key=key)
            for res, rows in body["candles"].items():
                history.candles[res] = [
                    Candle(time=datetime.fromisoformat(r[0]), open=dec(r[1]), high=dec(r[2]),
                           low=dec(r[3]), close=dec(r[4]), volume=dec(r[5]))
                    for r in rows
                ]
            history.funding = [FundingPoint(time=datetime.fromisoformat(t), rate=dec(r))
                               for t, r in body["funding"]]
            bundle.instruments[key] = history
        return bundle

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_json(), separators=(",", ":"))
        if path.suffix == ".gz":
            path.write_bytes(gzip.compress(payload.encode("utf-8")))
        else:
            path.write_text(payload, encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> HistoryBundle:
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        return cls.from_json(json.loads(data))


def align_closes(
    bundle: HistoryBundle, source_key: str, hedge_key: str, resolution: str = "1h"
) -> list[tuple[datetime, Decimal, Decimal]]:
    """Candle closes of both legs at the timestamps where *both* printed."""
    hedge = dict(bundle[hedge_key].closes(resolution))
    return [
        (at, price, hedge[at])
        for at, price in bundle[source_key].closes(resolution)
        if at in hedge
    ]


async def _delta_candles(client: DeltaPublicClient, symbol: str, resolution: str,
                         start: datetime, end: datetime) -> list[Candle]:
    step = RESOLUTIONS[resolution] * (DELTA_PAGE - 1)
    candles: dict[datetime, Candle] = {}
    cursor = start
    while cursor < end:
        upper = min(cursor + step, end)
        for c in await client.candles(symbol, resolution, cursor, upper):
            candles[c.time] = c
        cursor = upper
    return sorted(candles.values(), key=lambda c: c.time)


async def _hl_candles(client: HyperliquidPublicClient, coin: str, resolution: str,
                      start: datetime, end: datetime) -> list[Candle]:
    step = RESOLUTIONS[resolution] * (HYPERLIQUID_PAGE - 1)
    candles: dict[datetime, Candle] = {}
    cursor = start
    while cursor < end:
        upper = min(cursor + step, end)
        for c in await client.candles(coin, resolution, cursor, upper):
            candles[c.time] = c
        cursor = upper
    return sorted(candles.values(), key=lambda c: c.time)


async def fetch_history(
    specs: list[InstrumentSpec],
    *,
    hourly_days: int = 30,
    five_minute_days: int = 3,
    end: datetime | None = None,
    delta: DeltaPublicClient | None = None,
    hyperliquid: HyperliquidPublicClient | None = None,
) -> HistoryBundle:
    """Fetch closed candles and settled funding for every instrument.

    The window ends at the last *closed* hour, so a candle still forming is
    never mixed with finished ones.
    """
    delta = delta or DeltaPublicClient()
    hyperliquid = hyperliquid or HyperliquidPublicClient()
    now = end or datetime.now(UTC)
    end_at = now.replace(minute=0, second=0, microsecond=0)
    windows = {"1h": end_at - timedelta(days=hourly_days),
               "5m": end_at - timedelta(days=five_minute_days)}
    bundle = HistoryBundle(fetched_at=now)

    async def one(spec: InstrumentSpec) -> InstrumentHistory:
        history = InstrumentHistory(key=spec.key)
        funding_start = windows["1h"]
        if spec.price_source == SOURCE_DELTA:
            for res, start in windows.items():
                history.candles[res] = await _delta_candles(delta, spec.symbol, res, start, end_at)
            history.funding = await delta.funding_history(spec.symbol, funding_start, end_at)
        elif spec.price_source == SOURCE_HYPERLIQUID:
            for res, start in windows.items():
                history.candles[res] = await _hl_candles(hyperliquid, spec.symbol, res, start,
                                                         end_at)
            history.funding = await hyperliquid.funding_history(spec.symbol, funding_start,
                                                                end_at)
        else:
            raise ValueError(f"{spec.key}: no history source for {spec.price_source!r}")
        # A candle stamped at end_at is still open.
        for res in history.candles:
            history.candles[res] = [c for c in history.candles[res] if c.time < end_at]
        return history

    try:
        for history in await asyncio.gather(*(one(spec) for spec in specs)):
            bundle.instruments[history.key] = history
    finally:
        await asyncio.gather(delta.close(), hyperliquid.close())
    return bundle
