"""Application settings.

Everything is environment-driven (12-factor).  There is deliberately **no
setting that holds an exchange API secret** -- the platform has no live order
path, so it has nothing to authenticate with.  See ``PAPER_TRADING.md``.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.enums import TradingMode

PACKAGE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parent.parent
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEDGELAB_",
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity -------------------------------------------------------
    app_name: str = "HedgeLab"
    environment: str = "local"
    version: str = "1.0.0"

    # --- safety ---------------------------------------------------------
    #: PAPER is the only mode with an implementation.  See ``venues/factory``.
    trading_mode: TradingMode = TradingMode.PAPER
    #: Must be explicitly true *and* a live adapter registered for LIVE to be
    #: constructible.  No live adapter exists in this repository.
    allow_live: bool = False
    require_confirmation_for_dangerous_actions: bool = True

    # --- storage --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://hedgelab:hedgelab@localhost:5468/hedgelab"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6479/0"
    #: When Redis is unreachable the event bus degrades to in-process only.
    redis_required: bool = False

    # --- market data ----------------------------------------------------
    #: ``live`` polls Delta Exchange and Hyperliquid's public endpoints;
    #: ``replay`` steps through a recording of a live session.  Both are real
    #: market data -- there is no synthetic source.
    market_data_source: str = "live"
    #: Seconds between live polls.  Hyperliquid allows 1200 request-weight a
    #: minute per IP; one poll of four coins costs 28, so 3s uses about half.
    market_poll_seconds: float = 3.0
    #: Recorded frames (``hedgelab market record``) used when source=replay.
    market_replay_path: Path | None = None
    #: Real candle history used to seed the statistics estimator at startup
    #: (``hedgelab market history``).  Live mode fetches it when unset.
    market_history_path: Path | None = None
    #: Book levels kept per side from each venue.
    market_depth: int = 20
    #: Seeds the fault injector's probabilistic draws -- not any price.
    random_seed: int = 20260908
    #: A quote older than this, relative to the newest poll, is stale.
    stale_data_threshold_seconds: float = 15.0
    #: Rolling-statistics estimator. Sampling coarsely is deliberate: sampling
    #: every tick and scaling to a day amplifies microstructure noise into a
    #: badly overstated volatility.
    stats_window: int = 500
    stats_sample_seconds: float = 300.0
    stats_min_samples: int = 30
    max_spread_bps_for_execution: Decimal = Decimal("50")

    # --- accounting -----------------------------------------------------
    account_currency: str = "USD"
    #: Starting paper balances, keyed by venue name.
    paper_starting_balance: Decimal = Decimal("250000")

    # --- risk defaults (overridable per hedge config) --------------------
    warning_margin_level: Decimal = Decimal("200")
    danger_margin_level: Decimal = Decimal("150")
    emergency_margin_level: Decimal = Decimal("120")
    kill_switch_margin_level: Decimal = Decimal("100")
    max_daily_loss: Decimal = Decimal("25000")
    max_portfolio_notional: Decimal = Decimal("5000000")
    max_residual_exposure_bps: Decimal = Decimal("50")
    max_concentration_pct: Decimal = Decimal("60")

    # --- hard limits (venues/guard.py) -----------------------------------
    #: Enforced in front of every venue, below the strategy, in the account
    #: currency.  Orders that only reduce a position are exempt from the size
    #: and loss limits so de-risking always works.  The per-venue loss limit is
    #: a last-resort backstop: on a cross-venue hedge one leg loses whenever the
    #: other gains, so it must sit well above a normal one-leg move -- portfolio
    #: loss control is the risk engine's ``max_daily_loss``.
    hard_max_order_notional: Decimal = Decimal("3500000")
    hard_max_position_notional: Decimal = Decimal("5000000")
    hard_max_daily_loss_per_venue: Decimal = Decimal("100000")
    hard_max_orders_per_minute: int = 60
    #: Build and check the venue-native payload of every order (never sent).
    dry_run_payloads: bool = True

    # --- execution ------------------------------------------------------
    default_rebalance_tolerance_bps: Decimal = Decimal("25")
    execution_timeout_seconds: float = 10.0
    max_execution_retries: int = 2

    # --- api ------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:4173"])
    log_level: str = "INFO"
    log_json: bool = True

    #: Directory of YAML instrument specifications.
    instrument_spec_dir: Path = PACKAGE_ROOT / "instruments" / "specs"

    @field_validator("market_data_source")
    @classmethod
    def _real_sources_only(cls, value: str) -> str:
        if value not in ("live", "replay"):
            raise ValueError("market_data_source must be 'live' or 'replay'")
        return value

    @field_validator("trading_mode", mode="after")
    @classmethod
    def _paper_only(cls, value: TradingMode) -> TradingMode:
        return value

    @property
    def is_paper(self) -> bool:
        return self.trading_mode is TradingMode.PAPER

    @property
    def sync_database_url(self) -> str:
        """Alembic needs a synchronous driver."""
        return (
            self.database_url.replace("+asyncpg", "+psycopg2")
            .replace("+aiosqlite", "")
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch the environment."""
    get_settings.cache_clear()
