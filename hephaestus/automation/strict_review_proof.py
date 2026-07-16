"""Commit-bound required-check verifier for strict-review artifacts.

The queue's strict-review stage publishes an authenticated issue-comment
artifact. That artifact is durable evidence, but a comment alone cannot stop
GitHub auto-merge from accepting a later head while the coordinator is parked.
The dedicated trusted ``strict-review-proof`` workflow invokes this module for
each pull-request event, then publishes its result as a commit status on that
event's immutable head SHA. A new head therefore has no passing proof until it
receives its own authenticated strict-review artifact.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.automation.strict_review_artifact import (
    STRICT_REVIEW_ARTIFACT_MARKER,
    STRICT_REVIEW_ARTIFACT_V2_MARKER,
    parse_strict_review_artifact,
    parse_strict_review_lease,
)

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_TrustedComment = tuple[int, int, str]
_LeaseRecord = tuple[str, str, int, int]


def _comment_entries(raw_comments: object) -> list[dict[str, Any]] | None:
    """Normalize GitHub's paginated comments response, rejecting ambiguity.

    ``gh api --paginate --slurp`` produces a list of page lists. A one-page
    fixture or direct API response can be a list of comment objects. Every
    other shape is treated as an unavailable proof rather than guessed at.
    """
    if not isinstance(raw_comments, list):
        return None
    comments: list[dict[str, Any]] = []
    for page in raw_comments:
        if isinstance(page, dict):
            comments.append(page)
            continue
        if not isinstance(page, list):
            return None
        if not all(isinstance(comment, dict) for comment in page):
            return None
        comments.extend(page)
    return comments


def _comment_id(comment: dict[str, Any]) -> int | None:
    """Return GitHub's immutable numeric comment id, rejecting ambiguity."""
    value = comment.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _comment_timestamp(comment: dict[str, Any]) -> int | None:
    """Return an aware GitHub ``created_at`` timestamp in whole UTC seconds."""
    value = comment.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.astimezone(timezone.utc).timestamp())


def _trusted_comment_records(
    comments: list[dict[str, Any]], trusted_login: str
) -> list[_TrustedComment] | None:
    """Return trusted immutable comment metadata or fail on an ambiguity."""
    trusted: list[_TrustedComment] = []
    seen_comment_ids: set[int] = set()
    for comment in comments:
        body = comment.get("body")
        user = comment.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        if author != trusted_login or not isinstance(body, str):
            continue
        comment_id = _comment_id(comment)
        created_at = _comment_timestamp(comment)
        if comment_id is None or created_at is None or comment_id in seen_comment_ids:
            return None
        seen_comment_ids.add(comment_id)
        trusted.append((comment_id, created_at, body))
    return trusted


def _valid_leases(trusted: list[_TrustedComment], expected_head: str) -> dict[int, _LeaseRecord]:
    """Parse only leases that were alive when their immutable claim was written."""
    leases: dict[int, _LeaseRecord] = {}
    for comment_id, created_at, body in trusted:
        lease = parse_strict_review_lease(body)
        if lease is None or lease.head_sha != expected_head or created_at > lease.expires_at:
            continue
        leases[comment_id] = (lease.head_sha, lease.lease_id, created_at, lease.expires_at)
    return leases


def _artifact_uses_elected_live_lease(
    artifact: Any,
    result_created_at: int,
    *,
    expected_head: str,
    leases: dict[int, _LeaseRecord],
) -> bool:
    """Validate a v2 result against lease identity, lifetime, and election."""
    if artifact.lease_id is None or artifact.lease_comment_id is None:
        return False
    lease = leases.get(artifact.lease_comment_id)
    if lease is None:
        return False
    lease_head, lease_id, lease_created_at, lease_expires_at = lease
    if (
        lease_head != expected_head
        or lease_id != artifact.lease_id
        or result_created_at < lease_created_at
        or result_created_at > lease_expires_at
    ):
        return False
    live_lease_ids = [
        comment_id
        for comment_id, (
            candidate_head,
            _candidate_id,
            candidate_created,
            candidate_expires,
        ) in leases.items()
        if (
            candidate_head == expected_head
            and candidate_created <= result_created_at <= candidate_expires
        )
    ]
    return bool(live_lease_ids and artifact.lease_comment_id == min(live_lease_ids))


def _verdict_state_for_exact_head(
    trusted: list[_TrustedComment], expected_head: str, leases: dict[int, _LeaseRecord]
) -> tuple[bool, bool] | None:
    """Return ``(GO, NOGO)`` for valid evidence, or ``None`` on ambiguity.

    A malformed v2 result and a result whose immutable comment id predates its
    referenced lease are protocol ambiguities.  They must fail the complete
    proof rather than permit a previous GO to remain authoritative.
    """
    qualifying_go = False
    terminal_nogo = False
    for result_id, result_created_at, body in trusted:
        if body.startswith(STRICT_REVIEW_ARTIFACT_MARKER):
            legacy = parse_strict_review_artifact(body)
            if (
                legacy is not None
                and legacy.schema_version == 1
                and legacy.head_sha == expected_head
                and not legacy.is_go
            ):
                terminal_nogo = True
            continue
        if not body.startswith(STRICT_REVIEW_ARTIFACT_V2_MARKER):
            continue
        artifact = parse_strict_review_artifact(body)
        if artifact is None or artifact.schema_version != 2:
            return None
        if artifact.head_sha != expected_head:
            continue
        if artifact.lease_comment_id is None or result_id <= artifact.lease_comment_id:
            return None
        if not _artifact_uses_elected_live_lease(
            artifact,
            result_created_at,
            expected_head=expected_head,
            leases=leases,
        ):
            continue
        if artifact.is_go:
            qualifying_go = True
        else:
            terminal_nogo = True
    return qualifying_go, terminal_nogo


def has_trusted_strict_review_proof(
    raw_comments: object, expected_head_sha: str, automation_login: str
) -> bool:
    """Return whether elected, valid v2 evidence authorizes ``expected_head``.

    v1 artifacts remain readable for audit but are never required-check
    authority. For v2, this verifier replays the adapter's durable fencing
    decision from immutable GitHub metadata: a result must reference an
    authenticated lease, appear while it was live, and originate from the
    lowest-id valid lease active at that result time. Any such NOGO is
    terminal for the head and dominates every GO.
    """
    expected_head = expected_head_sha.strip().lower()
    trusted_login = automation_login.strip()
    if _SHA_RE.fullmatch(expected_head) is None or not trusted_login:
        return False
    comments = _comment_entries(raw_comments)
    if comments is None:
        return False
    trusted = _trusted_comment_records(comments, trusted_login)
    if trusted is None:
        return False
    leases = _valid_leases(trusted, expected_head)

    verdict_state = _verdict_state_for_exact_head(trusted, expected_head, leases)
    if verdict_state is None:
        return False
    qualifying_go, terminal_nogo = verdict_state
    return qualifying_go and not terminal_nogo


def _parser() -> argparse.ArgumentParser:
    """Build the fail-closed command-line parser used by GitHub Actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comments-json", type=Path, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--automation-login", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the event head's strict-review proof, returning a shell status."""
    args = _parser().parse_args(argv)
    try:
        raw_comments = json.loads(args.comments_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read strict-review comments: {exc}", file=sys.stderr)
        return 1
    if not has_trusted_strict_review_proof(
        raw_comments, args.expected_head_sha, args.automation_login
    ):
        print(
            "ERROR: the current PR head lacks an authenticated strict-review GO artifact.",
            file=sys.stderr,
        )
        return 1
    print("Authenticated strict-review GO artifact matches the current PR head.")
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI dispatch
    raise SystemExit(main())
