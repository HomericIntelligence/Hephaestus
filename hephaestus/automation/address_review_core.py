"""Pure parsing helper for the pipeline-owned review-thread address job."""

from __future__ import annotations

from typing import Any

from ._review_utils import parse_json_block

_ADDRESS_PARSE_DEFAULT: dict[str, Any] = {"addressed": [], "replies": {}}


def _parse_addressed_block(text: str) -> dict[str, Any]:
    """Extract the last JSON object emitted by the pipeline address agent.

    Args:
        text: The address agent's full response.

    Returns:
        The parsed ``addressed``/``replies`` object, or the empty default when
        no parseable JSON object is present.

    """
    return parse_json_block(text, default=_ADDRESS_PARSE_DEFAULT)
