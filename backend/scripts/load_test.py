"""Load test: concurrent dashboard readers, hedge calculators and WebSocket
subscribers against a running API.

    python scripts/load_test.py --base http://localhost:8012 --clients 50 --seconds 30

Run it against a replay server (``HEDGELAB_MARKET_DATA_SOURCE=replay``) so the
load lands on this process and not on the venues.  It reports throughput,
latency percentiles and errors per endpoint, and how many price frames each
WebSocket subscriber received -- a subscriber that falls behind is dropped by
the hub's bounded queue, and that shows up here as missing frames.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
import websockets

READS = [
    "/api/market-data",
    "/api/system/status",
    "/api/instrument-mappings",
    "/api/positions",
    "/api/portfolio",
    "/api/hedge/cycles",
]
CALCULATE = {
    "source_key": "PAPER_DELTA:BTCUSD", "hedge_key": "PAPER_HYPERLIQUID:BTC",
    "source_quantity": "5000", "objective": "QUOTE_PNL_NEUTRAL", "use_live_positions": False,
}


@dataclass
class Results:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    ws_frames: list[int] = field(default_factory=list)


async def http_client(base: str, deadline: float, results: Results, rng: random.Random) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        while time.monotonic() < deadline:
            if rng.random() < 0.2:
                name, call = "POST /api/hedge/calculate", http.post("/api/hedge/calculate",
                                                                   json=CALCULATE)
            else:
                path = rng.choice(READS)
                name, call = f"GET {path}", http.get(path)
            started = time.perf_counter()
            try:
                response = await call
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code >= 400:
                    results.errors[name].append(str(response.status_code))
                else:
                    results.latencies[name].append(elapsed)
            except httpx.HTTPError as exc:
                results.errors[name].append(type(exc).__name__)


async def ws_client(base: str, deadline: float, results: Results) -> None:
    url = base.replace("http", "ws", 1) + "/ws?topics=prices"
    frames = 0
    try:
        async with websockets.connect(url) as socket:
            while time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(socket.recv(), deadline - time.monotonic())
                except TimeoutError:
                    break
                if json.loads(message).get("topic") == "prices":
                    frames += 1
    except (OSError, websockets.WebSocketException) as exc:
        results.errors["WS /ws"].append(type(exc).__name__)
    results.ws_frames.append(frames)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def main(base: str, clients: int, sockets: int, seconds: float) -> int:
    results = Results()
    deadline = time.monotonic() + seconds
    rng = random.Random(7)
    started = time.monotonic()
    await asyncio.gather(
        *(http_client(base, deadline, results, random.Random(rng.random())) for _ in range(clients)),
        *(ws_client(base, deadline, results) for _ in range(sockets)),
    )
    wall = time.monotonic() - started

    total = sum(len(v) for v in results.latencies.values())
    failed = sum(len(v) for v in results.errors.values())
    print(f"{clients} HTTP clients + {sockets} WebSocket subscribers for {wall:.1f}s")
    print(f"requests: {total} ok, {failed} failed, {total / wall:.0f} req/s\n")
    print(f"{'endpoint':38} {'n':>6} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'errors':>7}")
    for name in sorted(set(results.latencies) | set(results.errors)):
        lat = results.latencies.get(name, [])
        errs = len(results.errors.get(name, []))
        if lat:
            print(f"{name:38} {len(lat):>6} {statistics.median(lat):>8.1f} "
                  f"{pct(lat, 0.95):>8.1f} {pct(lat, 0.99):>8.1f} {errs:>7}")
        else:
            print(f"{name:38} {0:>6} {'-':>8} {'-':>8} {'-':>8} {errs:>7}")
    if results.ws_frames:
        print(f"\nWebSocket price frames per subscriber: min {min(results.ws_frames)}, "
              f"median {statistics.median(results.ws_frames):.0f}, max {max(results.ws_frames)}")
    for name, errs in results.errors.items():
        print(f"errors {name}: {sorted(set(errs))}")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="http://localhost:8012")
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--sockets", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.base, args.clients, args.sockets, args.seconds)))
