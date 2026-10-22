"""The market-data feed: real venue quotes plus explicit, labelled stress.

Every price this platform sees comes from a :class:`MarketFrame` -- one
synchronised poll of Delta Exchange and Hyperliquid, taken live or replayed
from a recording of a live session.  There is no price process here and no
fallback that invents a quote: an instrument with no real data raises
:class:`NoMarketData`.

Stress testing still needs prices the market has not printed: a 15% crash, a
spread blow-out, a frozen feed.  Those are *overlays* applied on top of the
real quote, each one switched on by an operator or a scenario, reported in
``active_scenarios()`` / ``active_overlays()``, and removed by
``clear_scenario()``.  A stressed ticker is still a real book, transformed in
a stated way -- never a synthetic one.

Two ways to feed it:

* **live** -- a poller calls :meth:`push` with each new frame; :meth:`advance`
  applies the newest one;
* **replay** -- construct with recorded frames; :meth:`advance` steps through
  them in order and stops at the last one.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from ..domain.enums import FundingModel, MarketScenario
from ..domain.instrument import InstrumentSpec
from ..domain.market import OrderBook, OrderBookLevel, Ticker
from ..domain.numeric import ZERO, dec, quantize
from ..logging_setup import get_logger
from .frames import InstrumentQuote, MarketFrame
from .scenarios import ScenarioProfile, profile_for

log = get_logger(__name__)

ONE = Decimal(1)
TWO = Decimal(2)


class NoMarketData(KeyError):
    """No real quote has been received for this instrument."""

    def __init__(self, key: str) -> None:
        super().__init__(f"no market data received for {key!r}")
        self.key = key


@dataclass
class _State:
    spec: InstrumentSpec
    quote: InstrumentQuote | None = None
    #: Last ticker served, kept so a frozen feed can replay it verbatim.
    last_ticker: Ticker | None = None
    sequence: int = 0
    #: Instrument missing from the most recent poll.
    missing: bool = False


class MarketFeed:
    """Serves tickers and books for every configured instrument."""

    def __init__(
        self,
        instruments: Iterable[InstrumentSpec],
        *,
        frames: Sequence[MarketFrame] = (),
        source: str = "live",
        stale_after_seconds: float = 15.0,
        seed: int = 20260908,
    ) -> None:
        self.source = source
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self._frames: list[MarketFrame] = list(frames)
        self._index = -1
        self._pending: MarketFrame | None = None
        self._clock: datetime | None = None
        self._fx: dict[tuple[str, str], Decimal] = {}
        self._states: dict[str, _State] = {}
        self._profiles: dict[str, ScenarioProfile] = {}
        self._global_profile = profile_for(MarketScenario.NORMAL)
        #: Multiplicative price shocks per underlying, e.g. 0.85 after -15%.
        self._shocks: dict[str, Decimal] = {}
        self._funding_overrides: dict[str, Decimal] = {}
        #: Mid per underlying when a volatility-scaling scenario was armed.
        self._vol_anchor: dict[str, Decimal] = {}
        self._rng = random.Random(seed)
        self.frames_applied = 0
        for spec in instruments:
            self.add_instrument(spec)
        if self._frames:
            self._step_replay()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def add_instrument(self, spec: InstrumentSpec) -> None:
        state = self._states.get(spec.key)
        if state is None:
            self._states[spec.key] = _State(spec=spec)
        else:
            state.spec = spec

    @property
    def instruments(self) -> list[InstrumentSpec]:
        return [s.spec for s in self._states.values()]

    @property
    def is_replay(self) -> bool:
        return bool(self._frames)

    @property
    def replay_position(self) -> tuple[int, int]:
        return self._index + 1, len(self._frames)

    @property
    def has_data(self) -> bool:
        return self._clock is not None

    # ------------------------------------------------------------------
    # frames
    # ------------------------------------------------------------------
    def push(self, frame: MarketFrame) -> None:
        """Queue a live frame; the next :meth:`advance` applies it."""
        self._pending = frame

    def advance(self, steps: int = 1) -> int:
        """Apply the next frame(s).  Returns how many frames were applied."""
        applied = 0
        for _ in range(max(0, steps)):
            if self._frames:
                if not self._step_replay():
                    break
            elif self._pending is not None:
                self._apply(self._pending)
                self._pending = None
            else:
                break
            applied += 1
            self._maybe_jump()
        return applied

    def _step_replay(self) -> bool:
        if self._index + 1 >= len(self._frames):
            return False
        self._index += 1
        self._apply(self._frames[self._index])
        return True

    def _apply(self, frame: MarketFrame) -> None:
        if self._global_profile.freeze_feed and not self._profiles:
            # A frozen feed receives nothing: the clock and quotes stay put,
            # which is what makes the staleness detectable.
            return
        for key, quote in frame.quotes.items():
            state = self._states.get(key)
            if state is None:
                continue
            if self.profile_for_venue(state.spec.venue).freeze_feed:
                continue
            state.quote = quote
            state.missing = False
        for key in frame.missing:
            if key in self._states:
                self._states[key].missing = True
        self._fx.update(frame.fx)
        self._clock = frame.timestamp
        self.frames_applied += 1

    @property
    def fx_rates(self) -> dict[tuple[str, str], Decimal]:
        return dict(self._fx)

    # ------------------------------------------------------------------
    # scenario overlays
    # ------------------------------------------------------------------
    def set_scenario(
        self, scenario: MarketScenario, venue: str | None = None, **overrides: object
    ) -> ScenarioProfile:
        """Apply a stress overlay globally, or to one venue only."""
        profile = profile_for(scenario)
        if overrides:
            profile = profile.with_overrides(**overrides)
        if venue is None:
            self._global_profile = profile
            self._profiles.clear()
        else:
            self._profiles[venue] = profile
        if profile.volatility_multiplier != ONE:
            self._vol_anchor = {u: self._raw_underlying_mid(u) for u in self._underlyings()}
        log.info("market stress overlay applied",
                 extra={"scenario": scenario.value, "venue": venue or "ALL"})
        return profile

    def clear_scenario(self, venue: str | None = None) -> None:
        """Remove overlays.  Clearing globally also removes shocks and
        funding overrides, returning every price to the real market."""
        if venue is None:
            self._global_profile = profile_for(MarketScenario.NORMAL)
            self._profiles.clear()
            self._shocks.clear()
            self._funding_overrides.clear()
            self._vol_anchor.clear()
        else:
            self._profiles.pop(venue, None)

    def profile_for_venue(self, venue: str) -> ScenarioProfile:
        return self._profiles.get(venue, self._global_profile)

    def active_scenarios(self) -> dict[str, str]:
        result = {"GLOBAL": self._global_profile.scenario.value}
        result.update({venue: p.scenario.value for venue, p in self._profiles.items()})
        return result

    def active_overlays(self) -> dict[str, object]:
        return {
            "scenarios": self.active_scenarios(),
            "price_shocks": {u: str(quantize(f - ONE, 6)) for u, f in self._shocks.items()},
            "funding_overrides": {k: str(v) for k, v in self._funding_overrides.items()},
        }

    def is_disconnected(self, venue: str) -> bool:
        return self.profile_for_venue(venue).disconnect

    def apply_shock(self, underlying: str, pct_move: Decimal) -> Decimal:
        """Move every instrument on ``underlying`` by ``pct_move``, returning
        the new average mid.  Stays applied until ``clear_scenario()``."""
        if underlying not in self._underlyings():
            raise KeyError(f"unknown underlying {underlying!r}")
        factor = self._shocks.get(underlying, ONE) * (ONE + dec(pct_move))
        if factor <= ZERO:
            raise ValueError(f"a {pct_move} move would take {underlying} to or below zero")
        self._shocks[underlying] = factor
        price = self.underlying_price(underlying)
        log.warning("price shock overlay applied",
                    extra={"underlying": underlying, "pct_move": str(pct_move),
                           "price": str(price)})
        return price

    def set_funding_rate(self, instrument_key: str, rate: Decimal) -> None:
        if instrument_key not in self._states:
            raise KeyError(f"unknown instrument {instrument_key!r}")
        self._funding_overrides[instrument_key] = dec(rate)

    def _maybe_jump(self) -> None:
        profile = self._global_profile
        if profile.jump_probability <= ZERO:
            return
        for underlying in self._underlyings():
            if dec(round(self._rng.random(), 10)) < profile.jump_probability:
                direction = ONE if self._rng.random() < 0.5 else -ONE
                self.apply_shock(underlying, direction * profile.jump_size)

    # ------------------------------------------------------------------
    # prices
    # ------------------------------------------------------------------
    def _underlyings(self) -> set[str]:
        return {s.spec.effective_underlying_key for s in self._states.values()}

    def _quote(self, key: str) -> tuple[_State, InstrumentQuote]:
        try:
            state = self._states[key]
        except KeyError:
            raise KeyError(f"unknown instrument {key!r}") from None
        if state.quote is None:
            raise NoMarketData(key)
        return state, state.quote

    @staticmethod
    def _raw_mid(quote: InstrumentQuote) -> Decimal:
        if quote.bids and quote.asks:
            return (quote.bids[0][0] + quote.asks[0][0]) / TWO
        return quote.mark

    def _raw_underlying_mid(self, underlying: str) -> Decimal:
        mids = [
            self._raw_mid(s.quote) for s in self._states.values()
            if s.quote is not None and s.spec.effective_underlying_key == underlying
        ]
        return sum(mids, ZERO) / Decimal(len(mids)) if mids else ZERO

    def _price_offset(self, spec: InstrumentSpec, raw_mid: Decimal) -> tuple[Decimal, Decimal]:
        """``(factor, additive)`` turning a real price into the stressed one."""
        underlying = spec.effective_underlying_key
        factor = self._shocks.get(underlying, ONE)
        additive = ZERO
        profile = self.profile_for_venue(spec.venue)
        anchor = self._vol_anchor.get(underlying)
        if profile.volatility_multiplier != ONE and anchor:
            # Amplify the real move since the scenario began.
            additive = (raw_mid - anchor) * (profile.volatility_multiplier - ONE)
        return factor, additive

    def _levels(
        self, spec: InstrumentSpec, quote: InstrumentQuote, depth: int
    ) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
        profile = self.profile_for_venue(spec.venue)
        raw_mid = self._raw_mid(quote)
        factor, additive = self._price_offset(spec, raw_mid)
        widen = profile.spread_multiplier
        if profile.min_spread_bps > ZERO and quote.bids and quote.asks and raw_mid > ZERO:
            real_bps = (quote.asks[0][0] - quote.bids[0][0]) / raw_mid * Decimal(10000)
            if real_bps > ZERO:
                widen = max(widen, profile.min_spread_bps / real_bps)
        tick = spec.tick_size

        def transform(price: Decimal, is_bid: bool) -> Decimal:
            # Spread widening scales each level's distance from the mid, so
            # the whole book fans out and depth is preserved.
            stressed = raw_mid + (price - raw_mid) * widen
            stressed = stressed * factor + additive
            rounding = ROUND_FLOOR if is_bid else ROUND_CEILING
            return (stressed / tick).quantize(ONE, rounding=rounding) * tick

        def size(raw: Decimal) -> Decimal:
            scaled = quantize(raw * profile.liquidity_multiplier, spec.quantity_precision)
            return scaled if scaled > ZERO else spec.quantity_step

        bids = [OrderBookLevel(transform(p, True), size(s)) for p, s in quote.bids[:depth]]
        asks = [OrderBookLevel(transform(p, False), size(s)) for p, s in quote.asks[:depth]]
        bids = [lvl for lvl in bids if lvl.price > ZERO]
        if bids and asks and asks[0].price <= bids[0].price:
            asks[0] = OrderBookLevel(bids[0].price + tick, asks[0].size)
        return bids, asks

    def _next_funding(self, spec: InstrumentSpec, at: datetime) -> datetime:
        interval = int(spec.funding_interval_hours * 3600)
        midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = int((at - midnight).total_seconds())
        return midnight + timedelta(seconds=(elapsed // interval + 1) * interval)

    def _build_ticker(self, state: _State, quote: InstrumentQuote) -> Ticker:
        spec = state.spec
        bids, asks = self._levels(spec, quote, 1)
        raw_mid = self._raw_mid(quote)
        factor, additive = self._price_offset(spec, raw_mid)
        bid = bids[0].price if bids else quote.mark
        ask = asks[0].price if asks else quote.mark
        funding: Decimal | None = None
        next_funding: datetime | None = None
        if spec.funding_model is FundingModel.PERPETUAL_FUNDING:
            funding = self._funding_overrides.get(spec.key, quote.funding_rate)
            next_funding = self._next_funding(spec, quote.venue_timestamp)
        clock = self._clock or quote.venue_timestamp
        stale = state.missing or (clock - quote.venue_timestamp) > self.stale_after
        state.sequence += 1
        return Ticker(
            venue=spec.venue,
            symbol=spec.symbol,
            bid=bid,
            ask=ask,
            last=quote.mark * factor + additive,
            volume=quote.volume_24h,
            timestamp=quote.venue_timestamp,
            funding_rate=funding,
            next_funding_time=next_funding,
            bid_size=bids[0].size if bids else ZERO,
            ask_size=asks[0].size if asks else ZERO,
            is_stale=stale or self.profile_for_venue(spec.venue).freeze_feed,
            sequence=state.sequence,
        )

    def ticker(self, instrument_key: str) -> Ticker:
        """Current top of book.  Under a frozen feed, the last ticker served
        is returned again with its original timestamp, flagged stale."""
        state, quote = self._quote(instrument_key)
        if self.profile_for_venue(state.spec.venue).freeze_feed and state.last_ticker is not None:
            last = state.last_ticker
            return Ticker(
                venue=last.venue, symbol=last.symbol, bid=last.bid, ask=last.ask,
                last=last.last, volume=last.volume, timestamp=last.timestamp,
                funding_rate=last.funding_rate, next_funding_time=last.next_funding_time,
                bid_size=last.bid_size, ask_size=last.ask_size, is_stale=True,
                sequence=last.sequence,
            )
        ticker = self._build_ticker(state, quote)
        state.last_ticker = ticker
        return ticker

    def tickers(self, venue: str | None = None) -> dict[str, Ticker]:
        result: dict[str, Ticker] = {}
        for key, state in self._states.items():
            if venue is not None and state.spec.venue != venue:
                continue
            if state.quote is None:
                continue
            result[key] = self.ticker(key)
        return result

    def orderbook(self, instrument_key: str, levels: int = 10) -> OrderBook:
        """The real book, with any active overlay applied."""
        state, quote = self._quote(instrument_key)
        bids, asks = self._levels(state.spec, quote, levels)
        return OrderBook(
            venue=state.spec.venue,
            symbol=state.spec.symbol,
            bids=tuple(bids),
            asks=tuple(asks),
            timestamp=quote.venue_timestamp,
            sequence=state.sequence,
        )

    def underlying_price(self, underlying: str) -> Decimal:
        """Average stressed mid across the venues quoting ``underlying``."""
        mids = [
            self.ticker(key).mid for key, s in self._states.items()
            if s.quote is not None and s.spec.effective_underlying_key == underlying
        ]
        if not mids:
            raise KeyError(f"no market data for underlying {underlying!r}")
        return sum(mids, ZERO) / Decimal(len(mids))

    @property
    def clock(self) -> datetime:
        if self._clock is None:
            raise NoMarketData("*")
        return self._clock

    def state_digest(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for underlying in sorted(self._underlyings()):
            try:
                result[underlying] = str(quantize(self.underlying_price(underlying), 8))
            except KeyError:
                continue
        return result
