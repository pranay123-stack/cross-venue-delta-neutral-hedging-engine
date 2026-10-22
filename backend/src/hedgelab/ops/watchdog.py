"""An independent kill switch: a separate process that flattens the book when
the trading process stops answering.

The kill switch inside the API is only as alive as the API.  If that process
crashes, hangs or loses its event loop, nothing in it can close positions.
The watchdog shares no memory with it.  It reads the heartbeat the API writes
to the database every poll; when the heartbeat is older than the timeout --
or an operator asks it to (``hedgelab watchdog --kill-now``) -- it builds its
*own* connections to the venues, cancels open orders, flattens every
position, verifies the book is flat, and leaves the kill switch engaged and
persisted, so the trading process refuses to trade when it comes back.

With paper venues the book of record is the database (the venues rehydrate
from it), so the watchdog flattens the persisted positions.  With live venues
it would flatten at the venue itself, through its own adapters and keys --
which is the point of being a separate process.  One limitation of the paper
setup: a *hung* (rather than crashed) API still holds its own in-memory paper
positions, which the watchdog cannot reach.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from ..config import Settings
from ..db.repositories import ControlStateRepository, PositionRepository
from ..db.session import Database
from ..domain.market import utcnow
from ..logging_setup import get_logger
from ..service import KILL_SWITCH_KEY, HedgeLabService

log = get_logger(__name__)

HEARTBEAT_KEY = "heartbeat:api"
WATCHDOG_KEY = "heartbeat:watchdog"


class Verdict(StrEnum):
    HEALTHY = "HEALTHY"
    #: No heartbeat has ever been written and nothing is open: nothing to guard.
    NOT_STARTED = "NOT_STARTED"
    #: Already killed and flat: nothing left to do.
    KILLED_AND_FLAT = "KILLED_AND_FLAT"
    TRIGGERED = "TRIGGERED"


@dataclass
class WatchdogCheck:
    verdict: Verdict
    heartbeat_age: float | None
    open_positions: int
    detail: str = ""
    kill_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value, "heartbeat_age_s": self.heartbeat_age,
                "open_positions": self.open_positions, "detail": self.detail,
                "kill_result": self.kill_result}


ServiceFactory = Callable[[Settings, Database], HedgeLabService]


class Watchdog:
    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] = utcnow,
        service_factory: ServiceFactory | None = None,
    ) -> None:
        self.settings = settings
        self.database = database or Database.from_settings(settings)
        self.timeout = timedelta(seconds=timeout_seconds)
        self.clock = clock
        self.service_factory = service_factory or (
            lambda s, db: HedgeLabService(s, database=db))

    async def _state(self) -> tuple[float | None, bool, int]:
        async with self.database.session() as session:
            control = ControlStateRepository(session)
            beat = await control.get(HEARTBEAT_KEY)
            kill = await control.get(KILL_SWITCH_KEY)
            positions = await PositionRepository(session).to_domain()
        age = None
        if beat is not None:
            age = (self.clock() - datetime.fromisoformat(beat.value["at"])).total_seconds()
        engaged = bool(kill and kill.value.get("engaged"))
        open_count = sum(1 for p in positions if not p.is_flat)
        return age, engaged, open_count

    async def check_once(self, *, force: bool = False) -> WatchdogCheck:
        """Decide, and act if the trading process looks dead (or ``force``)."""
        age, engaged, open_count = await self._state()
        if not force:
            if age is None:
                # Never seen alive is not the same as dead: a backend that has
                # not written its first beat yet, or a one-off CLI run, must not
                # be flattened.  Only a heartbeat that *stopped* triggers; an
                # operator can still act at any time with --kill-now.
                return WatchdogCheck(Verdict.NOT_STARTED, None, open_count, "no heartbeat yet")
            if age is not None and age <= self.timeout.total_seconds():
                return WatchdogCheck(Verdict.HEALTHY, age, open_count)
            if engaged and open_count == 0:
                return WatchdogCheck(Verdict.KILLED_AND_FLAT, age, 0,
                                     "kill switch already engaged and the book is flat")
        reason = ("operator requested an independent kill" if force
                  else f"no heartbeat for {age:.0f}s")
        result = await self._kill(f"watchdog: {reason}")
        log.error("watchdog engaged the kill switch", extra={"reason": reason, **result})
        return WatchdogCheck(Verdict.TRIGGERED, age, open_count, reason, result)

    async def _kill(self, reason: str) -> dict[str, Any]:
        service = self.service_factory(self.settings, self.database)
        try:
            await service.startup(seed_reference_data=False, rehydrate=True, seed_stats=False)
            if service.market.source == "live":
                await service.refresh_market()
            return await service.engage_kill_switch(reason, actor="watchdog")
        finally:
            if service._collector is not None:
                await service._collector.close()

    async def _beat(self, check: WatchdogCheck) -> None:
        """The watchdog's own proof of life, shown in the API's status."""
        async with self.database.session() as session:
            await ControlStateRepository(session).set(WATCHDOG_KEY, {
                "at": self.clock().isoformat(), "verdict": check.verdict.value,
                "timeout_s": self.timeout.total_seconds(),
            })

    async def run(
        self,
        *,
        interval_seconds: float = 5.0,
        stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        log.info("watchdog started", extra={"timeout_s": self.timeout.total_seconds(),
                                            "interval_s": interval_seconds})
        while not stop():
            try:
                check = await self.check_once()
                await self._beat(check)
                if check.verdict is not Verdict.HEALTHY:
                    log.warning("watchdog check", extra=check.to_dict())
            except Exception as exc:  # the watchdog itself must not die quietly
                log.exception("watchdog check failed", extra={"error": str(exc)})
            await sleep(interval_seconds)
