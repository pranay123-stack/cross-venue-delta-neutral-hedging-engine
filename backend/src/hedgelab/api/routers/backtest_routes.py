"""Backtests on real venue history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from ...instruments.registry import MappingNotFound
from ..deps import Service
from ..schemas import BacktestRequest
from ..security import RequireTrader

router = APIRouter(prefix="/backtest", tags=["analytics"])


@router.post(
    "/run",
    summary="Backtest a hedge pair on real hourly history",
    description=(
        "Replays real hourly candle closes of both legs and each venue's *settled* "
        "funding through the hedge objectives, rounding and cost model. History comes "
        "from the configured file, the history shipped with a replay recording, or -- "
        "on live data -- 30 days fetched from the venues. Exit costs are not charged. "
        "Results describe the past window only; they are not a forecast."
    ),
)
async def run_backtest(
    payload: BacktestRequest, service: Service, principal: RequireTrader
) -> dict[str, Any]:
    try:
        report = await service.run_backtest(
            payload.mapping_name, source_quantity=payload.source_quantity,
            target_ratio=payload.target_ratio, objective=payload.objective,
            steps=payload.steps, tolerance_bps=payload.tolerance_bps,
            book_impact=payload.book_impact,
        )
    except MappingNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    result = report.to_dict()
    if not payload.include_points:
        result.pop("points", None)
    result["summary"] = report.summary()
    return result
