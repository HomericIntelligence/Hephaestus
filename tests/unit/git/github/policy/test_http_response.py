"""Behavior tests for the inclusive ``gh api`` response parser.

The parser reads untrusted network output, so each case pins the fail-closed
return. The test asserts the exact 3-tuple and reads no source file.
"""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.git.github.policy.http_response import _parse_included_http_response


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param("no status line here", (None, None, False), id="no-status-line"),
        pytest.param(
            "HTTP/2.0 200\r\nContent-Type: application/json",
            (200, None, True),
            id="headers-without-separator",
        ),
        pytest.param(
            "HTTP/2.0 200\r\nContent-Type: application/json\r\n\r\n",
            (200, None, True),
            id="empty-body",
        ),
        pytest.param("HTTP/2.0 200\r\n\r\n{not json", (200, None, True), id="invalid-json"),
        pytest.param("HTTP/2.0 200\r\n\r\n[1, 2]", (200, None, True), id="json-array"),
        pytest.param("HTTP/2.0 200\r\n\r\nnull", (200, None, True), id="json-null"),
        pytest.param(
            'HTTP/2.0 200\r\n\r\n{"id": 1}',
            (200, {"id": 1}, False),
            id="json-object",
        ),
        pytest.param(
            'HTTP/2.0 404\r\n\r\n{"message": "missing"}\nHTTP/2.0 200\r\n\r\n',
            (200, None, True),
            id="last-block-wins-without-body",
        ),
        pytest.param(
            'HTTP/2.0 404\r\n\r\n{}\nHTTP/2.0 201\r\n\r\n{"ok": true}',
            (201, {"ok": True}, False),
            id="last-block-wins-with-body",
        ),
    ],
)
def test_parse_included_http_response(stdout: str, expected: tuple[int | None, Any, bool]) -> None:
    """Return the exact status/body/failure triple for each payload class."""
    assert _parse_included_http_response(stdout) == expected
