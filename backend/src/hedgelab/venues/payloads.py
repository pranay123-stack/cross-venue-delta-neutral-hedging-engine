"""Dry-run: the exact order each venue would receive, checked against its rules.

Nothing here is sent anywhere and nothing is signed.  For every paper order the
guard builds the venue-native payload a live adapter would submit, validates it
against that venue's published rules, and records it beside the paper result.
That answers the question paper fills cannot: *would the venue have accepted
this order, in this exact form?*  It is the step between paper and testnet.

Shapes, from each venue's API documentation (checked 2026-10-22):

* **Delta Exchange** ``POST /v2/orders`` -- ``product_id`` (integer),
  ``product_symbol``, ``size`` (integer contracts), ``side`` buy/sell,
  ``order_type`` ``limit_order``/``market_order``, ``limit_price`` (string),
  ``time_in_force`` ``gtc``/``ioc``, ``post_only``, ``reduce_only``,
  ``client_order_id``.
* **Hyperliquid** ``POST /exchange`` action ``order`` -- ``a`` asset index in
  ``meta.universe``, ``b`` is-buy, ``p`` price and ``s`` size as strings,
  ``r`` reduce-only, ``t`` ``{"limit": {"tif": "Alo"|"Ioc"|"Gtc"}}``, ``c`` a
  128-bit hex cloid.  There is no market order type: a market order is an IOC
  limit priced through the book, as the official SDK does with a 5% cap.

Neither venue accepts fill-or-kill, so a FOK order is flagged as unsendable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ..domain.enums import OrderType, TimeInForce
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import ZERO, normalize
from ..domain.orders import OrderRequest
from ..domain.quantity import QuantityConverter
from ..marketdata.collector import SOURCE_DELTA, SOURCE_HYPERLIQUID

#: How far through the book a Hyperliquid "market" order may trade.
HL_MARKET_SLIPPAGE = Decimal("0.05")
HL_MAX_DECIMALS = 6
DELTA_CLIENT_ID_MAX = 32


@dataclass(frozen=True, slots=True)
class DryRunOrder:
    venue: str
    symbol: str
    endpoint: str
    payload: dict[str, Any]
    #: Rules the payload breaks; empty means the venue should accept it.
    issues: tuple[str, ...]
    at: datetime
    #: Filled in after the paper order executes.
    paper: dict[str, str] = field(default_factory=dict)

    @property
    def sendable(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "symbol": self.symbol, "endpoint": self.endpoint,
                "payload": self.payload, "sendable": self.sendable,
                "issues": list(self.issues), "paper": self.paper, "at": self.at.isoformat()}


def _plain(value: Decimal) -> str:
    return format(normalize(value), "f")


def hyperliquid_price(price: Decimal, sz_decimals: int, *, round_up: bool) -> Decimal:
    """Snap to Hyperliquid's rule: five significant figures, at most
    ``6 - szDecimals`` decimals; integers are always allowed."""
    if price <= ZERO:
        raise ValueError("price must be positive")
    decimals = min(max(4 - price.adjusted(), 0), HL_MAX_DECIMALS - sz_decimals)
    step = Decimal(1).scaleb(-decimals)
    return (price / step).quantize(Decimal(1), rounding=ROUND_CEILING if round_up
                                   else ROUND_FLOOR) * step


def cloid(client_order_id: str) -> str:
    """A deterministic 128-bit hex cloid derived from our client order id."""
    return "0x" + hashlib.sha256(client_order_id.encode()).hexdigest()[:32]


def delta_order(spec: InstrumentSpec, request: OrderRequest, mid: Decimal,
                at: datetime, *, reducing: bool) -> DryRunOrder:
    issues: list[str] = []
    if not spec.venue_instrument_id.isdigit():
        issues.append("no Delta product_id in the instrument spec")
    if request.quantity != request.quantity.to_integral_value():
        issues.append(f"size {request.quantity} is not a whole number of contracts")
    if request.time_in_force is TimeInForce.FOK:
        issues.append("Delta accepts time_in_force gtc or ioc; fill-or-kill is not supported")
    payload: dict[str, Any] = {
        "product_id": int(spec.venue_instrument_id) if spec.venue_instrument_id.isdigit() else None,
        "product_symbol": spec.symbol,
        "size": int(request.quantity),
        "side": "buy" if request.side.sign > 0 else "sell",
        "order_type": "limit_order" if request.order_type is OrderType.LIMIT else "market_order",
        "time_in_force": "gtc" if request.time_in_force is TimeInForce.GTC else "ioc",
        "post_only": False,
        "reduce_only": reducing,
        "client_order_id": request.client_order_id[:DELTA_CLIENT_ID_MAX],
    }
    if len(request.client_order_id) > DELTA_CLIENT_ID_MAX:
        issues.append(f"client_order_id truncated to {DELTA_CLIENT_ID_MAX} characters")
    if request.order_type is OrderType.LIMIT and request.price is not None:
        payload["limit_price"] = _plain(request.price)
        if (request.price / spec.tick_size) % 1 != 0:
            issues.append(f"limit_price {request.price} is off the {spec.tick_size} tick")
    if request.quantity < spec.min_quantity or request.quantity > spec.max_quantity:
        issues.append(f"size {request.quantity} outside [{spec.min_quantity}, {spec.max_quantity}]")
    return DryRunOrder(spec.venue, spec.symbol, "POST /v2/orders", payload, tuple(issues), at)


def hyperliquid_order(spec: InstrumentSpec, request: OrderRequest, mid: Decimal,
                      at: datetime, *, reducing: bool) -> DryRunOrder:
    issues: list[str] = []
    sz_decimals = spec.quantity_precision
    is_buy = request.side.sign > 0
    if not spec.venue_instrument_id.isdigit():
        issues.append("no Hyperliquid asset index in the instrument spec")
    if request.order_type is OrderType.LIMIT and request.price is not None:
        price = request.price
        tif = "Gtc" if request.time_in_force is TimeInForce.GTC else "Ioc"
    else:
        # Market order: an IOC limit priced through the book, capped at 5%.
        bound = mid * (1 + HL_MARKET_SLIPPAGE) if is_buy else mid * (1 - HL_MARKET_SLIPPAGE)
        price = hyperliquid_price(bound, sz_decimals, round_up=not is_buy)
        tif = "Ioc"
    if request.time_in_force is TimeInForce.FOK:
        issues.append("Hyperliquid accepts Alo, Ioc or Gtc; fill-or-kill is not supported")
    if hyperliquid_price(price, sz_decimals, round_up=False) != price:
        issues.append(f"price {price} breaks five significant figures / "
                      f"{HL_MAX_DECIMALS - sz_decimals} decimals")
    step = Decimal(1).scaleb(-sz_decimals)
    if (request.quantity / step) % 1 != 0:
        issues.append(f"size {request.quantity} is not a multiple of {step}")
    value = abs(QuantityConverter(spec).notional_quote(request.quantity, mid))
    if not reducing and value < spec.min_notional:
        issues.append(f"order value {value:.2f} below the ${spec.min_notional} minimum")
    order: dict[str, Any] = {
        "a": int(spec.venue_instrument_id) if spec.venue_instrument_id.isdigit() else None,
        "b": is_buy,
        "p": _plain(price),
        "s": _plain(request.quantity),
        "r": reducing,
        "t": {"limit": {"tif": tif}},
        "c": cloid(request.client_order_id),
    }
    payload = {"type": "order", "orders": [order], "grouping": "na"}
    return DryRunOrder(spec.venue, spec.symbol, "POST /exchange", payload, tuple(issues), at)


def build_dry_run(spec: InstrumentSpec, request: OrderRequest, mid: Decimal, at: datetime,
                  *, reducing: bool) -> DryRunOrder | None:
    if spec.price_source == SOURCE_DELTA:
        return delta_order(spec, request, mid, at, reducing=reducing)
    if spec.price_source == SOURCE_HYPERLIQUID:
        return hyperliquid_order(spec, request, mid, at, reducing=reducing)
    return None
