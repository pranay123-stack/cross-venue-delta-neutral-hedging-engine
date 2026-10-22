"""The OpenAPI document, with a real example for every operation.

FastAPI generates the schema from the routes; this adds what a reader needs to
understand the API without running it: for each operation, a real request and
the real responses -- success *and* the errors it can return -- recorded from
the running application on the committed Delta/Hyperliquid recording
(``scripts/capture_api_examples.py``).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

EXAMPLES = Path(__file__).with_name("openapi_examples.json")

#: What each status means for this API, shown beside the recorded example.
STATUS_MEANING = {
    "200": "Success.",
    "401": "Missing or invalid API key (only when HEDGELAB_API_KEYS is set).",
    "403": "The API key's role may not perform this action.",
    "404": "The instrument, pair, cycle or scenario does not exist.",
    "409": "Refused in the current state: the kill switch is on, trading is paused, "
           "a hard limit or the risk gate said no, or the resource is still in use.",
    "422": "The request is invalid: a field is missing, out of range or not a known value.",
    "428": "An irreversible action: resend with the header `X-Confirm-Action: CONFIRM`.",
    "503": "A venue or its market data is unavailable: disconnected, stale, or no real "
           "quote received yet. Never answered with an invented price.",
}


@cache
def _examples() -> dict[str, Any]:
    if not EXAMPLES.exists():
        return {}
    loaded: dict[str, Any] = json.loads(EXAMPLES.read_text())
    return loaded


def _key(title: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in title.lower()).strip("_")


def build_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description,
        routes=app.routes, tags=app.openapi_tags,
    )
    examples = _examples()
    for route, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            recorded = examples.get(f"{method.upper()} {route}")
            if not recorded:
                continue
            body = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if body is not None and recorded["requests"]:
                # Valid requests first, so the dropdown opens on one that works.
                ordered = sorted(recorded["requests"].items(),
                                 key=lambda item: item[0] in recorded.get("error_requests", []))
                body["examples"] = {
                    _key(title): {"summary": title, "value": value} for title, value in ordered
                }
            responses = operation.setdefault("responses", {})
            for status, sample in recorded["responses"].items():
                response = responses.setdefault(status, {})
                meaning = STATUS_MEANING.get(status, "")
                response["description"] = f"{meaning} Example: {sample['title']}.".strip()
                content = response.setdefault("content", {}).setdefault("application/json", {})
                content["examples"] = {
                    _key(sample["title"]): {"summary": sample["title"], "value": sample["body"]}
                }
                content.pop("example", None)
    app.openapi_schema = schema
    return schema
