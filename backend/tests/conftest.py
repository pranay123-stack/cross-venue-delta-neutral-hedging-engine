"""Shared fixtures.

Tests run against SQLite so the whole suite works with no Docker and no
PostgreSQL.  The schema is identical -- see ``db/base.py`` for the dialect
variants that make that true.

Market data is **real**: ``tests/data/live_frames.jsonl.gz`` is a recording
of a live session against Delta Exchange (India) and Hyperliquid, and
``tests/data/history.json.gz`` holds their real candles and settled funding.
Both were produced by ``hedgelab market record`` / ``hedgelab market history``
and are replayed offline, so the suite needs no network and is reproducible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from hedgelab.config import Settings, reset_settings_cache
from hedgelab.db.session import Database
from hedgelab.domain.enums import (
    FundingModel,
    InstrumentType,
    MarginModel,
    QuantityUnit,
    SettlementStyle,
    VenueKind,
)
from hedgelab.domain.instrument import InstrumentSpec
from hedgelab.faults.injector import FaultInjector
from hedgelab.hedge.calculator import HedgeCalculator
from hedgelab.instruments.registry import InstrumentRegistry
from hedgelab.marketdata.feed import MarketFeed
from hedgelab.marketdata.frames import MarketFrame, load_frames
from hedgelab.marketdata.fx import FxService
from hedgelab.marketdata.history import HistoryBundle
from hedgelab.service import HedgeLabService

SPEC_DIR = Path(__file__).resolve().parents[1] / "src" / "hedgelab" / "instruments" / "specs"
DATA_DIR = Path(__file__).resolve().parent / "data"
LIVE_FRAMES = DATA_DIR / "live_frames.jsonl.gz"
HISTORY = DATA_DIR / "history.json.gz"


@pytest.fixture(scope="session")
def frames() -> list[MarketFrame]:
    """The recorded live session, loaded once."""
    return load_frames(LIVE_FRAMES)


@pytest.fixture(scope="session")
def history() -> HistoryBundle:
    return HistoryBundle.load(HISTORY)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test reaches a real venue.

    The suite replays committed recordings; a test that silently fell back
    to live polling would be non-reproducible and would spend the venues'
    per-IP rate budget.  In-process ASGI transports are unaffected.
    """

    async def refuse(self: object, request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"network access is disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def registry() -> InstrumentRegistry:
    return InstrumentRegistry.from_directory(SPEC_DIR)


@pytest.fixture
def fx(frames: list[MarketFrame]) -> FxService:
    """FX service holding the stablecoin rates measured in the recording."""
    service = FxService()
    for (base, quote), rate in frames[0].fx.items():
        service.set_rate(base, quote, rate, source="kraken")
    return service


@pytest.fixture
def market(registry: InstrumentRegistry, frames: list[MarketFrame]) -> MarketFeed:
    feed = MarketFeed(registry.all(), frames=frames, source="replay")
    feed.advance(20)
    return feed


@pytest.fixture
def calculator(fx: FxService) -> HedgeCalculator:
    return HedgeCalculator(fx)


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector(seed=1)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        instrument_spec_dir=SPEC_DIR,
        market_data_source="replay",
        market_replay_path=LIVE_FRAMES,
        market_history_path=HISTORY,
        random_seed=424242,
        account_currency="USD",
        paper_starting_balance=Decimal("250000"),
    )


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database.from_settings(settings)
    await db.create_all()
    yield db
    await db.dispose()


@pytest_asyncio.fixture
async def service(settings: Settings, database: Database) -> AsyncIterator[HedgeLabService]:
    svc = HedgeLabService(settings, database=database)
    await svc.startup(create_schema=False, seed_reference_data=True, rehydrate=False)
    svc.advance_market(20)
    yield svc
    # The ``database`` fixture owns disposal; disposing twice would error.


# ----------------------------------------------------------------------
# Instrument *specifications* deliberately unlike anything in the catalogue,
# used to prove the engine is not tuned to the shipped symbols.  These are
# configuration, not market data: no test prices them from an invented feed.
# ----------------------------------------------------------------------
def make_perp(
    symbol: str = "TEST-PERP",
    *,
    contract_size: str = "1",
    quantity_step: str = "1",
    min_quantity: str = "1",
    tick_size: str = "0.01",
    inverse: bool = False,
    quote: str = "USDT",
    settlement: str | None = None,
    base: str = "TST",
    underlying: str = "",
    taker_fee_bps: str = "5",
    spread_bps: str = "2",
    funding_rate: str = "0.0001",
    initial_margin_rate: str = "0.02",
    maintenance_margin_rate: str = "0.005",
    venue: str = "PAPER_DELTA",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, venue=venue, venue_kind=VenueKind.PERPETUAL_EXCHANGE,
        instrument_type=InstrumentType.PERPETUAL,
        base_asset=base, quote_asset=quote,
        settlement_asset=settlement or (base if inverse else quote),
        underlying_key=underlying or base,
        quantity_unit=QuantityUnit.CONTRACT,
        settlement_style=SettlementStyle.INVERSE if inverse else SettlementStyle.LINEAR,
        contract_size=Decimal(contract_size), contract_multiplier=Decimal(1),
        tick_size=Decimal(tick_size), min_quantity=Decimal(min_quantity),
        max_quantity=Decimal("100000000"), quantity_step=Decimal(quantity_step),
        price_precision=8, quantity_precision=8,
        max_leverage=Decimal(50),
        initial_margin_rate=Decimal(initial_margin_rate),
        maintenance_margin_rate=Decimal(maintenance_margin_rate),
        maker_fee_bps=Decimal(2), taker_fee_bps=Decimal(taker_fee_bps),
        typical_spread_bps=Decimal(spread_bps),
        funding_model=FundingModel.PERPETUAL_FUNDING,
        funding_interval_hours=Decimal(8),
        baseline_funding_rate=Decimal(funding_rate),
    )


def make_cfd(
    symbol: str = "TESTUSD",
    *,
    units_per_lot: str = "1",
    quantity_step: str = "0.01",
    min_quantity: str = "0.01",
    tick_size: str = "0.01",
    quote: str = "USD",
    base: str = "TST",
    underlying: str = "",
    spread_bps: str = "8",
    swap_long: str = "-100",
    swap_short: str = "-40",
    initial_margin_rate: str = "0.01",
    maintenance_margin_rate: str = "0.005",
    venue: str = "PAPER_BROKER",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol, venue=venue, venue_kind=VenueKind.MT5_BROKER,
        instrument_type=InstrumentType.CFD,
        base_asset=base, quote_asset=quote, settlement_asset=quote,
        underlying_key=underlying or base,
        quantity_unit=QuantityUnit.LOT, settlement_style=SettlementStyle.LINEAR,
        contract_size=Decimal(units_per_lot), units_per_lot=Decimal(units_per_lot),
        contract_multiplier=Decimal(1),
        tick_size=Decimal(tick_size), min_quantity=Decimal(min_quantity),
        max_quantity=Decimal("1000000"), quantity_step=Decimal(quantity_step),
        price_precision=8, quantity_precision=8,
        max_leverage=Decimal(100),
        initial_margin_rate=Decimal(initial_margin_rate),
        maintenance_margin_rate=Decimal(maintenance_margin_rate),
        maker_fee_bps=Decimal(0), taker_fee_bps=Decimal(0),
        typical_spread_bps=Decimal(spread_bps),
        funding_model=FundingModel.SWAP_POINTS,
        swap_long_points=Decimal(swap_long), swap_short_points=Decimal(swap_short),
        margin_model=MarginModel.BROKER_LEVERAGE,
    )


def approx_dec(actual: Decimal, expected: str | Decimal, tol: str = "1e-9") -> bool:
    """Decimal-safe closeness check.

    ``pytest.approx`` coerces to float and then refuses to subtract a Decimal,
    so comparisons here stay in Decimal throughout.
    """
    return abs(Decimal(actual) - Decimal(expected)) <= Decimal(tol)
