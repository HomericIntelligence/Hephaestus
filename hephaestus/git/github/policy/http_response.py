"""Parse ``gh api --include`` output without trusting the payload.

A response body arrives from a network boundary, so the parser fails closed:
a malformed payload returns no parsed body, and the caller keeps the status
code only. The helper stays private to the policy package because the product
layer owns the command path that produces the text.
"""

from __future__ import annotations

import json
import re
from typing import Any

_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.MULTILINE)


def _parse_included_http_response(
    stdout: str,
) -> tuple[int | None, dict[str, Any] | None, bool]:
    """Parse ``gh api --include`` status/JSON and fail closed on malformed payloads."""
    matches = list(_HTTP_STATUS_RE.finditer(stdout))
    if not matches:
        return None, None, False
    status = int(matches[-1].group(1))
    headers_end = re.search(r"\r?\n\r?\n", stdout[matches[-1].start() :])
    if headers_end is None:
        return status, None, True
    body_start = matches[-1].start() + headers_end.end()
    body = stdout[body_start:].strip()
    if not body:
        return status, None, True
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return status, None, True
    return (status, parsed, False) if isinstance(parsed, dict) else (status, None, True)
