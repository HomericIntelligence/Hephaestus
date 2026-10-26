"""Shared names for the PR-review stage.

The review stage reads a review, posts its comments, and reads the unresolved
threads back from GitHub. Any unresolved thread sends the pull request to the
implementation agent, which answers and resolves each one. No unresolved thread
means the pull request is approved. The host keeps no record of its own.
"""

# This module intentionally re-exports the stage's shared imported namespace.

from __future__ import annotations

import json
import logging
import re
import typing as _typing
from dataclasses import dataclass
from typing import Any, cast

from hephaestus.automation.agent_config import (
    AGENT_PR_REVIEWER,
    pr_reviewer_claude_timeout,
    reviewer_model,
)
from hephaestus.automation.issue_waves import is_full_commit_sha
from hephaestus.automation.prompts.pr_review import (
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)
from hephaestus.automation.review_audit import ReviewAudit, parse_review_audit
from hephaestus.automation.state_labels import STATE_SKIP

from ..scope_retraction import scope_retraction_paths_for_threads
from ..work_item import ItemKind
from .base import (
    GIT_JOB_TIMEOUT_S,
    AgentJob,
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
    _worktree_path,
    agent_provider,
    stage_model,
    stage_timeout,
    write_skip_label,
)
from .pr_review_round_state import _ROUND_PAYLOAD_KEYS, _clear_round_review_state

logger = logging.getLogger(__name__)


def _scope_retraction_paths(threads: list[dict[str, Any]]) -> tuple[str, ...] | None:
    """Extract host-enforced retractions from explicit scope-control findings."""
    return scope_retraction_paths_for_threads(threads)


@dataclass(frozen=True)
class _ParsedReviewResponse:
    """The review, read inside the worker."""

    audit: ReviewAudit


def _parse_review_response(response: str) -> _ParsedReviewResponse:
    """Read the review the reviewer returned."""
    return _ParsedReviewResponse(parse_review_audit(response))


# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
ADOPT_WORKTREE_WAIT = "ADOPT_WORKTREE_WAIT"
REVIEW_WAIT = "REVIEW_WAIT"
REVIEW_CHECKOUT_WAIT = "REVIEW_CHECKOUT_WAIT"
VALIDATE_WAIT = "VALIDATE_WAIT"
POST = "POST"
POST_APPLY = "POST_APPLY"
EVAL = "EVAL"
COMPACT_REVIEWER_WAIT = "COMPACT_REVIEWER_WAIT"
CLEANUP_REVIEW_WORKTREE_WAIT = "CLEANUP_REVIEW_WORKTREE_WAIT"

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    ADOPT_WORKTREE_WAIT: "_adopt_worktree_wait",
    REVIEW_WAIT: "_review_wait",
    REVIEW_CHECKOUT_WAIT: "_review_checkout_wait",
    VALIDATE_WAIT: "_validate_wait",
    POST: "_post",
    POST_APPLY: "_post_apply",
    EVAL: "_eval",
    COMPACT_REVIEWER_WAIT: "_compact_reviewer_wait",
    CLEANUP_REVIEW_WORKTREE_WAIT: "_cleanup_review_worktree_wait",
}


def _issue_number(item: WorkItem) -> int:
    """Return the issue number; the stage entry point has already checked it."""
    return cast(int, item.issue)


def _review_context_kind(item: WorkItem) -> str:
    """Return the prompt-facing numeric context kind for this review item."""
    return "PR" if item.payload.get("review_context_kind") == "PR" else "issue"


#: Max consecutive reviewer failures (an unreadable response or a failed review
#: job) before the item fails back ``agent_error``. A failure never burns a
#: review round or changes a label.
REVIEW_ERROR_RETRY_CAP = 2
REVIEW_CHECKOUT_RETRY_CAP = 2

_COMMENT_VALIDATION_ONLY = "reviewer_comment_validation_only"
_THREAD_READ_RETRIES = "review_thread_read_retries"
_REPLY_PREFIX = "[Response] "


def _parse_validation_result(raw: Any) -> dict[str, Any] | None:
    """Read the validator's ``{"resolved": [...], "unaddressed": [...]}`` answer.

    The answer is the last fenced JSON block, or the whole response when it is
    one JSON object. Returns None when no object can be read.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    blocks = list(re.finditer(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL))
    candidate = blocks[-1].group(1) if blocks else raw
    try:
        parsed = json.loads(candidate.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validation_decisions(raw: Any) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return the thread ids the reviewer accepted and the explanation for each other one."""
    parsed = _parse_validation_result(raw)
    if parsed is None:
        return (), {}
    resolved = parsed.get("resolved")
    unaddressed = parsed.get("unaddressed")
    resolved_ids = tuple(
        sorted({str(value).strip() for value in resolved or [] if str(value).strip()})
        if isinstance(resolved, list)
        else ()
    )
    feedback: dict[str, str] = {}
    for entry in unaddressed if isinstance(unaddressed, list) else []:
        if not isinstance(entry, dict):
            continue
        thread_id = str(entry.get("thread_id") or entry.get("id") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        if thread_id and detail and thread_id not in resolved_ids:
            feedback[thread_id] = detail
    return resolved_ids, feedback


def _durable_thread_id(thread: dict[str, Any]) -> str | None:
    """Return one non-empty GraphQL thread id, if present."""
    value = thread.get("id") or thread.get("thread_id")
    return value.strip() or None if isinstance(value, str) else None


def _thread_snapshots(live_threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each open thread by whether the implementation agent already answered it.

    A thread is answered when its last comment starts with ``[Response]``.
    """
    snapshots: list[dict[str, Any]] = []
    for thread in live_threads:
        snapshot = dict(thread)
        comments = thread.get("comments")
        last = comments[-1] if isinstance(comments, list) and comments else None
        body = str(last.get("body") or "") if isinstance(last, dict) else ""
        answered = len(comments or []) > 1 and body.startswith(_REPLY_PREFIX)
        snapshot["implementation_reply_body"] = body if answered else None
        snapshot["implementation_reply_submitted"] = answered
        snapshots.append(snapshot)
    return snapshots


def _normalize_remediation_threads(
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe every open thread for the implementation agent.

    A follow-up on a thread keeps the whole conversation, so the agent acts on
    what is still wrong instead of repeating the first fix.
    """
    normalized: list[dict[str, Any]] = []
    for thread in threads:
        thread_id = _durable_thread_id(thread)
        if thread_id is None:
            continue
        line = thread.get("line")
        body = str(thread.get("body") or "")
        comments = thread.get("comments")
        if isinstance(comments, list) and len(comments) > 1:
            follow_ups = [
                f"{str(comment.get('author') or 'unknown reviewer').strip()}: "
                f"{str(comment.get('body') or '').strip()}"
                for comment in comments[1:]
                if isinstance(comment, dict) and str(comment.get("body") or "").strip()
            ]
            if follow_ups:
                body = f"{body}\n\nThread conversation:\n" + "\n\n".join(follow_ups)
        normalized.append(
            {
                "thread_id": thread_id,
                "path": str(thread.get("path") or ""),
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "body": body,
            }
        )
    return normalized


if _typing.TYPE_CHECKING:

    class _PrReviewHost(_typing.Protocol):
        """Cross-collaborator methods supplied by the review-stage façade."""

        @staticmethod
        def _write_no_go(item: WorkItem, ctx: StageContext) -> StepResult | None:
            raise NotImplementedError

        def _cleanup_review_worktree_then(
            self, item: WorkItem, outcome: StageOutcome
        ) -> StepResult:
            raise NotImplementedError

        def _handoff_implementation(self, item: WorkItem, ctx: StageContext) -> StepResult:
            raise NotImplementedError

        def _route_existing_threads(
            self, item: WorkItem, ctx: StageContext, *, after_checkout: bool
        ) -> StepResult | None:
            raise NotImplementedError

        @staticmethod
        def _on_reconciliation_done(item: WorkItem, result: JobResult) -> None:
            raise NotImplementedError


else:

    class _PrReviewHost:
        """Runtime-empty base for the statically checked host contract."""


# These are internal stage APIs shared by the façade and its collaborators.
__all__ = [
    "ADOPT_WORKTREE_WAIT",
    "AGENT_PR_REVIEWER",
    "CLEANUP_REVIEW_WORKTREE_WAIT",
    "COMPACT_REVIEWER_WAIT",
    "ENTER",
    "EVAL",
    "GIT_JOB_TIMEOUT_S",
    "POST",
    "POST_APPLY",
    "REVIEW_CHECKOUT_RETRY_CAP",
    "REVIEW_CHECKOUT_WAIT",
    "REVIEW_ERROR_RETRY_CAP",
    "REVIEW_WAIT",
    "STATE_SKIP",
    "VALIDATE_WAIT",
    "_COMMENT_VALIDATION_ONLY",
    "_ROUND_PAYLOAD_KEYS",
    "_STEP_HANDLER_NAMES",
    "_THREAD_READ_RETRIES",
    "AgentJob",
    "Any",
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
    "_ParsedReviewResponse",
    "_PrReviewHost",
    "_clear_round_review_state",
    "_durable_thread_id",
    "_issue_number",
    "_normalize_remediation_threads",
    "_parse_review_response",
    "_parse_validation_result",
    "_review_context_kind",
    "_scope_retraction_paths",
    "_thread_snapshots",
    "_validation_decisions",
    "_worktree_path",
    "agent_provider",
    "annotations",
    "cast",
    "get_pr_review_analysis_prompt",
    "get_review_validation_prompt",
    "is_full_commit_sha",
    "json",
    "logger",
    "logging",
    "pr_reviewer_claude_timeout",
    "re",
    "reviewer_model",
    "stage_model",
    "stage_timeout",
    "write_skip_label",
]
