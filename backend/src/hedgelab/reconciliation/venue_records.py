"""Reconcile what this process recorded against what the venue says happened.

The position reconciliation in :mod:`.engine` compares *positions*.  This goes
a level deeper, to the records a venue keeps and a real account statement
shows -- every execution and every funding payment -- because a position can
agree by accident while the trades underneath it do not:

* a fill the venue reports that this process never recorded;
* a fill this process recorded that the venue has no record of;
* the same fill with a different size, price or fee;
* a venue position that its own fill history does not explain;
* funding charged that differs from the funding expected at the real rates;
* equity that has drifted from what the records add up to.

Inputs are normalised records, so the checks are venue-independent.  The
paper venues produce them directly (:func:`records_from_paper_engine`); a live
adapter produces them by parsing the venue's private fills, funding and
balance endpoints.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..domain.numeric import ZERO, quantize
from ..domain.orders import Fill, Position
from ..execution.fill_ledger import FillLedger


class RecordIssue(StrEnum):
    FILL_MISSING_INTERNALLY = "FILL_MISSING_INTERNALLY"
    FILL_UNKNOWN_TO_VENUE = "FILL_UNKNOWN_TO_VENUE"
    FILL_MISMATCH = "FILL_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    FILLS_DONT_EXPLAIN_POSITION = "FILLS_DONT_EXPLAIN_POSITION"
    FUNDING_MISMATCH = "FUNDING_MISMATCH"
    BALANCE_DRIFT = "BALANCE_DRIFT"


CRITICAL = frozenset({
    RecordIssue.FILL_MISSING_INTERNALLY, RecordIssue.FILL_UNKNOWN_TO_VENUE,
    RecordIssue.POSITION_MISMATCH, RecordIssue.FILLS_DONT_EXPLAIN_POSITION,
})


@dataclass(frozen=True, slots=True)
class FillRecord:
    exec_id: str
    order_id: str
    symbol: str
    side_sign: int
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class FundingRecord:
    symbol: str
    amount: Decimal
    timestamp: datetime


@dataclass
class VenueRecords:
    venue: str
    positions: dict[str, Decimal] = field(default_factory=dict)
    fills: list[FillRecord] = field(default_factory=list)
    funding: list[FundingRecord] = field(default_factory=list)
    equity: Decimal | None = None
    #: True when ``fills`` is the account's complete history, so the fills
    #: alone must reproduce every position.
    complete_fill_history: bool = True


@dataclass(frozen=True, slots=True)
class RecordDiscrepancy:
    issue: RecordIssue
    venue: str
    subject: str
    message: str
    internal: str = ""
    venue_value: str = ""

    @property
    def critical(self) -> bool:
        return self.issue in CRITICAL

    def to_dict(self) -> dict[str, Any]:
        return {"issue": self.issue.value, "venue": self.venue, "subject": self.subject,
                "message": self.message, "internal": self.internal,
                "venue_value": self.venue_value, "critical": self.critical}


@dataclass(frozen=True, slots=True)
class Tolerances:
    quantity: Decimal = Decimal("1e-9")
    price: Decimal = Decimal("1e-9")
    fee: Decimal = Decimal("0.01")
    #: Funding may differ by this much *or* this fraction, whichever is larger:
    #: venues round payments, and rates move between our sample and settlement.
    funding_abs: Decimal = Decimal("0.05")
    funding_rel: Decimal = Decimal("0.02")
    equity: Decimal = Decimal("1")


@dataclass
class RecordReport:
    venue: str
    discrepancies: list[RecordDiscrepancy] = field(default_factory=list)
    fills_checked: int = 0
    funding_checked: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    @property
    def has_critical(self) -> bool:
        return any(d.critical for d in self.discrepancies)

    def to_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "clean": self.is_clean, "critical": self.has_critical,
                "fills_checked": self.fills_checked, "funding_checked": self.funding_checked,
                "discrepancies": [d.to_dict() for d in self.discrepancies]}


def reconcile_records(
    venue: VenueRecords,
    *,
    internal_positions: Iterable[Position],
    internal_fills: Iterable[FillRecord],
    expected_funding: Iterable[FundingRecord],
    internal_equity: Decimal | None = None,
    tolerances: Tolerances = Tolerances(),
) -> RecordReport:
    report = RecordReport(venue=venue.venue)
    add = report.discrepancies.append
    name = venue.venue

    # --- executions -----------------------------------------------------
    ours = {f.exec_id: f for f in internal_fills}
    theirs = {f.exec_id: f for f in venue.fills}
    report.fills_checked = len(set(ours) | set(theirs))
    for exec_id in sorted(set(theirs) - set(ours)):
        f = theirs[exec_id]
        add(RecordDiscrepancy(RecordIssue.FILL_MISSING_INTERNALLY, name, exec_id,
                              f"venue reports a {f.symbol} fill of {f.quantity} @ {f.price} "
                              f"this process never recorded", venue_value=str(f.quantity)))
    for exec_id in sorted(set(ours) - set(theirs)):
        f = ours[exec_id]
        add(RecordDiscrepancy(RecordIssue.FILL_UNKNOWN_TO_VENUE, name, exec_id,
                              f"recorded {f.symbol} fill of {f.quantity} @ {f.price} has no "
                              f"venue record", internal=str(f.quantity)))
    for exec_id in sorted(set(ours) & set(theirs)):
        a, b = ours[exec_id], theirs[exec_id]
        diffs = []
        if a.side_sign != b.side_sign or abs(a.quantity - b.quantity) > tolerances.quantity:
            diffs.append(f"size {a.side_sign * a.quantity} vs {b.side_sign * b.quantity}")
        if abs(a.price - b.price) > tolerances.price:
            diffs.append(f"price {a.price} vs {b.price}")
        if abs(a.fee - b.fee) > tolerances.fee:
            diffs.append(f"fee {a.fee} vs {b.fee}")
        if diffs:
            add(RecordDiscrepancy(RecordIssue.FILL_MISMATCH, name, exec_id,
                                  "; ".join(diffs) + " (internal vs venue)"))

    # --- positions --------------------------------------------------------
    internal = {p.symbol: p.quantity for p in internal_positions if p.venue == name}
    for symbol in sorted(set(internal) | set(venue.positions)):
        ours_qty = internal.get(symbol, ZERO)
        venue_qty = venue.positions.get(symbol, ZERO)
        if abs(ours_qty - venue_qty) > tolerances.quantity:
            add(RecordDiscrepancy(RecordIssue.POSITION_MISMATCH, name, symbol,
                                  f"{symbol}: recorded {ours_qty}, venue holds {venue_qty}",
                                  str(ours_qty), str(venue_qty)))

    if venue.complete_fill_history:
        ledger = FillLedger()
        for f in venue.fills:
            ledger.add(name, f.symbol, f.side_sign,
                       Fill(order_id=f.order_id, quantity=f.quantity, price=f.price, fee=f.fee,
                            is_maker=False, timestamp=f.timestamp, exec_id=f.exec_id))
        rebuilt = {p.symbol: p.quantity for p in ledger.positions()}
        for symbol in sorted(set(rebuilt) | set(venue.positions)):
            implied = rebuilt.get(symbol, ZERO)
            held = venue.positions.get(symbol, ZERO)
            if abs(implied - held) > tolerances.quantity:
                add(RecordDiscrepancy(RecordIssue.FILLS_DONT_EXPLAIN_POSITION, name, symbol,
                                      f"{symbol}: venue fills add up to {implied}, venue "
                                      f"position is {held}", str(implied), str(held)))

    # --- funding ----------------------------------------------------------
    expected: dict[str, Decimal] = defaultdict(lambda: ZERO)
    charged: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in expected_funding:
        expected[r.symbol] += r.amount
    for r in venue.funding:
        charged[r.symbol] += r.amount
    report.funding_checked = len(set(expected) | set(charged))
    for symbol in sorted(set(expected) | set(charged)):
        want, got = expected[symbol], charged[symbol]
        allowed = max(tolerances.funding_abs, abs(want) * tolerances.funding_rel)
        if abs(want - got) > allowed:
            add(RecordDiscrepancy(RecordIssue.FUNDING_MISMATCH, name, symbol,
                                  f"{symbol}: expected funding {quantize(want, 4)}, venue "
                                  f"charged {quantize(got, 4)}", str(want), str(got)))

    # --- equity -----------------------------------------------------------
    if (internal_equity is not None and venue.equity is not None
            and abs(internal_equity - venue.equity) > tolerances.equity):
        add(RecordDiscrepancy(RecordIssue.BALANCE_DRIFT, name, "equity",
                              f"recorded equity {quantize(internal_equity, 2)}, venue "
                              f"reports {quantize(venue.equity, 2)}",
                              str(internal_equity), str(venue.equity)))
    return report


def records_from_paper_engine(engine: Any) -> VenueRecords:
    """The paper venue's own records, as a live venue's statement would give them."""
    sides = {o.order_id: (o.request.symbol, o.request.side.sign) for o in engine.orders.values()}
    fills = []
    for fill in engine.fills:
        symbol, sign = sides.get(fill.order_id, ("", 0))
        fills.append(FillRecord(fill.exec_id, fill.order_id, symbol, sign, fill.quantity,
                                fill.price, fill.fee, fill.timestamp))
    return VenueRecords(
        venue=engine.venue,
        positions={p.symbol: p.quantity for p in engine.positions.values() if not p.is_flat},
        fills=fills,
        funding=[FundingRecord(a.symbol, a.amount, a.timestamp) for a in engine.funding_history],
        equity=engine.account().equity,
        complete_fill_history=not engine.restored_positions,
    )
