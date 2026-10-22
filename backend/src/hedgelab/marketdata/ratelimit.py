"""Client-side rate limiting and retry with backoff.

Both venues meter public traffic per IP.  Hyperliquid allows 1200
request-weight a minute and *weights* requests (an ``l2Book`` costs 2, a
``metaAndAssetCtxs`` 20); exceeding it returns HTTP 429, which this project
hit for real while a recording and the test suite polled at the same time.

Two mechanisms, both process-wide per host so that every client in the
process shares one budget:

* :class:`WeightBudget` -- a sliding one-minute window of request weight.  A
  request waits until it fits, so the process stays under the venue's limit
  instead of discovering it from a 429.
* :class:`RetryPolicy` -- when a venue still answers 429 or 5xx, or the
  connection drops, wait and retry with exponential backoff and jitter,
  honouring ``Retry-After``.  Other client errors are not retried: a 404 will
  not become a 200 by asking again.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

#: Statuses worth retrying: rate limited, or the venue is having a bad moment.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class WeightBudget:
    """At most ``per_minute`` weight in any trailing 60 seconds."""

    per_minute: int
    clock: Clock = time.monotonic
    sleep: Sleep = asyncio.sleep
    _spent: deque[tuple[float, int]] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _used(self, now: float) -> int:
        while self._spent and now - self._spent[0][0] >= 60.0:
            self._spent.popleft()
        return sum(weight for _, weight in self._spent)

    async def acquire(self, weight: int = 1) -> float:
        """Wait until ``weight`` fits, then spend it.  Returns seconds waited."""
        if weight > self.per_minute:
            raise ValueError(f"a request of weight {weight} can never fit {self.per_minute}/min")
        waited = 0.0
        async with self._lock:
            while True:
                now = self.clock()
                if self._used(now) + weight <= self.per_minute:
                    self._spent.append((now, weight))
                    return waited
                # The oldest spend expires first; wait exactly until it does.
                delay = max(60.0 - (now - self._spent[0][0]), 0.01)
                waited += delay
                await self.sleep(delay)

    def used(self) -> int:
        return self._used(self.clock())


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 20.0
    jitter: float = 0.25

    def delay(self, attempt: int, retry_after: float | None, rng: random.Random) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based)."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return backoff * (1 + rng.uniform(-self.jitter, self.jitter))


#: Conservative per-host budgets, below each venue's published limit so other
#: traffic from the same IP has room.
DEFAULT_BUDGETS: dict[str, int] = {
    "api.hyperliquid.xyz": 900,          # venue limit 1200 weight/min
    "api.india.delta.exchange": 1500,
    "api.delta.exchange": 1500,
    "api.kraken.com": 50,                # public endpoints, ~1/s
}

_budgets: dict[str, tuple[asyncio.AbstractEventLoop, WeightBudget]] = {}


def budget_for(host: str) -> WeightBudget | None:
    """The process-wide budget for ``host``, or ``None`` if it is unmetered.

    One budget per host per event loop: an ``asyncio.Lock`` belongs to the
    loop it is first used on, and a process normally runs exactly one.
    """
    if host not in DEFAULT_BUDGETS:
        return None
    loop = asyncio.get_running_loop()
    entry = _budgets.get(host)
    if entry is None or entry[0] is not loop:
        entry = _budgets[host] = (loop, WeightBudget(per_minute=DEFAULT_BUDGETS[host]))
    return entry[1]


def reset_budgets() -> None:
    _budgets.clear()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
