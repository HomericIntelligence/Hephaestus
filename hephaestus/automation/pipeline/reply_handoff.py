"""Durable, head-gated implementation reply handoffs.

The implementation and PR-review stages can each hold a validated response
batch after the writer runs. This module is the single owner of the replay
contract: preserve the exact thread snapshots and response prose, bind them to
the pushed or unchanged reviewed head, then retry only that batch. A head or
conversation that actually changed is never replayed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any, Literal

from hephaestus.automation.address_review_core import parse_addressed_replies
from hephaestus.automation.pipeline.github_jobs import (
    DeliverReplyHandoffRequest,
    FrozenJson,
    ReplyHandoffAttempted,
)
from hephaestus.automation.review_journal import IssueComment

from .stages.base import ImplementationReplyProgress

IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP = 2
IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP = 2
IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRY_CAP = 2
PENDING_IMPLEMENTATION_REPLY_HANDOFF = "pending_implementation_reply_handoff"
PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES = "pending_implementation_reply_handoff_retries"
PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES = (
    "pending_implementation_reply_handoff_visibility_retries"
)
PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL = "pending_implementation_reply_handoff_journal"
PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RETRIES = (
    "pending_implementation_reply_handoff_journal_retries"
)

_BATCH_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HANDOFF_JOURNAL_RE = re.compile(
    r"^<!-- hephaestus-implementation-reply-handoff:"
    r"pr=(?P<pr>\d+):head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?):"
    r"batch=(?P<batch>[0-9a-f]{32}) -->$"
)


def pr_is_current_open_head(state: object, expected_head_sha: object) -> bool:
    """Return whether a fresh PR state proves one exact open, unarmed head."""
    return bool(
        isinstance(expected_head_sha, str)
        and _FULL_COMMIT_SHA_RE.fullmatch(expected_head_sha)
        and isinstance(state, dict)
        and state.get("state") == "OPEN"
        and state.get("autoMergeRequest") is None
        and state.get("headRefOid") == expected_head_sha
    )


def _has_complete_pr_state(state: object) -> bool:
    """Return whether a PR-state response contains all stale-proof fields."""
    return bool(
        isinstance(state, dict)
        and isinstance(state.get("state"), str)
        and "autoMergeRequest" in state
        and isinstance(state.get("headRefOid"), str)
        and _FULL_COMMIT_SHA_RE.fullmatch(state["headRefOid"])
    )


def implementation_reply_handoff(
    head_sha: object,
    threads: object,
    replies: object,
    batch_nonce: object,
    *,
    progress: ImplementationReplyProgress | None = None,
    reconciliation_only: bool = False,
) -> dict[str, Any] | None:
    """Return a replay-safe outstanding implementation-reply handoff.

    The persisted value is an exact host snapshot plus the model's already
    validated reply mapping.  It grants no authority itself: the GitHub
    adapter rechecks the current PR and thread state before every retry.
    """
    if (
        not isinstance(head_sha, str)
        or _FULL_COMMIT_SHA_RE.fullmatch(head_sha) is None
        or not isinstance(threads, list)
        or not isinstance(replies, dict)
        or not isinstance(batch_nonce, str)
        or _BATCH_NONCE_RE.fullmatch(batch_nonce) is None
    ):
        return None
    snapshots = [dict(thread) for thread in threads if isinstance(thread, dict)]
    if len(snapshots) != len(threads):
        return None
    normalized_replies = parse_addressed_replies(
        {"addressed": list(replies), "replies": replies}, snapshots
    )
    if normalized_replies is None:
        return None
    ids = {str(snapshot.get("id") or "") for snapshot in snapshots}
    if "" in ids or ids != set(normalized_replies) or len(ids) != len(snapshots):
        return None
    return {
        "head_sha": head_sha,
        "threads": deepcopy(snapshots),
        "replies": dict(normalized_replies),
        "batch_nonce": batch_nonce,
        "reconciliation_only": reconciliation_only,
        **({"progress": progress.as_dict()} if progress is not None else {}),
    }


def _thread_snapshot_fingerprint(threads: object) -> str | None:
    """Return the immutable source-conversation fingerprint for live threads.

    A writer push naturally changes a thread's live PR head and can move its
    diff anchor.  Those fields are deliberately excluded: the adapter checks
    the live head immediately before every mutation.  The recovery journal is
    instead bound to the complete sequence of existing source-comment ids and
    bodies, matching the adapter's per-thread concurrency guard.
    """
    if not isinstance(threads, list):
        return None
    source_threads: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    seen_thread_ids: set[str] = set()
    for thread in threads:
        if not isinstance(thread, dict):
            return None
        thread_id = thread.get("id")
        comments = thread.get("comments")
        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or thread_id in seen_thread_ids
            or not isinstance(comments, list)
            or not comments
        ):
            return None
        seen_thread_ids.add(thread_id)
        source_comments: list[tuple[str, str]] = []
        seen_comment_ids: set[str] = set()
        for comment in comments:
            if not isinstance(comment, dict):
                return None
            comment_id = comment.get("id")
            body = comment.get("body")
            if (
                not isinstance(comment_id, str)
                or not comment_id.strip()
                or comment_id in seen_comment_ids
                or not isinstance(body, str)
            ):
                return None
            seen_comment_ids.add(comment_id)
            source_comments.append((comment_id, body))
        source_threads.append((thread_id, tuple(source_comments)))
    try:
        encoded = json.dumps(sorted(source_threads), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        return None
    return sha256(encoded).hexdigest()


def implementation_reply_handoff_journal_entry(
    pr_number: object,
    handoff: object,
) -> tuple[str, str] | None:
    """Render the immutable GitHub journal record for one exact reply batch.

    This is an internal recovery artifact, not an implementation response: the
    only human-facing ``[Response]`` prose is later posted on its source
    review thread. Its sole purpose is to let a restarted coordinator replay
    the already-pushed writer's exact, validated batch without asking a fresh
    no-op implementer to make an unprovable claim.
    """
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return None
    progress = (
        ImplementationReplyProgress.from_dict(handoff.get("progress"))
        if isinstance(handoff, dict) and "progress" in handoff
        else None
    )
    if isinstance(handoff, dict) and "progress" in handoff and progress is None:
        return None
    normalized = implementation_reply_handoff(
        handoff.get("head_sha") if isinstance(handoff, dict) else None,
        handoff.get("threads") if isinstance(handoff, dict) else None,
        handoff.get("replies") if isinstance(handoff, dict) else None,
        handoff.get("batch_nonce") if isinstance(handoff, dict) else None,
        progress=progress,
        reconciliation_only=(
            isinstance(handoff, dict) and handoff.get("reconciliation_only") is True
        ),
    )
    if normalized is None:
        return None
    marker = (
        "<!-- hephaestus-implementation-reply-handoff:"
        f"pr={pr_number}:head={normalized['head_sha']}:batch={normalized['batch_nonce']} -->"
    )
    thread_snapshot_sha256 = _thread_snapshot_fingerprint(normalized["threads"])
    if thread_snapshot_sha256 is None:
        return None
    payload = json.dumps(
        {
            "format": 2,
            "armed": True,
            "pr_number": pr_number,
            "head_sha": normalized["head_sha"],
            "batch_nonce": normalized["batch_nonce"],
            "thread_snapshot_sha256": thread_snapshot_sha256,
            "replies": normalized["replies"],
            **(
                {"progress": normalized["progress"]}
                if isinstance(normalized.get("progress"), dict)
                else {}
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    # Keep the machine journal invisible in the issue timeline. The actual
    # implementation prose is only rendered as its source-attached response.
    body = f"{marker}\n<!-- {payload} -->"
    return marker, body


def _journal_handoff_from_comment(
    comment: IssueComment,
    *,
    pr_number: int,
    threads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Parse one actor-owned journal comment when it targets these source threads."""
    marker, separator, payload = comment.body.lstrip().partition("\n")
    match = _HANDOFF_JOURNAL_RE.fullmatch(marker)
    if match is None or int(match.group("pr")) != pr_number:
        return None
    if not (separator and payload.startswith("<!-- ") and payload.endswith(" -->")):
        raise ValueError("implementation reply handoff journal is malformed")
    try:
        raw = json.loads(payload.removeprefix("<!-- ").removesuffix(" -->"))
    except json.JSONDecodeError as error:
        raise ValueError("implementation reply handoff journal is malformed") from error
    if (
        not isinstance(raw, dict)
        or raw.get("format") not in {1, 2}
        or raw.get("pr_number") != pr_number
        or (raw.get("format") == 2 and raw.get("armed") is not True)
    ):
        raise ValueError("implementation reply handoff journal identity is invalid")
    if raw.get("thread_snapshot_sha256") != _thread_snapshot_fingerprint(threads):
        return None
    progress = (
        ImplementationReplyProgress.from_dict(raw.get("progress")) if "progress" in raw else None
    )
    if "progress" in raw and progress is None:
        raise ValueError("implementation reply handoff progress is invalid")
    handoff = implementation_reply_handoff(
        raw.get("head_sha"),
        threads,
        raw.get("replies"),
        raw.get("batch_nonce"),
        progress=progress,
        reconciliation_only=True,
    )
    if (
        handoff is None
        or not isinstance(raw.get("head_sha"), str)
        or not isinstance(raw.get("batch_nonce"), str)
        or handoff["head_sha"] != match.group("head")
        or handoff["batch_nonce"] != match.group("batch")
    ):
        raise ValueError("implementation reply handoff journal payload is invalid")
    handoff["reconciliation_only"] = True
    return handoff


def journaled_implementation_reply_handoff(
    comments: Sequence[IssueComment],
    *,
    pr_number: object,
    threads: object,
) -> dict[str, Any] | None:
    """Recover the latest exact actor-owned reply batch for current source threads.

    A journal record is eligible only if GitHub identifies it as written by
    the current actor and its immutable source-snapshot fingerprint exactly
    matches the live remediation snapshot read for this pass. The host adapter
    still validates the current open head and every source thread before any
    reply mutation occurs.
    """
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return None
    if not isinstance(threads, list) or not all(isinstance(thread, dict) for thread in threads):
        return None
    recovered: dict[str, Any] | None = None
    for comment in comments:
        if (
            comment.viewer_did_author
            and (
                handoff := _journal_handoff_from_comment(
                    comment,
                    pr_number=pr_number,
                    threads=threads,
                )
            )
            is not None
        ):
            recovered = handoff
    return recovered


def _take_visibility_retry(
    payload: dict[str, Any],
    *,
    issue_number: int | None,
    logger: logging.Logger,
) -> bool:
    """Record one bounded backoff while GitHub converges on a pushed head."""
    visibility_retries = payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, 0)
    if (
        not isinstance(visibility_retries, int)
        or isinstance(visibility_retries, bool)
        or visibility_retries < 0
        or visibility_retries >= IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP
    ):
        return False
    visibility_retries += 1
    payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES] = visibility_retries
    payload["retry_delay_s"] = float(2 ** (visibility_retries - 1))
    logger.info(
        "reply_handoff:%s: waiting for pushed implementation head visibility before replying "
        "to review threads (%d/%d)",
        issue_number,
        visibility_retries,
        IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP,
    )
    return True


def _classify_handoff_pr_state(
    state: object,
    *,
    head_sha: str,
    payload: dict[str, Any],
    issue_number: int | None,
    logger: logging.Logger,
) -> Literal["current", "visibility_wait", "stale", "retry"]:
    """Classify one host state read without confusing incompleteness with drift."""
    if not isinstance(state, dict) or not _has_complete_pr_state(state):
        return "retry"
    if pr_is_current_open_head(state, head_sha):
        return "current"
    if (
        state.get("state") == "OPEN"
        and state.get("autoMergeRequest") is None
        and _take_visibility_retry(payload, issue_number=issue_number, logger=logger)
    ):
        return "visibility_wait"
    return "stale"


def _consume_reply_post_result(
    payload: dict[str, Any],
    *,
    result: object,
    head_sha: str,
    threads: list[dict[str, Any]],
    replies: dict[str, str],
    batch_nonce: str,
    issue_number: int | None,
    logger: logging.Logger,
) -> Literal["completed", "visibility_wait", "stale", "invalid", "blocked", "retry"]:
    """Classify one reply mutation result and retain only proven retry work."""
    expected_ids = set(replies)
    if bool(getattr(result, "outcome_unknown", False)):
        # An issued mutation can have succeeded even when its receipt was
        # lost.  Never retain the target in a pending handoff for replay.
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
        return "blocked"
    replied = set(getattr(result, "replied_thread_ids", ()))
    receipts = list(getattr(result, "receipts", ()))
    if replied == expected_ids and len(receipts) == len(replied):
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        return "completed"
    if bool(getattr(result, "visibility_lag", False)):
        if replied or receipts:
            return "invalid"
        if _take_visibility_retry(
            payload,
            issue_number=issue_number,
            logger=logger,
        ):
            return "visibility_wait"
        return "stale"
    progress = getattr(result, "progress", None)
    if bool(getattr(result, "retryable", False)) and isinstance(
        progress, ImplementationReplyProgress
    ):
        replacement = implementation_reply_handoff(
            head_sha,
            threads,
            replies,
            batch_nonce,
            progress=progress,
            reconciliation_only=False,
        )
        if replacement is None:
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            return "invalid"
        payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = replacement
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        return "retry"
    remaining_ids = expected_ids - replied
    retryable = bool(getattr(result, "retryable", False))
    result_retryable_ids = set(getattr(result, "retryable_thread_ids", ()))
    retryable_ids = (
        result_retryable_ids
        if result_retryable_ids and result_retryable_ids.issubset(remaining_ids)
        else remaining_ids
        if retryable and not result_retryable_ids
        else set()
    )
    if not retryable_ids:
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        return "stale"
    if not replied.issubset(expected_ids) or len(receipts) != len(replied):
        return "invalid"
    replacement = implementation_reply_handoff(
        head_sha,
        [snapshot for snapshot in threads if str(snapshot.get("id") or "") in retryable_ids],
        {thread_id: reply for thread_id, reply in replies.items() if thread_id in retryable_ids},
        batch_nonce,
    )
    if replacement is None:
        return "invalid"
    payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = replacement
    return "retry"


def retry_pending_implementation_reply_handoff(  # noqa: C901
    payload: dict[str, Any],
    *,
    pr_number: int | None,
    issue_number: int | None,
    github: Any,
    logger: logging.Logger,
) -> Literal["none", "completed", "visibility_wait", "stale", "invalid", "blocked", "retry"]:
    """Retry one exact post-push reply batch without invoking an agent.

    Returns ``none`` when no handoff exists, ``completed`` when every reply
    has a host receipt, ``visibility_wait`` while GitHub is briefly catching
    up with the pushed head, ``stale`` when the exact pushed head can no
    longer safely receive the saved response, ``invalid`` for malformed
    persisted state, and ``retry`` for a bounded transient/incomplete host
    operation.  No outcome authorizes review or merge decisions.
    """
    raw_handoff = payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF)
    if raw_handoff is None:
        return "none"
    handoff = implementation_reply_handoff(
        raw_handoff.get("head_sha") if isinstance(raw_handoff, dict) else None,
        raw_handoff.get("threads") if isinstance(raw_handoff, dict) else None,
        raw_handoff.get("replies") if isinstance(raw_handoff, dict) else None,
        raw_handoff.get("batch_nonce") if isinstance(raw_handoff, dict) else None,
    )
    if handoff is None or pr_number is None:
        return "invalid"
    head_sha = handoff["head_sha"]
    threads = handoff["threads"]
    replies = handoff["replies"]
    batch_nonce = handoff["batch_nonce"]
    reconciliation_only = (
        isinstance(raw_handoff, dict) and raw_handoff.get("reconciliation_only") is True
    )
    progress = (
        ImplementationReplyProgress.from_dict(raw_handoff.get("progress"))
        if isinstance(raw_handoff, dict) and "progress" in raw_handoff
        else None
    )
    if isinstance(raw_handoff, dict) and "progress" in raw_handoff and progress is None:
        return "invalid"
    try:
        state = github.gh_pr_state(pr_number)
        pr_state_outcome = _classify_handoff_pr_state(
            state,
            head_sha=head_sha,
            payload=payload,
            issue_number=issue_number,
            logger=logger,
        )
        if pr_state_outcome != "current":
            if pr_state_outcome != "stale":
                return pr_state_outcome
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
            return "stale"
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
        if reconciliation_only:
            # A journal-recovered armed intent is read-only.  The accessor
            # owns the complete marker-bound reconciliation and has no
            # mutation-capable fallback.
            result = github.reconcile_implementation_thread_replies(
                pr_number,
                expected_head_sha=head_sha,
                threads=threads,
                replies=replies,
                batch_nonce=batch_nonce,
            )
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            if bool(getattr(result, "outcome_unknown", False)) or bool(
                getattr(result, "blocked_thread_ids", ())
            ):
                return "blocked"
            return "completed" if getattr(result, "replied_thread_ids", ()) else "blocked"
        delivery_kwargs: dict[str, Any] = {
            "expected_head_sha": head_sha,
            "threads": threads,
            "replies": replies,
            "batch_nonce": batch_nonce,
        }
        if progress is not None:
            delivery_kwargs["progress"] = progress
        result = github.post_implementation_thread_replies(pr_number, **delivery_kwargs)
    except Exception as error:
        pre_dispatch_retry = (
            type(error).__name__ == "GraphQLRetryableError"
            and type(error).__module__ == "hephaestus.automation.github_api.graphql"
            and getattr(error, "pre_dispatch", False) is True
        )
        if pre_dispatch_retry:
            if reconciliation_only:
                return "retry"
            logger.warning(
                "reply_handoff:%s: mutation handoff transport was retryable before proof",
                issue_number,
            )
            return "retry"
        if reconciliation_only:
            # A recovered armed intent is reconciliation-only.  Any missing
            # read seam or incomplete proof blocks the intent; it must never
            # fall through to the ordinary mutation retry path.
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            logger.warning(
                "reply_handoff:%s: reconciliation could not prove the armed reply (%s)",
                issue_number,
                type(error).__name__,
            )
            return "blocked"
        logger.warning(
            "reply_handoff:%s: implementation reply handoff retry failed (%s)",
            issue_number,
            type(error).__name__,
        )
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        return "blocked"

    return _consume_reply_post_result(
        payload,
        result=result,
        head_sha=head_sha,
        threads=threads,
        replies=replies,
        batch_nonce=batch_nonce,
        issue_number=issue_number,
        logger=logger,
    )


def attempt_reply_handoff(
    request: DeliverReplyHandoffRequest,
    github: Any,
) -> ReplyHandoffAttempted:
    """Attempt a handoff against detached state and return an immutable receipt."""
    payload: dict[str, Any] = {
        PENDING_IMPLEMENTATION_REPLY_HANDOFF: request.handoff.thaw(),
        PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES: request.visibility_retries,
    }
    status = retry_pending_implementation_reply_handoff(
        payload,
        pr_number=request.pr_number,
        issue_number=request.issue_number,
        github=github,
        logger=logging.getLogger(__name__),
    )
    if status == "none":
        status = "invalid"
    remaining = payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF)
    retry_delay = payload.get("retry_delay_s")
    return ReplyHandoffAttempted(
        request=request,
        status=status,
        remaining_handoff=(FrozenJson.snapshot(remaining) if remaining is not None else None),
        visibility_retries=int(
            payload.get(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, 0)
        ),
        retry_delay_s=(float(retry_delay) if isinstance(retry_delay, (int, float)) else None),
    )
