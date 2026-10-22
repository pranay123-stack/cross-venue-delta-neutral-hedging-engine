"""Build instrument specifications from the venues' own published metadata.

The catalogue YAML is *generated*, not transcribed: ``hedgelab instruments
sync`` reads Delta Exchange's product endpoint and Hyperliquid's ``meta`` and
writes every field it can from them.  The few fields neither venue publishes
are marked in the output with the reason, so nothing in the catalogue is an
unexplained number.

Venue rules encoded here, each checked against the live API or the venue's
documentation on 2026-10-22:

* Delta: ``initial_margin`` / ``maintenance_margin`` are **percent**
  (``0.5`` = 0.5 % = 200x); commission rates are **fractions**; the funding
  interval is ``product_specs.rate_exchange_interval`` in seconds; book sizes
  are in contracts of ``contract_value`` base units.
* Hyperliquid: sizes are coins rounded to ``szDecimals``; prices allow at most
  five significant figures and ``6 - szDecimals`` decimals, so the tick
  *depends on the price level* and is fixed here at the level seen at sync;
  base-tier fees are 4.5 bps taker / 1.5 bps maker; funding settles hourly;
  maintenance margin is half the initial margin at maximum leverage; orders
  under $10 are rejected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..domain.numeric import dec, decimal_places, normalize
from ..marketdata.collector import SOURCE_DELTA, SOURCE_HYPERLIQUID
from ..marketdata.sources import DeltaPublicClient, HyperliquidPublicClient

DELTA_VENUE = "PAPER_DELTA"
HYPERLIQUID_VENUE = "PAPER_HYPERLIQUID"

HL_TAKER_BPS = Decimal("4.5")
HL_MAKER_BPS = Decimal("1.5")
HL_MIN_NOTIONAL = Decimal(10)
HL_MAX_DECIMALS = 6

HUNDRED = Decimal(100)
BPS = Decimal(10000)


@dataclass(frozen=True, slots=True)
class PairPlan:
    """One hedge pair to sync: a Delta perp and the Hyperliquid coin hedging it."""

    delta_symbol: str
    hyperliquid_coin: str
    objective: str
    tolerance_bps: str
    max_notional: str


DEFAULT_PAIRS: tuple[PairPlan, ...] = (
    PairPlan("BTCUSD", "BTC", "QUOTE_PNL_NEUTRAL", "25", "2000000"),
    PairPlan("ETHUSD", "ETH", "BASE_ASSET_NEUTRAL", "30", "1000000"),
    PairPlan("SOLUSD", "SOL", "BASE_ASSET_NEUTRAL", "50", "400000"),
    PairPlan("PAXGUSD", "PAXG", "NOTIONAL_NEUTRAL", "40", "300000"),
)


def _s(value: Decimal) -> str:
    return format(normalize(value), "f")


def spread_bps(bids: list[tuple[Decimal, Decimal]], asks: list[tuple[Decimal, Decimal]]) -> Decimal:
    if not bids or not asks:
        return Decimal(0)
    bid, ask = bids[0][0], asks[0][0]
    return (ask - bid) / ((ask + bid) / 2) * BPS


def hyperliquid_tick(price: Decimal, sz_decimals: int) -> Decimal:
    """Tick at ``price``: five significant figures, capped by the decimal limit.

    BTC near 82,000 ticks in whole dollars, ETH near 2,400 in dimes, SOL near
    104 in cents.  A price that crosses a power of ten changes its tick, which
    is why the spec records the level it was derived at.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    magnitude = price.adjusted()  # floor(log10(price))
    tick_exponent = max(magnitude - 4, -(HL_MAX_DECIMALS - sz_decimals))
    return Decimal(1).scaleb(min(tick_exponent, 0))


def delta_spec(product: dict[str, Any], book: dict[str, Any], ticker: dict[str, Any]) -> dict[str, Any]:
    symbol = product["symbol"]
    base = product["contract_unit_currency"]
    quote = product["quoting_asset"]["symbol"]
    settle = product["settling_asset"]["symbol"]
    tick = dec(product["tick_size"])
    initial = dec(product["initial_margin"]) / HUNDRED
    maintenance = dec(product["maintenance_margin"]) / HUNDRED
    interval_hours = Decimal(product["product_specs"]["rate_exchange_interval"]) / Decimal(3600)
    bids = [(dec(lvl["price"]), dec(lvl["size"])) for lvl in book.get("buy", [])]
    asks = [(dec(lvl["price"]), dec(lvl["size"])) for lvl in book.get("sell", [])]
    return {
        "symbol": symbol,
        "venue": DELTA_VENUE,
        "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL",
        "display_name": product.get("description") or product.get("short_description", symbol),
        "base_asset": base,
        "quote_asset": quote,
        "settlement_asset": settle,
        "underlying_key": base,
        "quantity_unit": "CONTRACT",
        "settlement_style": "LINEAR",
        "contract_size": _s(dec(product["contract_value"])),
        "contract_multiplier": "1",
        "tick_size": _s(tick),
        "min_quantity": "1",
        "max_quantity": str(product["position_size_limit"]),
        "quantity_step": "1",
        "price_precision": decimal_places(tick),
        "quantity_precision": 0,
        "max_leverage": _s(Decimal(1) / initial),
        "margin_model": "ISOLATED_LINEAR",
        "initial_margin_rate": _s(initial),
        "maintenance_margin_rate": _s(maintenance),
        # Delta publishes the scaling factors in percent per unit of notional.
        "margin_scaling_notional": _s(dec(product.get("max_leverage_notional") or 0)),
        "initial_margin_scaling": _s(dec(product.get("initial_margin_scaling_factor") or 0)
                                     / HUNDRED),
        "maintenance_margin_scaling": _s(
            dec(product.get("maintenance_margin_scaling_factor") or 0) / HUNDRED),
        "maker_fee_bps": _s(dec(product["maker_commission_rate"]) * BPS),
        "taker_fee_bps": _s(dec(product["taker_commission_rate"]) * BPS),
        "typical_spread_bps": _s(round(spread_bps(bids, asks), 3)),
        "funding_model": "PERPETUAL_FUNDING",
        "funding_interval_hours": _s(interval_hours),
        "baseline_funding_rate": _s(dec(ticker.get("funding_rate") or 0) / HUNDRED),
        "price_source": SOURCE_DELTA,
        "venue_instrument_id": str(product["id"]),
    }


def hyperliquid_spec(asset: dict[str, Any], ctx: dict[str, Any], book: dict[str, Any],
                     index: int | None = None) -> dict[str, Any]:
    coin = asset["name"]
    sz_decimals = int(asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    mark = dec(ctx["markPx"])
    tick = hyperliquid_tick(mark, sz_decimals)
    max_leverage = Decimal(asset["maxLeverage"])
    initial = Decimal(1) / max_leverage
    bids_raw, asks_raw = book["levels"]
    bids = [(dec(lvl["px"]), dec(lvl["sz"])) for lvl in bids_raw]
    asks = [(dec(lvl["px"]), dec(lvl["sz"])) for lvl in asks_raw]
    return {
        "symbol": coin,
        "venue": HYPERLIQUID_VENUE,
        "venue_kind": "PERPETUAL_EXCHANGE",
        "instrument_type": "PERPETUAL",
        "display_name": f"{coin} perpetual (USDC margined)",
        "base_asset": coin,
        "quote_asset": "USDC",
        "settlement_asset": "USDC",
        "underlying_key": coin,
        "quantity_unit": "BASE_UNIT",
        "settlement_style": "LINEAR",
        "contract_size": "1",
        "contract_multiplier": "1",
        "tick_size": _s(tick),
        "min_quantity": _s(step),
        "max_quantity": "1000000000",
        "quantity_step": _s(step),
        "min_notional": _s(HL_MIN_NOTIONAL),
        "price_precision": max(decimal_places(tick), 0),
        "quantity_precision": sz_decimals,
        "max_leverage": _s(max_leverage),
        "margin_model": "ISOLATED_LINEAR",
        "initial_margin_rate": _s(initial),
        "maintenance_margin_rate": _s(initial / 2),
        "maker_fee_bps": _s(HL_MAKER_BPS),
        "taker_fee_bps": _s(HL_TAKER_BPS),
        "typical_spread_bps": _s(round(spread_bps(bids, asks), 3)),
        "funding_model": "PERPETUAL_FUNDING",
        "funding_interval_hours": "1",
        "baseline_funding_rate": _s(dec(ctx["funding"])),
        "price_source": SOURCE_HYPERLIQUID,
        "venue_instrument_id": "" if index is None else str(index),
    }


async def fetch_catalogue(
    pairs: tuple[PairPlan, ...] = DEFAULT_PAIRS,
    *,
    delta: DeltaPublicClient | None = None,
    hyperliquid: HyperliquidPublicClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    delta = delta or DeltaPublicClient()
    hyperliquid = hyperliquid or HyperliquidPublicClient()
    try:
        delta_rows = await asyncio.gather(*(
            asyncio.gather(delta.product(p.delta_symbol), delta.orderbook(p.delta_symbol),
                           delta.ticker(p.delta_symbol))
            for p in pairs
        ))
        contexts = await hyperliquid.meta_and_contexts()
        hl_books = await asyncio.gather(*(hyperliquid.l2_book(p.hyperliquid_coin) for p in pairs))
    finally:
        await asyncio.gather(delta.close(), hyperliquid.close())

    delta_specs = [delta_spec(*row) for row in delta_rows]
    hl_specs = []
    for plan, book in zip(pairs, hl_books, strict=True):
        asset, ctx = contexts[plan.hyperliquid_coin]
        index = list(contexts).index(plan.hyperliquid_coin)   # position in meta.universe
        hl_specs.append(hyperliquid_spec(asset, ctx, book, index))
    return delta_specs, hl_specs


def mappings_for(pairs: tuple[PairPlan, ...] = DEFAULT_PAIRS) -> list[dict[str, Any]]:
    return [
        {
            "name": f"{p.hyperliquid_coin} Delta perp -> Hyperliquid perp",
            "source": f"{DELTA_VENUE}:{p.delta_symbol}",
            "hedge": f"{HYPERLIQUID_VENUE}:{p.hyperliquid_coin}",
            "objective": p.objective,
            "target_ratio": "1.0",
            "tolerance_bps": p.tolerance_bps,
            "max_notional": p.max_notional,
            "enabled": True,
        }
        for p in pairs
    ]


DELTA_HEADER = """\
# Delta Exchange (India) perpetuals -- GENERATED by `hedgelab instruments sync`
# from GET {base}/v2/products/<symbol>, /v2/l2orderbook and /v2/tickers
# at {at}.
#
# Every field comes from the venue except:
#   typical_spread_bps     measured from the live book at sync time
#   baseline_funding_rate  the live predicted rate at sync time (fraction per
#                          interval); only used if a ticker carries no rate
#   min_quantity / step    one contract -- Delta trades whole contracts
# Margin scales with size: above max_leverage_notional each extra dollar adds
# *_margin_scaling_factor percent to the rate (the formula ccxt implements
# from Delta's contract pages; Delta's own page renders it client-side).
"""

HYPERLIQUID_HEADER = """\
# Hyperliquid perpetuals -- GENERATED by `hedgelab instruments sync` from the
# public info endpoint (metaAndAssetCtxs, l2Book) at {at}.
#
# From the venue: size step (szDecimals), max leverage, funding.
# From the venue's documented rules:
#   tick_size        5 significant figures at the price seen at sync time --
#                    re-sync if the price crosses a power of ten
#   fees             base tier, 4.5 bps taker / 1.5 bps maker
#   maintenance      half the initial margin at max leverage
#   min_notional     orders under $10 are rejected
#   funding          hourly; baseline_funding_rate is the live rate at sync
# Measured: typical_spread_bps from the live book at sync time.
"""

MAPPINGS_HEADER = """\
# Hedge pairs: a Delta Exchange perpetual hedged with the Hyperliquid
# perpetual on the same underlying.  GENERATED by `hedgelab instruments sync`;
# adding a pair is a configuration change.
#
# The two venues size the same exposure differently -- Delta in contracts of
# 0.001 BTC / 0.01 ETH / 1 SOL / 0.001 PAXG, Hyperliquid in coins -- and fund
# on different clocks: Delta every 8h (4h for PAXG), Hyperliquid every hour.
"""


def write_catalogue(
    directory: Path,
    delta_specs: list[dict[str, Any]],
    hl_specs: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    delta_base: str = "https://api.india.delta.exchange",
    at: datetime | None = None,
) -> list[Path]:
    stamp = (at or datetime.now(UTC)).replace(microsecond=0).isoformat()
    outputs = [
        (directory / "delta_exchange.yaml", DELTA_HEADER.format(base=delta_base, at=stamp),
         {"instruments": delta_specs}),
        (directory / "hyperliquid.yaml", HYPERLIQUID_HEADER.format(at=stamp),
         {"instruments": hl_specs}),
        (directory / "mappings.yaml", MAPPINGS_HEADER, {"mappings": mappings}),
    ]
    written: list[Path] = []
    for path, header, body in outputs:
        text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)
        path.write_text(header + "\n" + text, encoding="utf-8")
        written.append(path)
    return written
