"""Poll the real venues once and assemble a :class:`MarketFrame`.

Which venue serves an instrument is decided by its spec's ``price_source``
(``delta_india`` or ``hyperliquid``), so adding an instrument is still a
configuration change.  A venue that fails to answer does not sink the poll:
its instruments are listed in ``frame.missing`` and the feed keeps their last
real quote, which then ages into staleness -- exactly the signal the risk gate
looks for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from ..domain.instrument import InstrumentSpec
from ..domain.market import utcnow
from ..logging_setup import get_logger
from .frames import InstrumentQuote, MarketFrame
from .sources import (
    DeltaPublicClient,
    HyperliquidPublicClient,
    KrakenFxClient,
    MarketDataUnavailable,
    VenueQuote,
)

log = get_logger(__name__)

SOURCE_DELTA = "delta_india"
SOURCE_HYPERLIQUID = "hyperliquid"
KNOWN_SOURCES = (SOURCE_DELTA, SOURCE_HYPERLIQUID)


def _quote(raw: VenueQuote, depth: int) -> InstrumentQuote:
    return InstrumentQuote(
        bids=raw.bids[:depth], asks=raw.asks[:depth], mark=raw.mark,
        funding_rate=raw.funding_rate, volume_24h=raw.volume_24h,
        venue_timestamp=raw.venue_timestamp,
    )


class LiveCollector:
    """Takes one synchronised snapshot of every configured instrument."""

    def __init__(
        self,
        *,
        delta: DeltaPublicClient | None = None,
        hyperliquid: HyperliquidPublicClient | None = None,
        fx: KrakenFxClient | None = None,
        depth: int = 20,
    ) -> None:
        self.delta = delta or DeltaPublicClient()
        self.hyperliquid = hyperliquid or HyperliquidPublicClient()
        self.fx = fx or KrakenFxClient()
        self.depth = depth

    async def close(self) -> None:
        await asyncio.gather(self.delta.close(), self.hyperliquid.close(), self.fx.close())

    async def collect(self, specs: Iterable[InstrumentSpec]) -> MarketFrame:
        by_source: dict[str, list[InstrumentSpec]] = {}
        unsourced: list[str] = []
        for spec in specs:
            if spec.price_source not in KNOWN_SOURCES:
                # Never guess a price for an instrument no real feed serves.
                unsourced.append(spec.key)
                continue
            by_source.setdefault(spec.price_source, []).append(spec)

        taken_at = utcnow()
        delta_specs = by_source.get(SOURCE_DELTA, [])
        hl_specs = by_source.get(SOURCE_HYPERLIQUID, [])
        results = await asyncio.gather(
            *(self.delta.quote(s.symbol, self.depth) for s in delta_specs),
            self.hyperliquid.quotes([s.symbol for s in hl_specs]) if hl_specs else _empty(),
            self.fx.rates(),
            return_exceptions=True,
        )

        quotes: dict[str, InstrumentQuote] = {}
        missing: list[str] = list(unsourced)
        for spec, result in zip(delta_specs, results[: len(delta_specs)], strict=True):
            if isinstance(result, BaseException):
                self._warn(spec.key, result)
                missing.append(spec.key)
            else:
                assert isinstance(result, VenueQuote)
                quotes[spec.key] = _quote(result, self.depth)

        hl_result = results[len(delta_specs)]
        if isinstance(hl_result, BaseException):
            for spec in hl_specs:
                self._warn(spec.key, hl_result)
                missing.append(spec.key)
        else:
            assert isinstance(hl_result, dict)
            for spec in hl_specs:
                quotes[spec.key] = _quote(hl_result[spec.symbol], self.depth)

        fx_result = results[-1]
        fx: dict[tuple[str, str], Decimal] = {}
        if isinstance(fx_result, BaseException):
            log.warning("fx rates unavailable this poll", extra={"error": str(fx_result)})
        else:
            assert isinstance(fx_result, dict)
            fx = fx_result
        return MarketFrame(timestamp=taken_at, quotes=quotes, fx=fx, missing=tuple(missing))

    @staticmethod
    def _warn(key: str, exc: BaseException) -> None:
        if not isinstance(exc, MarketDataUnavailable):
            log.error("unexpected market data failure", extra={"instrument": key,
                                                               "error": repr(exc)})
        else:
            log.warning("venue poll failed", extra={"instrument": key, "error": str(exc)})


async def _empty() -> dict[str, VenueQuote]:
    return {}


async def record(
    specs: list[InstrumentSpec],
    *,
    frames: int,
    interval_seconds: float,
    collector: LiveCollector | None = None,
) -> list[MarketFrame]:
    """Poll ``frames`` times, ``interval_seconds`` apart (start to start)."""
    owned = collector is None
    collector = collector or LiveCollector()
    loop = asyncio.get_running_loop()
    captured: list[MarketFrame] = []
    try:
        for index in range(frames):
            started = loop.time()
            frame = await collector.collect(specs)
            captured.append(frame)
            log.info("frame recorded", extra={"index": index, "missing": list(frame.missing)})
            if index + 1 < frames:
                await asyncio.sleep(max(0.0, interval_seconds - (loop.time() - started)))
    finally:
        if owned:
            await collector.close()
    return captured


def frame_age_seconds(frame: MarketFrame, now: datetime | None = None) -> float:
    return ((now or utcnow()) - frame.timestamp).total_seconds()
