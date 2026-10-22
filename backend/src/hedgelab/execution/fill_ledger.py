"""A position ledger that is correct whatever order fills arrive in.

A live venue reports executions asynchronously.  Over a WebSocket and a REST
backfill the same fill can arrive twice, a fill can arrive after a later one,
and a reconnect replays history.  Applying reports in arrival order gets the
average entry and realised P&L wrong whenever a position flips, and applying a
duplicate double-counts it.

The ledger keys every fill by its execution id (duplicates are ignored), keeps
fills in the venue's own sequence -- execution time, then id -- and, when a
late fill lands in the past, replays the instrument from the start.  The
result is the position the venue itself holds, independent of delivery order.
It also tracks cumulative fills per order and flags an overfill, which on a
real venue means a lost cancel or a bug.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from ..domain.numeric import ZERO
from ..domain.orders import Fill, Position


class FillOutcome(StrEnum):
    APPLIED = "APPLIED"
    #: Arrived after a later fill; the instrument was replayed in sequence.
    REORDERED = "REORDERED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, order=True, slots=True)
class _Entry:
    timestamp: datetime
    exec_id: str
    signed_quantity: Decimal = field(compare=False)
    price: Decimal = field(compare=False)
    fee: Decimal = field(compare=False)
    order_id: str = field(compare=False)


@dataclass
class _Book:
    venue: str
    symbol: str
    entries: list[_Entry] = field(default_factory=list)
    position: Position | None = None

    def replay(self) -> Position:
        position = Position(venue=self.venue, symbol=self.symbol)
        for entry in self.entries:
            position.apply(entry.signed_quantity, entry.price, entry.fee)
        self.position = position
        return position


@dataclass(frozen=True, slots=True)
class LedgerResult:
    outcome: FillOutcome
    position: Position
    #: Set when this order has now filled more than it asked for.
    overfilled_by: Decimal = ZERO


class FillLedger:
    """Positions rebuilt from execution reports, idempotent and order-free."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], _Book] = {}
        self._seen: set[str] = set()
        self._order_filled: dict[str, Decimal] = {}
        self._order_size: dict[str, Decimal] = {}

    def expect(self, order_id: str, quantity: Decimal) -> None:
        """Register an order's requested size so overfills can be detected."""
        self._order_size[order_id] = abs(quantity)

    def add(self, venue: str, symbol: str, side_sign: int, fill: Fill) -> LedgerResult:
        book = self._books.setdefault((venue, symbol), _Book(venue=venue, symbol=symbol))
        if fill.exec_id in self._seen:
            return LedgerResult(FillOutcome.DUPLICATE, book.position or book.replay())
        self._seen.add(fill.exec_id)

        entry = _Entry(fill.timestamp, fill.exec_id, fill.quantity * side_sign, fill.price,
                       fill.fee, fill.order_id)
        in_sequence = not book.entries or entry > book.entries[-1]
        insort(book.entries, entry)
        if in_sequence and book.position is not None:
            book.position.apply(entry.signed_quantity, entry.price, entry.fee)
            position, outcome = book.position, FillOutcome.APPLIED
        else:
            position = book.replay()
            outcome = FillOutcome.APPLIED if in_sequence else FillOutcome.REORDERED

        filled = self._order_filled.get(fill.order_id, ZERO) + abs(fill.quantity)
        self._order_filled[fill.order_id] = filled
        size = self._order_size.get(fill.order_id)
        over = filled - size if size is not None and filled > size else ZERO
        return LedgerResult(outcome, position, over)

    def position(self, venue: str, symbol: str) -> Position:
        book = self._books.get((venue, symbol))
        if book is None:
            return Position(venue=venue, symbol=symbol)
        return book.position or book.replay()

    def positions(self) -> list[Position]:
        return [self.position(v, s) for v, s in sorted(self._books)]

    def filled(self, order_id: str) -> Decimal:
        return self._order_filled.get(order_id, ZERO)
