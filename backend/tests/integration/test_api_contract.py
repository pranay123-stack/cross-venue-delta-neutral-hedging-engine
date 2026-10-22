"""The API reference cannot drift from the API.

Every operation in the OpenAPI document must carry a real example, and every
example must still be true: the recorded walk through the API
(:mod:`hedgelab.api.example_calls`) is replayed against a fresh application,
and each response must come back with the documented status and the documented
shape.  When an endpoint changes, re-record with
``python scripts/capture_api_examples.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.conftest import HISTORY, LIVE_FRAMES

from hedgelab.api.app import create_app
from hedgelab.api.example_calls import CALLS, Exchange, run_calls
from hedgelab.api.openapi import EXAMPLES
from hedgelab.config import Settings

RECORDED: dict[str, Any] = json.loads(EXAMPLES.read_text())


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def exchanges(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[list[Exchange]]:
    workdir: Path = tmp_path_factory.mktemp("contract")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{workdir / 'contract.db'}",
        environment="test", market_data_source="replay",
        market_replay_path=LIVE_FRAMES, market_history_path=HISTORY,
        market_poll_seconds=3600, random_seed=20260908,
        paper_starting_balance=Decimal("250000"),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://hedgelab") as client,
    ):
        yield await run_calls(client)


def shape(value: Any) -> Any:
    """Top-level shape: the keys of an object, or 'list' / the type."""
    if isinstance(value, dict):
        return sorted(value)
    return type(value).__name__


def test_every_operation_is_documented_with_a_real_example() -> None:
    schema = create_app().openapi()
    undocumented = []
    for route, operations in schema["paths"].items():
        for method, operation in operations.items():
            examples = [
                r["content"]["application/json"].get("examples")
                for r in operation["responses"].values()
                if "application/json" in r.get("content", {})
            ]
            if not any(examples):
                undocumented.append(f"{method.upper()} {route}")
            if "requestBody" in operation:
                body = operation["requestBody"]["content"]["application/json"]
                if not body.get("examples"):
                    undocumented.append(f"{method.upper()} {route} (request)")
    assert undocumented == []


def test_every_operation_has_a_success_example() -> None:
    schema = create_app().openapi()
    operations = {f"{m.upper()} {p}" for p, ops in schema["paths"].items() for m in ops}
    without_success = sorted(op for op in operations
                             if "200" not in RECORDED.get(op, {}).get("responses", {}))
    assert without_success == []


def test_the_walk_covers_every_operation() -> None:
    schema = create_app().openapi()
    operations = {f"{m.upper()} {p}" for p, ops in schema["paths"].items() for m in ops}
    assert operations == {call.operation for call in CALLS}


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("index", range(len(CALLS)), ids=[c.title for c in CALLS])
async def test_each_documented_call_still_behaves_as_documented(
    exchanges: list[Exchange], index: int,
) -> None:
    ex = exchanges[index]
    assert ex.status == ex.call.expect, f"{ex.call.operation}: {str(ex.response)[:300]}"
    # The document holds one example per status; compare shapes with the call
    # that produced it.
    documented = RECORDED[ex.call.operation]["responses"][str(ex.status)]
    if documented["title"] == ex.call.title:
        assert shape(ex.response) == shape(documented["body"])


@pytest.mark.asyncio(loop_scope="module")
async def test_errors_explain_themselves(exchanges: list[Exchange]) -> None:
    """An error response must say what went wrong, not just fail."""
    for ex in exchanges:
        if ex.status >= 400:
            assert isinstance(ex.response, dict) and ex.response.get("detail"), ex.call.title


def test_swagger_is_the_one_explorer() -> None:
    app = create_app()
    assert app.docs_url == "/docs"
    assert app.redoc_url is None


@pytest.mark.parametrize("raw,shown", [
    ("0E-8", "0"), ("-0E-8", "0"), ("2.5E-8", "0.000000025"), ("1.25E-8", "0.0000000125"),
    ("1.5E+3", "1500"), ("81673.25", "81673.25"), ("PAPER_DELTA:BTCUSD", "PAPER_DELTA:BTCUSD"),
    ("E-8", "E-8"), ("3E", "3E"),
])
def test_scientific_decimals_are_rendered_plainly(raw: str, shown: str) -> None:
    from hedgelab.api.responses import plain_decimals

    assert plain_decimals(raw) == shown
    assert plain_decimals({"a": [raw]}) == {"a": [shown]}


@pytest.mark.asyncio(loop_scope="module")
async def test_no_response_shows_scientific_notation(exchanges: list[Exchange]) -> None:
    import re

    scientific = re.compile(r'"-?\d+(\.\d+)?E[-+]\d+"')
    offenders = [ex.call.title for ex in exchanges
                 if scientific.search(json.dumps(ex.response))]
    assert offenders == []
