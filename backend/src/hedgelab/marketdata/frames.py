"""Market frames: one synchronised observation of every instrument.

A frame is what one poll of the real venues returned -- order books, mark
prices, funding and FX -- stamped with the time it was taken.  Live operation
produces frames continuously; recording writes them to gzipped JSON lines so
tests, scenarios and the demo can replay *real* market data offline and
reproducibly.  Nothing in this module invents a price.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, cast

from ..domain.numeric import dec

FORMAT_VERSION = 1

Level = tuple[Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class InstrumentQuote:
    """One instrument's state in a frame, in the venue's own quantity unit."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    mark: Decimal
    #: Fraction per the instrument's funding interval, or ``None``.
    funding_rate: Decimal | None
    volume_24h: Decimal
    venue_timestamp: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "bids": [[str(p), str(s)] for p, s in self.bids],
            "asks": [[str(p), str(s)] for p, s in self.asks],
            "mark": str(self.mark),
            "funding_rate": None if self.funding_rate is None else str(self.funding_rate),
            "volume_24h": str(self.volume_24h),
            "venue_ts": self.venue_timestamp.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> InstrumentQuote:
        return cls(
            bids=tuple((dec(p), dec(s)) for p, s in raw["bids"]),
            asks=tuple((dec(p), dec(s)) for p, s in raw["asks"]),
            mark=dec(raw["mark"]),
            funding_rate=None if raw.get("funding_rate") is None else dec(raw["funding_rate"]),
            volume_24h=dec(raw.get("volume_24h") or 0),
            venue_timestamp=datetime.fromisoformat(raw["venue_ts"]),
        )


@dataclass(frozen=True, slots=True)
class MarketFrame:
    timestamp: datetime
    quotes: dict[str, InstrumentQuote]
    #: ``{(base, quote): rate}`` measured in the same poll.
    fx: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    #: Instruments whose venue could not be read in this poll.
    missing: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "v": FORMAT_VERSION,
            "t": self.timestamp.isoformat(),
            "quotes": {key: q.to_json() for key, q in sorted(self.quotes.items())},
            "fx": {f"{b}/{q}": str(r) for (b, q), r in sorted(self.fx.items())},
            "missing": list(self.missing),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> MarketFrame:
        if raw.get("v") != FORMAT_VERSION:
            raise ValueError(f"unsupported frame format {raw.get('v')!r}")
        fx: dict[tuple[str, str], Decimal] = {}
        for pair, rate in raw.get("fx", {}).items():
            base, quote = pair.split("/")
            fx[(base, quote)] = dec(rate)
        return cls(
            timestamp=datetime.fromisoformat(raw["t"]),
            quotes={k: InstrumentQuote.from_json(v) for k, v in raw["quotes"].items()},
            fx=fx,
            missing=tuple(raw.get("missing", ())),
        )


def _open_text(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        # gzip.open's stubs widen to GzipFile; text mode returns a TextIOWrapper.
        return cast(IO[str], gzip.open(path, mode + "t", encoding="utf-8"))
    return path.open(mode, encoding="utf-8")


def write_frames(path: Path, frames: Iterable[MarketFrame], *, append: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with _open_text(path, "a" if append else "w") as handle:
        for frame in frames:
            handle.write(json.dumps(frame.to_json(), separators=(",", ":")) + "\n")
            count += 1
    return count


def read_frames(path: Path) -> Iterator[MarketFrame]:
    with _open_text(path, "r") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield MarketFrame.from_json(json.loads(line))


def load_frames(path: Path) -> list[MarketFrame]:
    frames = list(read_frames(path))
    if not frames:
        raise ValueError(f"{path} holds no market frames")
    return frames
