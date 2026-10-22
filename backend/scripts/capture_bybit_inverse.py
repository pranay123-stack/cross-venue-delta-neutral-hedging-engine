"""Capture Bybit's BTCUSD inverse perpetual history as a test fixture.

Neither Delta Exchange nor Hyperliquid lists a live inverse perpetual, so the
inverse maths is checked against a real one: Bybit BTCUSD (1 contract = 1 USD,
settled in BTC, funding every 8h).  Public endpoints only, no key.

    python scripts/capture_bybit_inverse.py tests/data/history.json.gz \\
        tests/data/bybit_inverse_btcusd.json.gz tests/data/raw/bybit_instrument_BTCUSD.json

The window matches the existing history bundle so the two can be aligned.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from hedgelab.marketdata.history import HistoryBundle, InstrumentHistory
from hedgelab.marketdata.sources import Candle, FundingPoint

BASE = "https://api.bybit.com"
KEY = "BYBIT:BTCUSD"


def main(reference: Path, out: Path, instrument_out: Path) -> None:
    ref = HistoryBundle.load(reference)
    closes = ref["PAPER_HYPERLIQUID:BTC"].candles["1h"]
    start, end = closes[0].time, closes[-1].time
    ms = lambda at: int(at.timestamp() * 1000)  # noqa: E731
    with httpx.Client(timeout=20) as http:
        info = http.get(f"{BASE}/v5/market/instruments-info",
                        params={"category": "inverse", "symbol": "BTCUSD"}).json()
        instrument_out.write_text(json.dumps(info, indent=1) + "\n")
        rows = http.get(f"{BASE}/v5/market/kline", params={
            "category": "inverse", "symbol": "BTCUSD", "interval": "60",
            "start": ms(start), "end": ms(end), "limit": 1000}).json()["result"]["list"]
        funding = http.get(f"{BASE}/v5/market/funding/history", params={
            "category": "inverse", "symbol": "BTCUSD", "startTime": ms(start),
            "endTime": ms(end), "limit": 200}).json()["result"]["list"]
    history = InstrumentHistory(key=KEY)
    history.candles["1h"] = sorted(
        (Candle(time=datetime.fromtimestamp(int(r[0]) / 1000, UTC), open=Decimal(r[1]),
                high=Decimal(r[2]), low=Decimal(r[3]), close=Decimal(r[4]), volume=Decimal(r[5]))
         for r in rows),
        key=lambda c: c.time,
    )
    history.funding = sorted(
        (FundingPoint(time=datetime.fromtimestamp(int(f["fundingRateTimestamp"]) / 1000, UTC),
                      rate=Decimal(f["fundingRate"])) for f in funding),
        key=lambda p: p.time,
    )
    bundle = HistoryBundle(fetched_at=datetime.now(UTC), instruments={KEY: history})
    bundle.save(out)
    print(f"{len(history.candles['1h'])} hourly candles, {len(history.funding)} funding points "
          f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
