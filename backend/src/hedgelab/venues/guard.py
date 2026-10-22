"""Hard limits that sit *below* the strategy, in front of every venue.

The risk engine decides what the strategy *should* do.  This module decides
what any order is *allowed* to do, whatever produced it -- the coordinator,
the rebalancer, an operator over the API, or a bug in any of them.  Every
venue the factory builds is wrapped in a :class:`GuardedVenue`, so there is no
path to ``place_order`` that does not pass through these checks.

Limits, all in the account currency, per venue:

* **max order notional** -- one order may not be larger than this;
* **max position notional** -- the position an order would leave may not
  exceed this;
* **max daily loss** -- once the venue's equity has fallen this far since the
  first check of the UTC day, only orders that reduce exposure are accepted;
* **max orders per minute** -- a runaway loop is stopped at the venue door.

Orders that only *reduce* a position are exempt from the size and loss limits:
the kill switch and the risk engine must always be able to de-risk.  They
still count toward the order rate.  Every refusal is recorded with its reason.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..domain.account import AccountSnapshot
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, Ticker, utcnow
from ..domain.numeric import ZERO, quantize
from ..domain.orders import Order, OrderRequest, Position
from ..domain.quantity import QuantityConverter
from ..logging_setup import get_logger
from ..marketdata.fx import FxService
from .base import OrderRejected, TradingVenue
from .payloads import DryRunOrder, build_dry_run

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VenueLimits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_orders_per_minute: int


@dataclass(frozen=True, slots=True)
class LimitBreach:
    venue: str
    symbol: str
    limit: str
    detail: str
    at: datetime

    def to_dict(self) -> dict[str, str]:
        return {"venue": self.venue, "symbol": self.symbol, "limit": self.limit,
                "detail": self.detail, "at": self.at.isoformat()}


class LimitExceeded(OrderRejected):
    """An order refused by a hard limit.  Not retryable without changing it."""

    def __init__(self, venue: str, limit: str, detail: str) -> None:
        super().__init__(venue, f"hard limit {limit}: {detail}")
        self.limit = limit


@dataclass
class _DayState:
    day: date
    start_equity: Decimal


class GuardedVenue(TradingVenue):
    """Wraps a venue; every order passes the hard limits first."""

    def __init__(
        self,
        *,
        inner: TradingVenue,
        instruments: dict[str, InstrumentSpec],
        fx: FxService,
        limits: VenueLimits,
        clock: Callable[[], datetime] = utcnow,
        dry_run: bool = True,
    ) -> None:
        self.inner = inner
        self.instruments = instruments
        self.fx = fx
        self.limits = limits
        self.clock = clock
        # The guard is transparent: it answers to the wrapped venue's identity.
        self.name = inner.name
        self.account_currency = inner.account_currency
        self.is_paper = inner.is_paper
        self.breaches: list[LimitBreach] = []
        self.dry_run = dry_run
        #: The venue-native order each request would have been, newest last.
        self.dry_runs: deque[DryRunOrder] = deque(maxlen=500)
        self._orders: deque[datetime] = deque()
        self._day: _DayState | None = None

    def __getattr__(self, item: str) -> Any:
        # Venue-specific extras (``engine``, ``apply_funding``, ...) pass through.
        if item.startswith("_") or item == "inner":
            raise AttributeError(item)
        return getattr(self.inner, item)

    # --- the limits -----------------------------------------------------
    def _refuse(self, request: OrderRequest, limit: str, detail: str) -> LimitExceeded:
        breach = LimitBreach(self.name, request.symbol, limit, detail, self.clock())
        self.breaches.append(breach)
        log.error("order refused by hard limit", extra=breach.to_dict())
        return LimitExceeded(self.name, limit, detail)

    async def _position(self, symbol: str) -> Decimal:
        for position in await self.inner.get_positions():
            if position.symbol == symbol:
                return position.quantity
        return ZERO

    async def _daily_loss(self) -> Decimal:
        snapshot = await self.inner.get_balance()
        today = self.clock().date()
        if self._day is None or self._day.day != today:
            self._day = _DayState(day=today, start_equity=snapshot.equity)
        return self._day.start_equity - snapshot.equity

    async def check(self, request: OrderRequest) -> tuple[bool, Decimal | None]:
        """Raise :class:`LimitExceeded` if ``request`` breaks any limit.

        Returns ``(reducing, mid)`` for the dry-run payload.
        """
        now = self.clock()
        while self._orders and (now - self._orders[0]).total_seconds() >= 60:
            self._orders.popleft()
        if len(self._orders) >= self.limits.max_orders_per_minute:
            raise self._refuse(request, "max_orders_per_minute",
                               f"{len(self._orders)} orders in the last 60s")
        self._orders.append(now)

        spec = self.instruments.get(f"{self.name}:{request.symbol}")
        if request.venue != self.name or spec is None:
            return False, None  # misrouted or unknown: the venue itself rejects those
        existing = await self._position(request.symbol)
        after = existing + request.signed_quantity
        reducing = existing != ZERO and abs(after) <= abs(existing) and after * existing >= ZERO
        mid = (await self.inner.get_ticker(request.symbol)).mid
        if reducing:
            return True, mid

        converter = QuantityConverter(spec)
        rate = self.fx.try_rate(spec.quote_asset, self.account_currency)
        order_value = abs(converter.notional_quote(request.quantity, mid)) * rate
        if order_value > self.limits.max_order_notional:
            raise self._refuse(request, "max_order_notional",
                               f"order worth {quantize(order_value, 2)} exceeds "
                               f"{self.limits.max_order_notional}")
        position_value = abs(converter.notional_quote(after, mid)) * rate
        if position_value > self.limits.max_position_notional:
            raise self._refuse(request, "max_position_notional",
                               f"position would be worth {quantize(position_value, 2)}, "
                               f"limit {self.limits.max_position_notional}")
        loss = await self._daily_loss()
        if loss >= self.limits.max_daily_loss:
            raise self._refuse(request, "max_daily_loss",
                               f"down {quantize(loss, 2)} today, limit "
                               f"{self.limits.max_daily_loss}; only reducing orders accepted")
        return False, mid

    # --- TradingVenue ---------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        reducing, mid = await self.check(request)
        record = self._dry_run(request, reducing, mid)
        order = await self.inner.place_order(request)
        if record is not None:
            record.paper.update({
                "status": order.status.value, "filled": str(order.filled_quantity),
                "average_price": str(order.average_price), "fees": str(order.fees_paid),
            })
        return order

    def _dry_run(self, request: OrderRequest, reducing: bool,
                 mid: Decimal | None) -> DryRunOrder | None:
        spec = self.instruments.get(f"{self.name}:{request.symbol}")
        if not self.dry_run or spec is None or mid is None:
            return None
        record = build_dry_run(spec, request, mid, self.clock(), reducing=reducing)
        if record is None:
            return None
        self.dry_runs.append(record)
        if record.issues:
            log.warning("dry-run: the venue would reject this order", extra={
                "venue": self.name, "symbol": request.symbol, "issues": list(record.issues)})
        return record

    async def get_instruments(self) -> list[InstrumentSpec]:
        return await self.inner.get_instruments()

    async def get_ticker(self, symbol: str) -> Ticker:
        return await self.inner.get_ticker(symbol)

    async def get_orderbook(self, symbol: str, depth: int = 10) -> OrderBook:
        return await self.inner.get_orderbook(symbol, depth)

    async def get_balance(self) -> AccountSnapshot:
        return await self.inner.get_balance()

    async def get_positions(self) -> list[Position]:
        return await self.inner.get_positions()

    async def get_open_orders(self) -> list[Order]:
        return await self.inner.get_open_orders()

    async def cancel_order(self, order_id: str) -> Order:
        return await self.inner.cancel_order(order_id)

    async def get_order(self, order_id: str) -> Order:
        return await self.inner.get_order(order_id)

    async def connect(self) -> None:
        await self.inner.connect()

    async def disconnect(self) -> None:
        await self.inner.disconnect()

    async def health(self) -> dict[str, object]:
        detail = await self.inner.health()
        detail["hard_limits"] = {
            "max_order_notional": str(self.limits.max_order_notional),
            "max_position_notional": str(self.limits.max_position_notional),
            "max_daily_loss": str(self.limits.max_daily_loss),
            "max_orders_per_minute": self.limits.max_orders_per_minute,
            "breaches": len(self.breaches),
        }
        return detail
