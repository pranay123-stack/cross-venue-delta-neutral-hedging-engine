"""Record a real request and response for every API operation.

Runs :data:`hedgelab.api.example_calls.CALLS` against the real application on
the committed recording of a live Delta Exchange + Hyperliquid session and
writes ``src/hedgelab/api/openapi_examples.json``, which the app injects into
its OpenAPI document (ReDoc at ``/redoc``).

    python scripts/capture_api_examples.py

Re-run it after changing an endpoint; ``tests/integration/test_api_contract.py``
fails while the stored examples and the live responses disagree.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

from hedgelab.api.app import create_app
from hedgelab.api.example_calls import run_calls, trim
from hedgelab.api.responses import plain_decimals
from hedgelab.config import Settings

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "tests" / "data"
OUT = BACKEND / "src" / "hedgelab" / "api" / "openapi_examples.json"


def settings(workdir: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{workdir / 'examples.db'}",
        environment="test",
        market_data_source="replay",
        market_replay_path=DATA / "live_frames.jsonl.gz",
        market_history_path=DATA / "history.json.gz",
        market_poll_seconds=3600,           # keep the replay still while recording
        random_seed=20260908,
        log_level="ERROR",
        log_json=False,
    )


async def capture() -> dict[str, dict[str, object]]:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(settings(Path(tmp)))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                              base_url="http://hedgelab") as client,
        ):
            exchanges = await run_calls(client)

    examples: dict[str, dict[str, object]] = {}
    failures = []
    for ex in exchanges:
        if ex.status != ex.call.expect:
            failures.append(f"{ex.call.operation} ({ex.call.title}): expected "
                            f"{ex.call.expect}, got {ex.status}: {str(ex.response)[:300]}")
        entry = examples.setdefault(ex.call.operation,
                                    {"requests": {}, "error_requests": [], "responses": {}})
        if ex.call.body is not None:
            entry["requests"].setdefault(ex.call.title, ex.call.body)  # type: ignore[union-attr]
            if ex.call.expect >= 400:
                entry["error_requests"].append(ex.call.title)  # type: ignore[union-attr]
        entry["responses"].setdefault(str(ex.status), {  # type: ignore[union-attr]
            "title": ex.call.title,
            "request": {"path": ex.path, "params": ex.call.params, "body": ex.call.body,
                        "headers": ex.call.headers or None},
            "body": trim(plain_decimals(ex.response)),
        })
    if failures:
        print("\n".join(failures), file=sys.stderr)
        raise SystemExit(f"{len(failures)} call(s) did not return their documented status")
    return examples


def main() -> None:
    examples = asyncio.run(capture())
    OUT.write_text(json.dumps(examples, indent=1, sort_keys=True, default=str) + "\n")
    print(f"{sum(len(e['responses']) for e in examples.values())} responses for "
          f"{len(examples)} operations written to {OUT.relative_to(BACKEND)}")


if __name__ == "__main__":
    main()
