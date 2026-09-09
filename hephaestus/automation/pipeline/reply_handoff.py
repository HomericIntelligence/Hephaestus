"""Durable, head-gated implementation reply handoffs.

The implementation stage holds each validated response batch after the
writer runs. Preserve its exact thread snapshots and response text. Bind the
batch to the pushed or unchanged reviewed head. Retry only that batch. Do
not replay it after its head or conversation changes.
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
    ImplementationReplyProgress,
    ReplyHandoffAttempted,
)
from hephaestus.automation.remediation_recovery import (
    REMEDIATION_JOURNAL_COMMENT_MAX_BYTES,
    RemediationReplyResult,
    RemediationReviewInput,
    decode_remediation_review_input,
    encode_remediation_review_input,
)
from hephaestus.automation.reply_limits import MAX_ADDRESS_REPLY_CHARS
from hephaestus.automation.review_journal import IssueComment

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
PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL_RECOVERY_RETRIES = (
    "pending_implementation_reply_handoff_journal_recovery_retries"
)

_BATCH_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HANDOFF_JOURNAL_RE = re.compile(
    r"^<!-- hephaestus-implementation-reply-handoff:"
    r"pr=(?P<pr>\d+):head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?):"
    r"batch=(?P<batch>[0-9a-f]{32}) -->$"
)
_REMEDIATION_HANDOFF_JOURNAL_RE = re.compile(
    r"^<!-- hephaestus-implementation-remediation-reply-handoff:"
    r"pr=(?P<pr>\d+):head=(?P<head>[0-9a-f]{40}(?:[0-9a-f]{24})?):"
    r"batch=(?P<batch>[0-9a-f]{32}):seq=(?P<sequence>0|[1-9]\d*) -->$"
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object and reject each duplicate member name."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> object:
    """Decode JSON without the standard decoder's last-member-wins rule."""
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("journal JSON is malformed or has duplicate members") from error


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
    raw = _strict_json_loads(payload.removeprefix("<!-- ").removesuffix(" -->"))
    if (
        not isinstance(raw, dict)
        or raw.get("format") != 2
        or raw.get("pr_number") != pr_number
        or raw.get("armed") is not True
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


def implementation_remediation_reply_handoff(
    review_input: RemediationReviewInput,
    reply_result: RemediationReplyResult,
    batch_nonce: object,
    *,
    progress: ImplementationReplyProgress | None = None,
    journal_input_encoding: object = None,
    journal_input_data: object = None,
    journal_sequence: object = 0,
    journal_predecessor_sha256: object = None,
) -> dict[str, Any] | None:
    """Return one remediation-only reply handoff."""
    if (
        not isinstance(review_input, RemediationReviewInput)
        or not isinstance(reply_result, RemediationReplyResult)
        or reply_result.review_input_sha256 != review_input.review_input_sha256
        or not isinstance(batch_nonce, str)
        or _BATCH_NONCE_RE.fullmatch(batch_nonce) is None
        or isinstance(journal_sequence, bool)
        or not isinstance(journal_sequence, int)
        or journal_sequence < 0
        or (journal_sequence == 0 and journal_predecessor_sha256 is not None)
        or (
            journal_sequence > 0
            and (
                not isinstance(journal_predecessor_sha256, str)
                or _SHA256_RE.fullmatch(journal_predecessor_sha256) is None
            )
        )
    ):
        return None
    try:
        threads = json.loads(review_input.thread_snapshot_json)
        if journal_input_encoding is None and journal_input_data is None:
            journal_input_encoding, journal_input_data = encode_remediation_review_input(
                review_input.canonical_bytes
            )
        if (
            decode_remediation_review_input(journal_input_encoding, journal_input_data)
            != review_input.canonical_bytes
        ):
            return None
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(threads, list):
        return None
    base = implementation_reply_handoff(
        review_input.recovery_commit_sha,
        threads,
        dict(reply_result.replies),
        batch_nonce,
        progress=progress,
    )
    if base is None:
        return None
    return {
        **base,
        "format": 3,
        "kind": "remediation-recovery",
        "repository": review_input.repository,
        "issue_number": review_input.issue_number,
        "pr_number": review_input.pr_number,
        "branch": review_input.branch,
        "review_input_bytes": review_input.canonical_bytes.decode("utf-8"),
        "review_input_sha256": review_input.review_input_sha256,
        "review_input_encoding": journal_input_encoding,
        "review_input_data": journal_input_data,
        "reply_result": reply_result.as_dict(),
        "reply_result_sha256": reply_result.digest,
        "journal_sequence": journal_sequence,
        "journal_predecessor_sha256": journal_predecessor_sha256,
    }


def implementation_remediation_reply_handoff_journal_entry(
    pr_number: object,
    handoff: object,
) -> tuple[str, str] | None:
    """Render one format-3 remediation-only journal entry."""
    if (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number <= 0
        or not isinstance(handoff, dict)
        or handoff.get("format") != 3
        or handoff.get("kind") != "remediation-recovery"
        or handoff.get("pr_number") != pr_number
    ):
        return None
    progress = (
        ImplementationReplyProgress.from_dict(handoff.get("progress"))
        if "progress" in handoff
        else None
    )
    if "progress" in handoff and progress is None:
        return None
    try:
        raw_review_input = handoff["review_input_bytes"].encode("utf-8")
        review_input = RemediationReviewInput.from_canonical_bytes(raw_review_input)
        reply_result = RemediationReplyResult.from_dict(handoff["reply_result"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    normalized = implementation_remediation_reply_handoff(
        review_input,
        reply_result,
        handoff.get("batch_nonce"),
        progress=progress,
        journal_input_encoding=handoff.get("review_input_encoding"),
        journal_input_data=handoff.get("review_input_data"),
        journal_sequence=handoff.get("journal_sequence"),
        journal_predecessor_sha256=handoff.get("journal_predecessor_sha256"),
    )
    if normalized is None or any(
        normalized.get(key) != handoff.get(key)
        for key in (
            "repository",
            "issue_number",
            "pr_number",
            "branch",
            "head_sha",
            "batch_nonce",
            "review_input_sha256",
            "review_input_encoding",
            "review_input_data",
            "reply_result_sha256",
            "journal_sequence",
            "journal_predecessor_sha256",
        )
    ):
        return None
    marker = (
        "<!-- hephaestus-implementation-remediation-reply-handoff:"
        f"pr={pr_number}:head={review_input.recovery_commit_sha}:"
        f"batch={normalized['batch_nonce']}:seq={normalized['journal_sequence']} -->"
    )
    payload: dict[str, Any] = {
        "format": 3,
        "kind": "remediation-recovery",
        "armed": True,
        "repository": review_input.repository,
        "issue_number": review_input.issue_number,
        "pr_number": review_input.pr_number,
        "head_sha": review_input.recovery_commit_sha,
        "branch": review_input.branch,
        "batch_nonce": normalized["batch_nonce"],
        "review_input_encoding": normalized["review_input_encoding"],
        "review_input_data": normalized["review_input_data"],
        "review_input_sha256": review_input.review_input_sha256,
        "reply_result": reply_result.as_dict(),
        "reply_result_sha256": reply_result.digest,
        "journal_sequence": normalized["journal_sequence"],
        "journal_predecessor_sha256": normalized["journal_predecessor_sha256"],
    }
    if progress is not None:
        payload["progress"] = progress.as_dict()
    body = f"{marker}\n<!-- {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"
    if len(body.encode("utf-8")) > REMEDIATION_JOURNAL_COMMENT_MAX_BYTES:
        return None
    return marker, body


def _implementation_reply_body(
    repository: str,
    pr_number: int,
    head_sha: str,
    thread_id: str,
    reply: str,
    batch_nonce: str,
) -> str:
    """Return the deterministic reply body that the GitHub adapter uses."""
    response = reply.strip().removeprefix("[Response] ")
    seed = ":".join((repository, str(pr_number), thread_id, head_sha, response, batch_nonce))
    marker = sha256(seed.encode("utf-8")).hexdigest()[:24]
    return (
        f"[Response] {response}\n\n"
        f"<!-- hephaestus-implementation-reply:{marker} -->\n"
        f"<!-- hephaestus-implementation-batch:{batch_nonce} -->"
    )


def _progress_matches_live_threads(  # noqa: C901 - sealed fail-closed validation matrix
    review_input: RemediationReviewInput,
    reply_result: RemediationReplyResult,
    batch_nonce: str,
    progress: ImplementationReplyProgress | None,
    live_threads: list[dict[str, Any]],
) -> bool:
    """Return whether live threads equal the source plus proved progress."""
    try:
        original = json.loads(review_input.thread_snapshot_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(original, list):
        return False
    original_by_id = {
        str(thread.get("id")): thread
        for thread in original
        if isinstance(thread, dict) and isinstance(thread.get("id"), str)
    }
    live_by_id = {
        str(thread.get("id")): thread
        for thread in live_threads
        if isinstance(thread, dict) and isinstance(thread.get("id"), str)
    }
    if len(original_by_id) != len(original) or len(live_by_id) != len(live_threads):
        return False
    if set(original_by_id) != set(live_by_id):
        return False
    expected_by_id = dict(original_by_id)
    if progress is not None:
        replied_ids = progress.replied_thread_ids
        if len(set(replied_ids)) != len(replied_ids) or len(progress.receipts) != len(replied_ids):
            return False
        receipts_by_id: dict[str, dict[str, Any]] = {}
        replies = dict(reply_result.replies)
        for receipt in progress.receipts:
            thread_id = receipt.get("id") or receipt.get("thread_id")
            if (
                not isinstance(thread_id, str)
                or thread_id not in original_by_id
                or thread_id in receipts_by_id
                or receipt.get("implementation_head_sha") != review_input.recovery_commit_sha
            ):
                return False
            reply = replies.get(thread_id)
            comment_id = receipt.get("implementation_reply_id")
            reply_body = receipt.get("implementation_reply_body")
            comments = receipt.get("comments")
            if (
                not isinstance(reply, str)
                or not isinstance(comment_id, str)
                or not comment_id
                or not isinstance(reply_body, str)
                or reply_body
                != _implementation_reply_body(
                    review_input.repository,
                    review_input.pr_number,
                    review_input.recovery_commit_sha,
                    thread_id,
                    reply,
                    batch_nonce,
                )
                or not isinstance(comments, list)
                or not comments
                or not isinstance(comments[-1], dict)
                or comments[-1].get("id") != comment_id
                or comments[-1].get("body") != reply_body
            ):
                return False
            live_comments = live_by_id[thread_id].get("comments")
            if (
                not isinstance(live_comments, list)
                or not live_comments
                or not isinstance(live_comments[-1], dict)
                or any(
                    live_comments[-1].get(field) != comments[-1].get(field)
                    for field in (
                        "id",
                        "body",
                        "viewer_did_author",
                        "review_id",
                        "review_state",
                        "review_commit_sha",
                    )
                )
            ):
                return False
            receipts_by_id[thread_id] = dict(receipt)
        if set(receipts_by_id) != set(replied_ids):
            return False
        expected_by_id.update(receipts_by_id)
    try:
        ordered_ids = [str(thread["id"]) for thread in original]
        expected_json = RemediationReviewInput.canonical_thread_snapshot(
            [expected_by_id[thread_id] for thread_id in ordered_ids]
        )
        live_json = RemediationReviewInput.canonical_thread_snapshot(
            [live_by_id[thread_id] for thread_id in ordered_ids]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return expected_json == live_json


def _canonical_comment_prefix(comments: list[object]) -> tuple[tuple[str, str, str], ...]:
    """Return the ordered canonical fields from hydrated GitHub comments."""
    canonical: list[tuple[str, str, str]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("remediation journal thread snapshot is invalid")
        comment_id = comment.get("id")
        author = comment.get("author")
        body = comment.get("body")
        if (
            not isinstance(comment_id, str)
            or not comment_id
            or not isinstance(author, str)
            or not isinstance(body, str)
        ):
            raise ValueError("remediation journal thread snapshot is invalid")
        canonical.append((comment_id, author, body))
    return tuple(canonical)


def _recover_live_progress(  # noqa: C901 - crash-state reconciliation matrix
    review_input: RemediationReviewInput,
    reply_result: RemediationReplyResult,
    batch_nonce: str,
    progress: ImplementationReplyProgress | None,
    live_threads: list[dict[str, Any]],
) -> ImplementationReplyProgress | None:
    """Recover exact batch replies that GitHub accepted before a process crash."""
    if _progress_matches_live_threads(
        review_input,
        reply_result,
        batch_nonce,
        progress,
        live_threads,
    ):
        return progress
    original = json.loads(review_input.thread_snapshot_json)
    if not isinstance(original, list):
        raise ValueError("remediation journal thread snapshot is invalid")
    original_by_id = {str(thread.get("id")): thread for thread in original}
    live_by_id = {str(thread.get("id")): thread for thread in live_threads}
    if (
        len(original_by_id) != len(original)
        or len(live_by_id) != len(live_threads)
        or set(original_by_id) != set(live_by_id)
    ):
        raise ValueError("remediation journal thread snapshot is invalid")
    replies = dict(reply_result.replies)
    prior_receipts = (
        {
            str(receipt.get("id") or receipt.get("thread_id") or ""): dict(receipt)
            for receipt in progress.receipts
        }
        if progress is not None
        else {}
    )
    recovered = dict(prior_receipts)
    pending_review_ids: set[str] = set()
    pull_request_ids: set[str] = set()
    states: set[str] = set()
    for thread_id, original_thread in original_by_id.items():
        live = live_by_id[thread_id]
        original_comments = original_thread.get("comments")
        live_comments = live.get("comments")
        if not isinstance(original_comments, list) or not isinstance(live_comments, list):
            raise ValueError("remediation journal thread snapshot is invalid")
        original_prefix = _canonical_comment_prefix(original_comments)
        pull_request_id = live.get("pr_node_id")
        if isinstance(pull_request_id, str) and pull_request_id:
            pull_request_ids.add(pull_request_id)
        if thread_id in recovered:
            if (
                len(live_comments) != len(original_comments) + 1
                or _canonical_comment_prefix(live_comments[:-1]) != original_prefix
            ):
                raise ValueError("remediation journal thread snapshot is invalid")
            final = live_comments[-1]
            prior = recovered[thread_id]
            if (
                not isinstance(final, dict)
                or final.get("id") != prior.get("implementation_reply_id")
                or final.get("body") != prior.get("implementation_reply_body")
                or final.get("viewer_did_author") is not True
                or final.get("review_commit_sha") != review_input.recovery_commit_sha
                or final.get("review_state") not in {"PENDING", "COMMENTED"}
            ):
                raise ValueError("remediation journal thread snapshot is invalid")
            recovered[thread_id] = {
                **live,
                "implementation_reply_id": prior["implementation_reply_id"],
                "implementation_reply_body": prior["implementation_reply_body"],
                "implementation_head_sha": review_input.recovery_commit_sha,
            }
            state = str(final["review_state"])
            review_id = final.get("review_id")
            states.add(state)
            if state == "PENDING" and isinstance(review_id, str) and review_id:
                pending_review_ids.add(review_id)
            continue
        reply = replies.get(thread_id)
        if not isinstance(reply, str):
            continue
        if len(live_comments) == len(original_comments) and (
            _canonical_comment_prefix(live_comments) == original_prefix
        ):
            continue
        if (
            len(live_comments) != len(original_comments) + 1
            or _canonical_comment_prefix(live_comments[:-1]) != original_prefix
        ):
            raise ValueError("remediation journal thread snapshot is invalid")
        final = live_comments[-1]
        expected_body = _implementation_reply_body(
            review_input.repository,
            review_input.pr_number,
            review_input.recovery_commit_sha,
            thread_id,
            reply,
            batch_nonce,
        )
        if (
            not isinstance(final, dict)
            or final.get("body") != expected_body
            or final.get("viewer_did_author") is not True
            or not isinstance(final.get("id"), str)
            or not final.get("id")
            or not isinstance(final.get("review_id"), str)
            or not final.get("review_id")
            or final.get("review_state") not in {"PENDING", "COMMENTED"}
            or final.get("review_commit_sha") != review_input.recovery_commit_sha
        ):
            raise ValueError("remediation journal thread snapshot is invalid")
        receipt = {
            **live,
            "implementation_reply_id": final["id"],
            "implementation_reply_body": expected_body,
            "implementation_head_sha": review_input.recovery_commit_sha,
        }
        recovered[thread_id] = receipt
        states.add(str(final["review_state"]))
        if final["review_state"] == "PENDING":
            pending_review_ids.add(str(final["review_id"]))
    if len(pull_request_ids) != 1 or len(pending_review_ids) > 1 or not recovered:
        raise ValueError("remediation journal thread snapshot is invalid")
    ordered_ids = tuple(sorted(recovered))
    inferred = ImplementationReplyProgress(
        phase=(
            "verify_submission"
            if states == {"COMMENTED"}
            else "submit_review"
            if set(replies).issubset(recovered)
            else "post_replies"
        ),
        pull_request_id=next(iter(pull_request_ids)),
        pending_review_id=next(iter(pending_review_ids), None),
        replied_thread_ids=ordered_ids,
        receipts=tuple(recovered[thread_id] for thread_id in ordered_ids),
    )
    if not _progress_is_monotonic(progress, inferred) or not _progress_matches_live_threads(
        review_input,
        reply_result,
        batch_nonce,
        inferred,
        live_threads,
    ):
        raise ValueError("remediation journal thread snapshot is invalid")
    return inferred


def _progress_is_monotonic(
    previous: ImplementationReplyProgress | None,
    current: ImplementationReplyProgress | None,
) -> bool:
    """Return whether one successor retains all proved reply receipts."""
    if current is None:
        return False
    current_ids_tuple = current.replied_thread_ids
    current_receipts_by_id = {
        str(receipt.get("id") or receipt.get("thread_id") or ""): receipt
        for receipt in current.receipts
    }
    if (
        len(set(current_ids_tuple)) != len(current_ids_tuple)
        or len(current.receipts) != len(current_ids_tuple)
        or set(current_receipts_by_id) != set(current_ids_tuple)
        or "" in current_receipts_by_id
    ):
        return False
    if previous is None:
        return True
    previous_ids = set(previous.replied_thread_ids)
    current_ids = set(current_ids_tuple)
    if not previous_ids.issubset(current_ids):
        return False
    previous_receipts = {
        str(receipt.get("id") or receipt.get("thread_id") or ""): receipt
        for receipt in previous.receipts
    }
    identity_fields = (
        "implementation_reply_id",
        "implementation_reply_body",
        "implementation_head_sha",
    )
    return all(
        isinstance(current_receipts_by_id.get(thread_id), dict)
        and all(
            current_receipts_by_id[thread_id].get(field) == receipt.get(field)
            for field in identity_fields
        )
        for thread_id, receipt in previous_receipts.items()
    )


def _unchanged_head_batch_is_disjoint(
    comment: IssueComment, match: re.Match[str], threads: list[dict[str, Any]]
) -> bool:
    """Exclude only a valid ordinary batch with no current thread IDs."""
    if (
        not threads
        or _thread_snapshot_fingerprint(threads) is None
        or any(any(char.isspace() or ord(char) < 32 for char in thread["id"]) for thread in threads)
    ):
        raise ValueError("unchanged-head reply journal current threads are incomplete")
    _, separator, encoded = comment.body.lstrip().partition("\n")
    if (
        len(comment.body.encode("utf-8")) > REMEDIATION_JOURNAL_COMMENT_MAX_BYTES
        or not separator
        or not encoded.startswith("<!-- ")
        or not encoded.endswith(" -->")
    ):
        raise ValueError("unchanged-head reply journal schema is malformed")
    payload = _strict_json_loads(encoded.removeprefix("<!-- ").removesuffix(" -->"))
    required = {
        "format",
        "pr_number",
        "head_sha",
        "batch_nonce",
        "thread_snapshot_sha256",
        "replies",
    }
    if not isinstance(payload, dict) or type(payload.get("format")) is not int:
        raise ValueError("unchanged-head reply journal format is invalid")
    required.add("armed")
    if (
        payload["format"] != 2
        or set(payload) not in (required, required | {"progress"})
        or payload.get("armed") is not True
        or type(payload.get("pr_number")) is not int
        or payload["pr_number"] != int(match.group("pr"))
        or payload.get("head_sha") != match.group("head")
        or payload.get("batch_nonce") != match.group("batch")
        or not isinstance(payload.get("thread_snapshot_sha256"), str)
        or _SHA256_RE.fullmatch(payload["thread_snapshot_sha256"]) is None
    ):
        raise ValueError("unchanged-head reply journal identity is invalid")
    replies = payload["replies"]
    if (
        not isinstance(replies, dict)
        or not replies
        or any(
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or any(char.isspace() or ord(char) < 32 for char in key)
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_ADDRESS_REPLY_CHARS
            for key, value in replies.items()
        )
    ):
        raise ValueError("unchanged-head reply journal replies are invalid")
    if "progress" in payload:
        progress = ImplementationReplyProgress.from_dict(payload["progress"])
        if (
            progress is None
            or not set(payload["progress"]).issubset(progress.as_dict())
            or not _progress_is_monotonic(None, progress)
            or not set(progress.replied_thread_ids).issubset(replies)
            or (progress.active_thread_id is not None and progress.active_thread_id not in replies)
            or any(
                not isinstance(receipt[key], str)
                or not receipt[key]
                or any(char.isspace() or ord(char) < 32 for char in receipt[key])
                or receipt[key] not in replies
                for receipt in progress.receipts
                for key in ("id", "thread_id")
                if key in receipt
            )
        ):
            raise ValueError("unchanged-head reply journal progress is invalid")
    return set(replies).isdisjoint(thread["id"] for thread in threads)


def _remediation_handoff_from_comment(
    comment: IssueComment,
    *,
    repository: str,
    issue_number: int,
    pr_number: int,
    branch: str,
    current_remote_head: str,
    threads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Parse and validate one actor-owned format-3 journal comment."""
    marker, separator, encoded_payload = comment.body.lstrip().partition("\n")
    marker_match = _REMEDIATION_HANDOFF_JOURNAL_RE.fullmatch(marker)
    if marker_match is None:
        unchanged_head_match = _HANDOFF_JOURNAL_RE.fullmatch(marker)
        if unchanged_head_match is not None and (
            int(unchanged_head_match.group("pr")) == pr_number
            and unchanged_head_match.group("head") == current_remote_head
            and not _unchanged_head_batch_is_disjoint(comment, unchanged_head_match, threads)
        ):
            raise ValueError("unchanged-head reply journal cannot recover remediation")
        return None
    if int(marker_match.group("pr")) != pr_number:
        raise ValueError("remediation journal PR identity is invalid")
    if not (separator and encoded_payload.startswith("<!-- ") and encoded_payload.endswith(" -->")):
        raise ValueError("remediation journal schema is malformed")
    payload = _strict_json_loads(encoded_payload.removeprefix("<!-- ").removesuffix(" -->"))
    required = {
        "format",
        "kind",
        "armed",
        "repository",
        "issue_number",
        "pr_number",
        "head_sha",
        "branch",
        "batch_nonce",
        "review_input_encoding",
        "review_input_data",
        "review_input_sha256",
        "reply_result",
        "reply_result_sha256",
        "journal_sequence",
        "journal_predecessor_sha256",
    }
    payload_keys = set(payload) if isinstance(payload, dict) else set()
    if not isinstance(payload, dict) or (
        payload_keys != required and payload_keys != required | {"progress"}
    ):
        raise ValueError("remediation journal schema is invalid")
    if (
        payload.get("format") != 3
        or payload.get("kind") != "remediation-recovery"
        or payload.get("armed") is not True
    ):
        raise ValueError("remediation journal schema is invalid")
    encoding = payload.get("review_input_encoding")
    data = payload.get("review_input_data")
    review_input = RemediationReviewInput.from_canonical_bytes(
        decode_remediation_review_input(encoding, data)
    )
    if payload.get("review_input_sha256") != review_input.review_input_sha256:
        raise ValueError("remediation journal review-input digest is invalid")
    identities = {
        "repository": repository,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "branch": branch,
        "head_sha": current_remote_head,
    }
    if any(payload.get(key) != value for key, value in identities.items()) or any(
        getattr(review_input, key) != value
        for key, value in {
            "repository": repository,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "branch": branch,
            "recovery_commit_sha": current_remote_head,
        }.items()
    ):
        raise ValueError("remediation journal repository or publication identity is invalid")
    reply_result = RemediationReplyResult.from_dict(payload.get("reply_result"))
    if (
        reply_result.review_input_sha256 != review_input.review_input_sha256
        or payload.get("reply_result_sha256") != reply_result.digest
    ):
        raise ValueError("remediation journal reply-result digest is invalid")
    progress = (
        ImplementationReplyProgress.from_dict(payload.get("progress"))
        if "progress" in payload
        else None
    )
    if "progress" in payload and progress is None:
        raise ValueError("remediation journal progress is invalid")
    sequence = payload.get("journal_sequence")
    predecessor = payload.get("journal_predecessor_sha256")
    handoff = implementation_remediation_reply_handoff(
        review_input,
        reply_result,
        payload.get("batch_nonce"),
        progress=progress,
        journal_input_encoding=encoding,
        journal_input_data=data,
        journal_sequence=sequence,
        journal_predecessor_sha256=predecessor,
    )
    if (
        handoff is None
        or handoff["head_sha"] != marker_match.group("head")
        or handoff["batch_nonce"] != marker_match.group("batch")
        or handoff["journal_sequence"] != int(marker_match.group("sequence"))
    ):
        raise ValueError("remediation journal identity is invalid")
    handoff["reconciliation_only"] = False
    handoff["journal_comment_sha256"] = sha256(comment.body.encode("utf-8")).hexdigest()
    return handoff


def journaled_implementation_remediation_reply_handoff(  # noqa: C901
    comments: Sequence[IssueComment],
    *,
    repository: str,
    issue_number: int,
    pr_number: int,
    branch: str,
    current_remote_head: str,
    threads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Recover one format-3 remediation-only handoff."""
    if (
        not isinstance(repository, str)
        or not repository
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number <= 0
        or not isinstance(branch, str)
        or not branch
        or _FULL_COMMIT_SHA_RE.fullmatch(current_remote_head) is None
        or not isinstance(threads, list)
    ):
        raise ValueError("remediation journal recovery identity is invalid")
    candidates: list[dict[str, Any]] = []
    for comment in comments:
        if not comment.viewer_did_author:
            continue
        marker = comment.body.lstrip().partition("\n")[0]
        marker_match = _REMEDIATION_HANDOFF_JOURNAL_RE.fullmatch(marker)
        if marker_match is not None and marker_match.group("head") != current_remote_head:
            # A PR can contain an immutable journal for each remediation
            # commit. Only the journal for the current remote head is a
            # recovery candidate.
            continue
        candidate = _remediation_handoff_from_comment(
            comment,
            repository=repository,
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            current_remote_head=current_remote_head,
            threads=threads,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None

    first = candidates[0]
    authority_keys = (
        "repository",
        "issue_number",
        "pr_number",
        "branch",
        "head_sha",
        "batch_nonce",
        "review_input_sha256",
        "reply_result_sha256",
    )
    if any(
        any(candidate.get(key) != first.get(key) for key in authority_keys)
        for candidate in candidates[1:]
    ):
        raise ValueError("remediation journals for the current head are ambiguous")

    previous_progress: ImplementationReplyProgress | None = None
    previous_digest: str | None = None
    for expected_sequence, candidate in enumerate(candidates):
        if (
            candidate.get("journal_sequence") != expected_sequence
            or candidate.get("journal_predecessor_sha256") != previous_digest
        ):
            raise ValueError("remediation journal successor chain is invalid")
        progress = (
            ImplementationReplyProgress.from_dict(candidate.get("progress"))
            if "progress" in candidate
            else None
        )
        if expected_sequence > 0 and not _progress_is_monotonic(previous_progress, progress):
            raise ValueError("remediation journal successor progress is invalid")
        previous_progress = progress
        previous_digest = candidate.get("journal_comment_sha256")

    recovered = candidates[-1]
    try:
        review_input = RemediationReviewInput.from_canonical_bytes(
            recovered["review_input_bytes"].encode("utf-8")
        )
        reply_result = RemediationReplyResult.from_dict(recovered["reply_result"])
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("remediation journal recovery payload is invalid") from error
    recovered_progress = _recover_live_progress(
        review_input,
        reply_result,
        recovered["batch_nonce"],
        previous_progress,
        threads,
    )
    if recovered_progress is not None:
        recovered["progress"] = recovered_progress.as_dict()
    recovered["recover_pending_review"] = True
    recovered.pop("journal_comment_sha256", None)
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


def _remediation_successor_handoff(
    handoff: object,
    progress: ImplementationReplyProgress,
) -> dict[str, Any] | None:
    """Create one linked format-3 successor for newly proved progress."""
    if not isinstance(handoff, dict) or handoff.get("format") != 3:
        return None
    current_entry = implementation_remediation_reply_handoff_journal_entry(
        handoff.get("pr_number"), handoff
    )
    try:
        review_input = RemediationReviewInput.from_canonical_bytes(
            handoff["review_input_bytes"].encode("utf-8")
        )
        reply_result = RemediationReplyResult.from_dict(handoff["reply_result"])
        sequence = handoff["journal_sequence"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    previous_progress = (
        ImplementationReplyProgress.from_dict(handoff.get("progress"))
        if "progress" in handoff
        else None
    )
    if previous_progress == progress:
        retained = deepcopy(handoff)
        retained["reconciliation_only"] = False
        return retained
    if (
        current_entry is None
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not _progress_is_monotonic(previous_progress, progress)
    ):
        return None
    successor = implementation_remediation_reply_handoff(
        review_input,
        reply_result,
        handoff.get("batch_nonce"),
        progress=progress,
        journal_input_encoding=handoff.get("review_input_encoding"),
        journal_input_data=handoff.get("review_input_data"),
        journal_sequence=sequence + 1,
        journal_predecessor_sha256=sha256(current_entry[1].encode("utf-8")).hexdigest(),
    )
    if successor is not None:
        successor["reconciliation_only"] = False
    return successor


def _consume_reply_post_result(  # noqa: C901
    payload: dict[str, Any],
    *,
    result: object,
    head_sha: str,
    threads: list[dict[str, Any]],
    replies: dict[str, str],
    batch_nonce: str,
    source_handoff: object,
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
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
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
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
        return "stale"
    progress = getattr(result, "progress", None)
    if bool(getattr(result, "retryable", False)) and isinstance(
        progress, ImplementationReplyProgress
    ):
        replacement = (
            _remediation_successor_handoff(source_handoff, progress)
            if isinstance(source_handoff, dict) and source_handoff.get("format") == 3
            else implementation_reply_handoff(
                head_sha,
                threads,
                replies,
                batch_nonce,
                progress=progress,
                reconciliation_only=False,
            )
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
        payload.pop(PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
        return "stale"
    if not replied.issubset(expected_ids) or len(receipts) != len(replied):
        return "invalid"
    if isinstance(source_handoff, dict) and source_handoff.get("format") == 3:
        if replied:
            return "invalid"
        retained = deepcopy(source_handoff)
        retained["reconciliation_only"] = False
        payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF] = retained
        return "retry"
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
    if isinstance(raw_handoff, dict) and raw_handoff.get("format") == 3:
        try:
            review_input_bytes = raw_handoff.get("review_input_bytes")
            if not isinstance(review_input_bytes, str):
                raise ValueError("review input is unavailable")
            review_input = RemediationReviewInput.from_canonical_bytes(
                review_input_bytes.encode("utf-8")
            )
            reply_result = RemediationReplyResult.from_dict(raw_handoff.get("reply_result"))
            progress = (
                ImplementationReplyProgress.from_dict(raw_handoff.get("progress"))
                if "progress" in raw_handoff
                else None
            )
            handoff = implementation_remediation_reply_handoff(
                review_input,
                reply_result,
                raw_handoff.get("batch_nonce"),
                progress=progress,
                journal_input_encoding=raw_handoff.get("review_input_encoding"),
                journal_input_data=raw_handoff.get("review_input_data"),
                journal_sequence=raw_handoff.get("journal_sequence"),
                journal_predecessor_sha256=raw_handoff.get("journal_predecessor_sha256"),
            )
        except (AttributeError, TypeError, UnicodeError, ValueError):
            return "invalid"
        exact_keys = (
            "format",
            "kind",
            "repository",
            "issue_number",
            "pr_number",
            "branch",
            "head_sha",
            "threads",
            "replies",
            "batch_nonce",
            "review_input_bytes",
            "review_input_sha256",
            "review_input_encoding",
            "review_input_data",
            "reply_result",
            "reply_result_sha256",
            "journal_sequence",
            "journal_predecessor_sha256",
        )
        if (
            handoff is None
            or issue_number != review_input.issue_number
            or pr_number != review_input.pr_number
            or any(handoff.get(key) != raw_handoff.get(key) for key in exact_keys)
            or (
                "progress" in raw_handoff and handoff.get("progress") != raw_handoff.get("progress")
            )
        ):
            return "invalid"
    else:
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
        if isinstance(raw_handoff, dict) and raw_handoff.get("recover_pending_review") is True:
            delivery_kwargs["recover_pending_review"] = True
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
        source_handoff=raw_handoff,
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
