"""Shared contracts for detached PR review and thread validation.

The review stage owns review checkouts, structural audits, and thread
validation. The implementation queue owns all source changes and reply
publication. A clean audit can grant GO only for its current-process reviewed
head after complete live thread and label checks. Merge wait verifies the
same head and the required server policy before each merge request.

Only valid structural audits use the review budget. Host check failures,
malformed audits, and scope dependencies use their bounded queue paths.
"""

# This module intentionally re-exports the stage's shared imported namespace.

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import typing as _typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from hephaestus.automation.agent_config import (
    pr_reviewer_claude_timeout,
    reviewer_model,
)
from hephaestus.automation.issue_waves import is_full_commit_sha
from hephaestus.automation.prompts.pr_review import (
    BLOCKING_SEVERITIES,
    VALID_SEVERITIES,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)
from hephaestus.automation.review_audit import (
    ReviewAudit,
    has_reserved_finding_control,
    parse_review_audit,
)
from hephaestus.automation.session_naming import (
    AGENT_PR_REVIEWER,
)
from hephaestus.automation.state_labels import STATE_SKIP

from ..scope_retraction import scope_retraction_paths_for_threads
from ..work_item import ItemKind
from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
    BuildTestJob,
    CompactJob,
    Continue,
    Disposition,
    GitJob,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _is_confirmed_open_unarmed,
    _worktree_path,
    agent_provider,
    stage_model,
    stage_timeout,
    write_skip_label,
)
from .pr_review_receipts import (
    UNSUPPORTED_HOST_VERIFICATION_ERROR,
    _host_verification_failure_kind,
    _host_verification_receipt_matches,
    _host_verification_result_status,
)
from .pr_review_repository import (
    _payload_host_verification_specs,
    _prepare_host_checks,
)
from .pr_review_verification import (
    HOST_VERIFICATION_DIAGNOSTIC_MAX,
    HOST_VERIFICATION_TIMEOUT_S,
    _host_verification_specs,
    _HostVerificationSpec,
)

logger = logging.getLogger(__name__)


def _host_verification_receipts_match(
    receipts: object,
    specs: tuple[_HostVerificationSpec, ...],
    reviewed_head: str,
) -> bool:
    """Return whether every required fixed check has a successful exact-head receipt."""
    return bool(
        isinstance(receipts, list)
        and len(receipts) == len(specs)
        and all(
            _host_verification_receipt_matches(receipt, spec, reviewed_head)
            for receipt, spec in zip(receipts, specs, strict=True)
        )
    )


_JSON_RESPONSE_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _scope_retraction_paths(threads: list[dict[str, Any]]) -> tuple[str, ...] | None:
    """Extract host-enforced retractions from explicit scope-control findings."""
    return scope_retraction_paths_for_threads(threads)


@dataclass(frozen=True)
class _ParsedReviewResponse:
    """Structural reviewer audit parsed inside the worker."""

    audit: ReviewAudit


def _parse_review_response(response: str) -> _ParsedReviewResponse:
    """Parse the review's strict structural audit without reading prose decisions."""
    return _ParsedReviewResponse(parse_review_audit(response))


# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
SCOPE_DEPENDENCY_WAIT = "SCOPE_DEPENDENCY_WAIT"
ADOPT_WORKTREE_WAIT = "ADOPT_WORKTREE_WAIT"
REVIEW_WAIT = "REVIEW_WAIT"
REVIEW_CHECKOUT_WAIT = "REVIEW_CHECKOUT_WAIT"
HOST_VERIFICATION_WAIT = "HOST_VERIFICATION_WAIT"
VALIDATE_WAIT = "VALIDATE_WAIT"
POST = "POST"
SCOPE_EXPANSION_PREPARE_SUBMIT = "SCOPE_EXPANSION_PREPARE_SUBMIT"
POST_APPLY = "POST_APPLY"
EVAL = "EVAL"
GO_AUDIT_RECEIPT = "GO_AUDIT_RECEIPT"
GO_AUDIT_PUBLISH = "GO_AUDIT_PUBLISH"
COMPACT_REVIEWER_WAIT = "COMPACT_REVIEWER_WAIT"
CLEANUP_REVIEW_WORKTREE_WAIT = "CLEANUP_REVIEW_WORKTREE_WAIT"

# Public audit publication is a receipt-backed external reconciliation, not a
# review round. Three delayed attempts (1s, 2s, 4s) bound one coordinator run;
# the durable receipt lets a later run resume without another reviewer call.
IMPLEMENTATION_GO_AUDIT_RETRY_CAP = 3

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    SCOPE_DEPENDENCY_WAIT: "_scope_dependency_wait",
    ADOPT_WORKTREE_WAIT: "_adopt_worktree_wait",
    REVIEW_WAIT: "_review_wait",
    REVIEW_CHECKOUT_WAIT: "_review_checkout_wait",
    HOST_VERIFICATION_WAIT: "_host_verification_wait",
    VALIDATE_WAIT: "_validate_wait",
    POST: "_post",
    SCOPE_EXPANSION_PREPARE_SUBMIT: "_scope_expansion_prepare_submit",
    POST_APPLY: "_post_apply",
    EVAL: "_eval",
    GO_AUDIT_RECEIPT: "_go_audit_receipt",
    GO_AUDIT_PUBLISH: "_go_audit_publish",
    COMPACT_REVIEWER_WAIT: "_compact_reviewer_wait",
    CLEANUP_REVIEW_WORKTREE_WAIT: "_cleanup_review_worktree_wait",
}


def _issue_number(item: WorkItem) -> int:
    """Return the issue number after the stage-level guard has run."""
    if item.issue is None:
        raise RuntimeError("pr_review stage reached without an issue number")
    return item.issue


def _review_context_kind(item: WorkItem) -> str:
    """Return the prompt-facing numeric context kind for this review item."""
    return "PR" if item.payload.get("review_context_kind") == "PR" else "issue"


#: Max CONSECUTIVE reviewer-infrastructure failures (malformed audits or
#: failed/valueless review jobs) tolerated before failing back
#: ``agent_error``. Bounds the in-stage ERROR retry loop without burning
#: ``pr_review_iter`` or stamping labels (#911/#1554; mirrors
#: plan_review.REVIEW_ERROR_RETRY_CAP). Reset whenever a valid audit arrives.
REVIEW_ERROR_RETRY_CAP = 2
REVIEW_CHECKOUT_RETRY_CAP = 2

_HOST_VERIFICATION_PENDING = "host_verification_pending"
_COMMENT_VALIDATION_ONLY = "reviewer_comment_validation_only"


#: Round-scoped payload keys cleared at REVIEW_WAIT submission so a failed
#: later round can never replay an earlier round's results.
_ROUND_PAYLOAD_KEYS = (
    "review_audit",
    "review_feedback",
    "review_text",
    "review_failed",
    "validation_result",
    "review_threads",
    "raw_review_threads",
    "posted_thread_ids",
    "remediation_threads",
    "remediation_thread_snapshots",
    "unaddressed_findings",
    "review_audit_failure",
    "prior_comments_json",
    "validation_threads",
    "validation_receipt_fingerprints",
    "validation_pr_metadata_fingerprint",
    "scope_retraction_paths",
    "_scope_expansion_pending_request",
    "_scope_expansion_receipt",
    "_scope_expansion_receipt_error",
    "_scope_expansion_prepared_receipt",
    "reviewed_pr_base_sha",
    "host_verification_receipts",
    "host_verification_repository_profile",
    "host_verification_failure",
    _HOST_VERIFICATION_PENDING,
)


def _clear_round_review_state(item: WorkItem) -> None:
    """Discard review evidence that cannot survive a head-changing commit."""
    for key in _ROUND_PAYLOAD_KEYS:
        item.payload.pop(key, None)
    item.payload.pop("reviewed_pr_head_sha", None)
    item.payload.pop("reviewed_pr_node_id", None)
    item.payload.pop("pr_node_id", None)
    item.payload.pop("pr_diff", None)
    item.payload.pop("review_changed_paths", None)


def _parse_validation_result(raw: Any) -> dict[str, Any] | None:
    """Parse the validator job's output into its verdict dict, tolerantly.

    The validation prompt asks for a single fenced JSON block at the END of
    the response (``{"resolved": [...], "unaddressed": [...]}``). When a
    response contains fenced blocks, only its complete final block can be a
    verdict; an earlier valid block must never authorize a malformed later
    answer. An unfenced response must be exactly one JSON object. Returns None
    when the final verdict does not parse — callers fail closed.

    Return the parsed verdict dict, or None if parsing fails.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    blocks = list(re.finditer(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL))
    if blocks:
        final = blocks[-1]
        if raw[final.end() :].strip():
            return None
        candidate = final.group(1)
    else:
        candidate = raw
    try:
        parsed = json.loads(candidate.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _thread_ids(entries: Any) -> set[str]:
    """Collect the ``thread_id``/``id`` strings from a validator bucket."""
    ids: set[str] = set()
    if not isinstance(entries, list):
        return ids
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        thread_id = entry.get("thread_id") or entry.get("id")
        if thread_id:
            ids.add(str(thread_id))
    return ids


def _is_postable_finding(thread: dict[str, Any]) -> bool:
    """Return whether a structured finding has a durable inline-thread shape."""
    return (
        isinstance(thread.get("path"), str)
        and bool(thread["path"].strip())
        and isinstance(thread.get("line"), int)
        and not isinstance(thread.get("line"), bool)
        and thread["line"] > 0
        and thread.get("side") == "RIGHT"
        and str(thread.get("severity", "")).lower() in VALID_SEVERITIES
        and isinstance(thread.get("body"), str)
        and bool(thread["body"].strip())
        and not has_reserved_finding_control(thread["body"])
    )


def _durable_thread_id(thread: dict[str, Any]) -> str | None:
    """Return one non-empty durable GraphQL thread id, if present."""
    value = thread.get("id") or thread.get("thread_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _finding_key(thread: dict[str, Any]) -> tuple[str, int, str, str] | None:
    """Build a stable key for one inline finding without trusting an agent id."""
    path = thread.get("path")
    line = thread.get("line")
    side = thread.get("side")
    body = thread.get("body")
    if (
        not isinstance(path, str)
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line <= 0
        or not isinstance(side, str)
        or not isinstance(body, str)
    ):
        return None
    body = "\n".join(
        line_text
        for line_text in body.splitlines()
        if not line_text.strip().startswith("<!-- hephaestus-severity:")
    )
    # The visible role marker distinguishes the original reviewer in GitHub's
    # conversation but must not make an otherwise identical finding look new.
    body = re.sub(r"^\[Review\]\s*", "", body)
    body = re.sub(r"^Reopened \(prior round, still unaddressed\):\s*", "", body).strip()
    return (path.strip(), line, side.strip().upper(), re.sub(r"\s+", " ", body).casefold())


def _without_duplicate_live_findings(
    findings: list[dict[str, Any]], live_threads: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep genuinely new audit findings while retaining their open-thread twins."""
    existing = {
        key for thread in live_threads.values() if (key := _finding_key(thread)) is not None
    }
    retained: list[dict[str, Any]] = []
    for finding in findings:
        prior_thread_id = finding.get("prior_thread_id")
        if isinstance(prior_thread_id, str) and prior_thread_id in live_threads:
            continue
        key = _finding_key(finding)
        if key is not None and key in existing:
            continue
        retained.append(finding)
        if key is not None:
            existing.add(key)
    return retained


def _normalize_remediation_threads(
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize every live review thread for implementation remediation.

    The reviewer audit contains proposed findings, not durable GitHub thread
    identities. Address jobs must instead consume the live post/read-back
    snapshot so every open thread—regardless of author—is investigated.  The
    implementation agent replies against the verified current head; the
    reviewer later performs a fresh review and resolves or returns the exact
    thread. No-commit replies are explicitly marked for thorough analysis.
    """
    normalized: list[dict[str, Any]] = []
    for thread in threads:
        thread_id = str(thread.get("id") or thread.get("thread_id") or "").strip()
        if not thread_id:
            continue
        line = thread.get("line")
        body = str(thread.get("body") or "")
        comments = thread.get("comments")
        # The first thread body is already supplied above.  Once a reviewer
        # leaves a follow-up, retain the entire conversation in the next
        # implementer prompt so the agent can act on the precise remaining
        # defect rather than trying the original fix again.
        if isinstance(comments, list) and len(comments) > 1:
            rendered_comments: list[str] = []
            for comment in comments[1:]:
                if not isinstance(comment, dict):
                    continue
                author = str(comment.get("author") or "unknown reviewer").strip()
                comment_body = str(comment.get("body") or "").strip()
                if comment_body:
                    rendered_comments.append(f"{author}: {comment_body}")
            if rendered_comments:
                body = f"{body}\n\nThread conversation:\n" + "\n\n".join(rendered_comments)
        normalized.append(
            {
                "thread_id": thread_id,
                "path": str(thread.get("path") or ""),
                "line": (
                    line
                    if isinstance(line, int) and not isinstance(line, bool) and line > 0
                    else None
                ),
                "body": body,
            }
        )
    return normalized


def _validation_thread_snapshots(
    live_threads: list[dict[str, Any]], receipts: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Decorate every open thread for reviewer validation.

    The reviewer always receives the full open-thread snapshot.  Only the
    subset with a host-verified implementation reply on the reviewed head is
    eligible for a resolve or reviewer-feedback mutation in this pass; the
    remainder stays open for the implementation agent.  Receipts come from a
    fresh GitHub read, rather than this process's former work-item payload.
    """
    receipt_by_id: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        thread_id = _durable_thread_id(receipt)
        if thread_id is None or thread_id in receipt_by_id:
            return None
        receipt_by_id[thread_id] = receipt

    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for thread in live_threads:
        if not isinstance(thread, dict):
            return None
        thread_id = _durable_thread_id(thread)
        if thread_id is None or thread_id in seen:
            return None
        seen.add(thread_id)
        snapshot = dict(thread)
        matched_receipt = receipt_by_id.get(thread_id)
        if matched_receipt is not None:
            snapshot["implementation_reply_body"] = matched_receipt["implementation_reply_body"]
            snapshot["implementation_reply_submitted"] = True
        else:
            snapshot["implementation_reply_body"] = None
            snapshot["implementation_reply_submitted"] = False
        snapshots.append(snapshot)

    if not set(receipt_by_id).issubset(seen):
        return None
    return snapshots


def _validation_receipt_fingerprints(
    receipts: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Bind a validator decision to the exact host-read reply receipt.

    A thread ID alone is not an immutable decision target: another reply can
    be appended on the same thread and head while the validation job is
    running.  Store a canonical digest of the complete comment receipt that
    the reviewer was shown, then require an identical fresh receipt before
    consuming its decision in ``POST``.
    """
    fingerprints: dict[str, str] = {}
    for receipt in receipts:
        thread_id = _durable_thread_id(receipt)
        comments = receipt.get("comments") if isinstance(receipt, dict) else None
        reply_id = receipt.get("implementation_reply_id") if isinstance(receipt, dict) else None
        reply_body = receipt.get("implementation_reply_body") if isinstance(receipt, dict) else None
        head_sha = receipt.get("implementation_head_sha") if isinstance(receipt, dict) else None
        if (
            thread_id is None
            or thread_id in fingerprints
            or not isinstance(comments, list)
            or not isinstance(reply_id, str)
            or not isinstance(reply_body, str)
            or not is_full_commit_sha(head_sha)
        ):
            return None
        snapshot: list[tuple[str, str]] = []
        seen_comment_ids: set[str] = set()
        for comment in comments:
            if not isinstance(comment, dict):
                return None
            comment_id = comment.get("id")
            body = comment.get("body")
            if (
                not isinstance(comment_id, str)
                or not comment_id
                or comment_id in seen_comment_ids
                or not isinstance(body, str)
            ):
                return None
            seen_comment_ids.add(comment_id)
            snapshot.append((comment_id, body))
        if not snapshot or snapshot[-1] != (reply_id, reply_body):
            return None
        canonical = json.dumps(
            {
                "comments": snapshot,
                "head_sha": head_sha,
                "implementation_reply_body": reply_body,
                "implementation_reply_id": reply_id,
                "thread_id": thread_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprints[thread_id] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return fingerprints


def _validation_pr_metadata_fingerprint(
    pr_context: dict[str, str] | None, reviewed_head: str
) -> str | None:
    """Bind a validator decision to the exact live title/body it reviewed."""
    if (
        pr_context is None
        or pr_context.get("pr_head_sha") != reviewed_head
        or not isinstance(pr_context.get("pr_title"), str)
        or not isinstance(pr_context.get("pr_description"), str)
    ):
        return None
    canonical = json.dumps(
        {
            "head_sha": reviewed_head,
            "pr_description": pr_context["pr_description"],
            "pr_title": pr_context["pr_title"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reviewer_thread_decisions(  # noqa: C901
    receipts: list[dict[str, Any]], validation_result: Any
) -> tuple[set[str], dict[str, str]] | None:
    """Return reviewer-approved IDs and rejection explanations, fail closed."""
    parsed = _parse_validation_result(validation_result)
    if parsed is None:
        return None
    # Resolution is authority-bearing.  Require the reviewer to make an
    # explicit, exhaustive two-way decision for every host-read receipt;
    # omission and a historical ``wont_fix`` bucket must never silently close
    # a discussion.
    if set(parsed) != {"resolved", "unaddressed"}:
        return None
    resolved = parsed.get("resolved")
    unaddressed = parsed.get("unaddressed")
    if not isinstance(resolved, list) or not isinstance(unaddressed, list):
        return None
    known_ids: set[str] = set()
    for receipt in receipts:
        thread_id = _durable_thread_id(receipt)
        if thread_id is None:
            return None
        known_ids.add(thread_id)
    if len(known_ids) != len(receipts):
        return None
    resolved_ids: set[str] = set()
    for thread_id in resolved:
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None
        normalized_id = thread_id.strip()
        if normalized_id in resolved_ids:
            return None
        resolved_ids.add(normalized_id)
    feedback: dict[str, str] = {}
    for entry in unaddressed:
        if not isinstance(entry, dict):
            return None
        thread_id = str(entry.get("thread_id") or entry.get("id") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        if not thread_id or not detail or thread_id in feedback:
            return None
        feedback[thread_id] = detail
    feedback_ids = set(feedback)
    if (
        not resolved_ids.issubset(known_ids)
        or not feedback_ids.issubset(known_ids)
        or resolved_ids & feedback_ids
        or resolved_ids | feedback_ids != known_ids
    ):
        return None
    return (resolved_ids, feedback)


if _typing.TYPE_CHECKING:

    class _PrReviewHost(_typing.Protocol):
        """Cross-collaborator methods supplied by the review-stage façade."""

        @staticmethod
        def _require_confirmed_unarmed(pr_number: int, ctx: StageContext) -> StageOutcome | None:
            raise NotImplementedError

        @staticmethod
        def _write_no_go(item: WorkItem, ctx: StageContext) -> StepResult | None:
            raise NotImplementedError

        def _cleanup_review_worktree_then(
            self, item: WorkItem, outcome: StageOutcome
        ) -> StepResult:
            raise NotImplementedError


else:

    class _PrReviewHost:
        """Runtime-empty base for the statically checked host contract."""


# These are internal stage APIs shared by the façade and its collaborators.
__all__ = [
    "ADOPT_WORKTREE_WAIT",
    "AGENT_PR_REVIEWER",
    "BLOCKING_SEVERITIES",
    "CLEANUP_REVIEW_WORKTREE_WAIT",
    "COMPACT_REVIEWER_WAIT",
    "ENTER",
    "EVAL",
    "GIT_JOB_TIMEOUT_S",
    "GO_AUDIT_PUBLISH",
    "GO_AUDIT_RECEIPT",
    "HOST_VERIFICATION_DIAGNOSTIC_MAX",
    "HOST_VERIFICATION_TIMEOUT_S",
    "HOST_VERIFICATION_WAIT",
    "IMPLEMENTATION_GO_AUDIT_RETRY_CAP",
    "POST",
    "REVIEW_CHECKOUT_RETRY_CAP",
    "REVIEW_CHECKOUT_WAIT",
    "REVIEW_ERROR_RETRY_CAP",
    "REVIEW_WAIT",
    "SCOPE_DEPENDENCY_WAIT",
    "SCOPE_EXPANSION_PREPARE_SUBMIT",
    "STATE_SKIP",
    "UNSUPPORTED_HOST_VERIFICATION_ERROR",
    "VALIDATE_WAIT",
    "VALID_SEVERITIES",
    "_COMMENT_VALIDATION_ONLY",
    "_HOST_VERIFICATION_PENDING",
    "_JSON_RESPONSE_BLOCK_RE",
    "_ROUND_PAYLOAD_KEYS",
    "_STEP_HANDLER_NAMES",
    "AgentJob",
    "Any",
    "BuildTestJob",
    "Callable",
    "CompactJob",
    "Continue",
    "Disposition",
    "GitJob",
    "ItemKind",
    "JobRequest",
    "JobResult",
    "ReviewAudit",
    "Stage",
    "StageContext",
    "StageOutcome",
    "StepResult",
    "WorkItem",
    "_HostVerificationSpec",
    "_ParsedReviewResponse",
    "_PrReviewHost",
    "_clear_round_review_state",
    "_durable_thread_id",
    "_finding_key",
    "_host_verification_failure_kind",
    "_host_verification_receipt_matches",
    "_host_verification_receipts_match",
    "_host_verification_result_status",
    "_host_verification_specs",
    "_is_confirmed_open_unarmed",
    "_is_postable_finding",
    "_issue_number",
    "_normalize_remediation_threads",
    "_parse_review_response",
    "_parse_validation_result",
    "_payload_host_verification_specs",
    "_prepare_host_checks",
    "_review_context_kind",
    "_reviewer_thread_decisions",
    "_scope_retraction_paths",
    "_thread_ids",
    "_validation_pr_metadata_fingerprint",
    "_validation_receipt_fingerprints",
    "_validation_thread_snapshots",
    "_without_duplicate_live_findings",
    "_worktree_path",
    "agent_provider",
    "annotations",
    "cast",
    "dataclass",
    "get_pr_review_analysis_prompt",
    "get_review_validation_prompt",
    "has_reserved_finding_control",
    "hashlib",
    "is_full_commit_sha",
    "json",
    "logger",
    "logging",
    "parse_review_audit",
    "pr_reviewer_claude_timeout",
    "re",
    "reviewer_model",
    "scope_retraction_paths_for_threads",
    "secrets",
    "stage_model",
    "stage_timeout",
    "write_skip_label",
]
