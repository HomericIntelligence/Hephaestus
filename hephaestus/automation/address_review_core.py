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


def parse_addressed_replies(
    address_result: Any, threads: list[dict[str, Any]]
) -> dict[str, str] | None:
    """Validate one non-empty implementation reply for every supplied thread."""
    if not isinstance(address_result, dict):
        return None
    addressed = address_result.get("addressed")
    replies = address_result.get("replies")
    if not isinstance(addressed, list) or not isinstance(replies, dict):
        return None
    known_ids: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            return None
        thread_id = str(thread.get("thread_id") or thread.get("id") or "").strip()
        if not thread_id or thread_id in known_ids:
            return None
        known_ids.append(thread_id)
    claimed_ids = [thread_id.strip() for thread_id in addressed if isinstance(thread_id, str)]
    if (
        len(claimed_ids) != len(addressed)
        or any(not thread_id for thread_id in claimed_ids)
        or len(set(claimed_ids)) != len(claimed_ids)
        or set(claimed_ids) != set(known_ids)
        or set(replies) != set(known_ids)
    ):
        return None
    normalized: dict[str, str] = {}
    for thread_id in known_ids:
        reply = replies.get(thread_id)
        if not isinstance(reply, str) or not 0 < len(reply.strip()) <= 4_000:
            return None
        normalized[thread_id] = reply.strip()
    return normalized
