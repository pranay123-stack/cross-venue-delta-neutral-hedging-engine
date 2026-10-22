"""JSON responses with readable decimals.

Money and quantities travel as decimal *strings* (see ``schemas.py``).  Python
renders very small or zero decimals in scientific notation -- ``0E-8``,
``2.5E-8`` -- which is exact but unreadable in an API reference or a client.
This response class rewrites exactly those strings as plain decimals; nothing
else is touched.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi.responses import JSONResponse

_SCIENTIFIC = re.compile(r"^-?\d+(\.\d+)?E[-+]\d+$")


def plain_decimals(value: Any) -> Any:
    if isinstance(value, str) and _SCIENTIFIC.match(value):
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        if number == 0:
            return "0"
        return format(number.normalize(), "f")
    if isinstance(value, dict):
        return {k: plain_decimals(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain_decimals(v) for v in value]
    return value


class PlainDecimalJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(plain_decimals(content))
