"""Pure validation helpers for recoverable GitHub review replies."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from .remediation_recovery import REMEDIATION_THREAD_SNAPSHOT_MAX_BYTES

THREAD_COMMENT_MAX = 2_000
THREAD_COMMENT_PAGE_MAX = 20


def implementation_review_body(
    pull_request_id: str,
    head_sha: str,
    batch_nonce: str,
) -> str:
    """Return the immutable marker for one pending implementation review."""
    return (
        "<!-- hephaestus-implementation-review:"
        f"pr-node={pull_request_id}:head={head_sha}:batch={batch_nonce} -->"
    )


def recover_pending_implementation_review(
    reviews: Iterable[Mapping[str, object]],
    *,
    pull_request_id: str,
    head_sha: str,
    batch_nonce: str,
) -> str | None:
    """Return the sole actor-owned pending review for a recovered batch."""
    expected_body = implementation_review_body(pull_request_id, head_sha, batch_nonce)
    matches = [
        review
        for review in reviews
        if review.get("body") == expected_body
        and review.get("state") == "PENDING"
        and review.get("viewerDidAuthor") is True
    ]
    if len(matches) > 1:
        raise RuntimeError("implementation review batch marker is duplicated")
    if not matches:
        return None
    review_id = matches[0].get("id")
    if not isinstance(review_id, str) or not review_id:
        raise RuntimeError("implementation review batch receipt is invalid")
    return review_id


def select_pending_implementation_review(
    reviews: Iterable[Mapping[str, object]] | None,
    *,
    saved_review_id: str | None,
    observed_review_id: str | None,
    pull_request_id: str,
    head_sha: str,
    batch_nonce: str,
    has_commented_reply: bool,
) -> str | None:
    """Select only one live or marked pending review for batch recovery."""
    if (
        observed_review_id is not None
        and saved_review_id is not None
        and observed_review_id != saved_review_id
    ):
        raise ValueError("saved and live pending reviews do not match")
    recovered_review_id = (
        recover_pending_implementation_review(
            reviews,
            pull_request_id=pull_request_id,
            head_sha=head_sha,
            batch_nonce=batch_nonce,
        )
        if reviews is not None and observed_review_id is None
        else None
    )
    if has_commented_reply and observed_review_id is None and recovered_review_id is None:
        return None
    return observed_review_id or recovered_review_id or saved_review_id


def admitted_review_comment(
    comment: Mapping[str, object],
    *,
    author: str,
    author_type: str,
    review: tuple[str | None, str | None, str, str],
    comment_count: int,
    current_bytes: int,
) -> tuple[dict[str, object], int] | None:
    """Normalize one reviewed comment and return its new bounded byte total."""
    if comment_count >= THREAD_COMMENT_MAX:
        return None
    review_id, review_state, review_body, review_commit_sha = review
    normalized = {
        "id": comment["id"],
        "body": comment["body"],
        "author": author,
        "author_type": author_type,
        "viewer_did_author": comment["viewerDidAuthor"],
        "review_id": review_id,
        "review_state": review_state,
        "review_body": review_body,
        "review_commit_sha": review_commit_sha,
    }
    encoded_size = len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    new_size = current_bytes + encoded_size
    if new_size > REMEDIATION_THREAD_SNAPSHOT_MAX_BYTES:
        return None
    return normalized, new_size
