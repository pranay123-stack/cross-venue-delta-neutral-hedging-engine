"""Price impact measured from real order books.

Hourly candles carry no depth, so a backtest on them cannot see that a large
order walks the book.  This module measures how far it walks: for a ladder of
order sizes, sweep every recorded book of an instrument and take the median
cost of the sweep relative to the mid.  The backtest then charges that measured
cost instead of a flat half-spread.

The curve is only as deep as the recording.  An order larger than the
shallowest recorded side cannot be priced from data; it is charged at the
deepest measured point and *counted*, so a report can say how much of its cost
is extrapolation rather than measurement.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, OrderBookLevel
from ..domain.quantity import QuantityConverter
from .frames import MarketFrame

BPS = Decimal(10000)

#: Order sizes, as quote-currency notional, at which impact is measured.
DEFAULT_LADDER: tuple[Decimal, ...] = tuple(
    Decimal(n) for n in (1_000, 10_000, 50_000, 100_000, 250_000, 500_000,
                         1_000_000, 2_500_000, 5_000_000)
)


@dataclass(frozen=True, slots=True)
class ImpactPoint:
    notional: Decimal
    #: Median cost of a taker sweep of this size, in bps of the mid.
    buy_bps: Decimal
    sell_bps: Decimal
    #: Frames whose book was deep enough to fill this size in full.
    samples: int


@dataclass(frozen=True, slots=True)
class ImpactCurve:
    key: str
    points: tuple[ImpactPoint, ...]
    frames: int

    @property
    def max_notional(self) -> Decimal:
        return self.points[-1].notional if self.points else Decimal(0)

    def cost_bps(self, notional: Decimal, is_buy: bool) -> tuple[Decimal, bool]:
        """``(cost_bps, measured)`` for a taker order of ``notional``.

        Linear interpolation between measured points; below the first point
        the first point's cost applies (the touch spread dominates).  Above the
        deepest measured size the deepest cost applies and ``measured`` is
        False -- the book needed to price it was never observed.
        """
        if not self.points:
            raise ValueError(f"{self.key}: empty impact curve")
        size = abs(notional)
        sizes = [p.notional for p in self.points]
        costs = [p.buy_bps if is_buy else p.sell_bps for p in self.points]
        if size <= sizes[0]:
            return costs[0], True
        if size > sizes[-1]:
            return costs[-1], False
        i = bisect_left(sizes, size)
        lo, hi = sizes[i - 1], sizes[i]
        weight = (size - lo) / (hi - lo)
        return costs[i - 1] + (costs[i] - costs[i - 1]) * weight, True


def _book(frame: MarketFrame, key: str) -> OrderBook:
    quote = frame.quotes[key]
    return OrderBook(
        venue="", symbol="",
        bids=tuple(OrderBookLevel(p, s) for p, s in quote.bids),
        asks=tuple(OrderBookLevel(p, s) for p, s in quote.asks),
        timestamp=frame.timestamp,
    )


def measure_impact(
    spec: InstrumentSpec,
    frames: Sequence[MarketFrame],
    ladder: Sequence[Decimal] = DEFAULT_LADDER,
    *,
    min_coverage: float = 0.5,
) -> ImpactCurve:
    """Median sweep cost per ladder size, over every frame quoting ``spec``.

    A size is kept only while at least ``min_coverage`` of the frames could
    fill it in full -- a median over the few deepest books would understate
    the cost of the typical one.
    """
    converter = QuantityConverter(spec)
    books = [_book(f, spec.key) for f in frames if spec.key in f.quotes]
    books = [b for b in books if b.bids and b.asks]
    points: list[ImpactPoint] = []
    for notional in ladder:
        buys: list[Decimal] = []
        sells: list[Decimal] = []
        for book in books:
            mid = book.mid
            quantity = converter.quantity_from_notional(notional, mid)
            for is_buy, bucket in ((True, buys), (False, sells)):
                filled, vwap = book.sweep(is_buy, quantity)
                if filled < quantity or filled == 0:
                    continue
                signed = (vwap - mid) if is_buy else (mid - vwap)
                bucket.append(signed / mid * BPS)
        covered = min(len(buys), len(sells))
        if not books or covered < min_coverage * len(books):
            break
        points.append(ImpactPoint(notional=notional, buy_bps=median(buys),
                                  sell_bps=median(sells), samples=covered))
    return ImpactCurve(key=spec.key, points=tuple(points), frames=len(books))
