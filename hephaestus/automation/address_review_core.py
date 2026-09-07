"""Pure parsing helper for the pipeline-owned review-thread address job."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .remediation_recovery import RemediationReplyResult, RemediationReviewInput

from ._review_utils import parse_json_block
from .reply_limits import MAX_ADDRESS_REPLY_CHARS

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
        if not isinstance(reply, str) or not 0 < len(reply.strip()) <= MAX_ADDRESS_REPLY_CHARS:
            return None
        normalized[thread_id] = reply.strip()
    return normalized


def parse_remediation_reply_result(
    address_result: Any,
    review_input: RemediationReviewInput,
) -> RemediationReplyResult | None:
    """Bind one exhaustive recovery reply map to its exact review input."""
    from .remediation_recovery import RemediationReplyResult, RemediationReviewInput

    if (
        not isinstance(review_input, RemediationReviewInput)
        or not isinstance(address_result, dict)
        or set(address_result) != {"review_input_sha256", "replies"}
        or address_result.get("review_input_sha256") != review_input.review_input_sha256
    ):
        return None
    replies = address_result.get("replies")
    if not isinstance(replies, dict) or not all(
        isinstance(thread_id, str) and isinstance(reply, str)
        for thread_id, reply in replies.items()
    ):
        return None
    try:
        return RemediationReplyResult.create(
            review_input_sha256=review_input.review_input_sha256,
            replies=replies,
            thread_snapshot_json=review_input.thread_snapshot_json,
        )
    except (TypeError, ValueError):
        return None
