"""PR-review stage: review, validate, post, address, and evaluate.

The queue stage is the sole live implementation of the review/validate/address
state machine (docs/architecture.md §5.5 "pr_review" is the binding contract):

- States: ENTER -> REVIEW_WAIT -> VALIDATE_WAIT -> POST -> DIFFICULTY_WAIT
  -> ADDRESS_WAIT -> PUSH_WAIT -> EVAL -> COMPACT_REVIEWER_WAIT
  -> COMPACT_WRITER_WAIT -> REVIEW_WAIT or terminal advance to ``merge_wait``.
  The legacy follow-up mini-states have been retired (#2140); a clean GO
  advances to ``merge_wait`` from EVAL.
- Budgets: ``pr_review_iter`` = 3 (soft cap), ``pr_review_hard`` = 6 (hard
  cap; rounds 4-6 are admitted ONLY while the unresolved-thread count
  strictly decreases under the progress-aware extension contract).
  Both read from ROUTES via
  ``ctx.budget``, never hardcoded here.
- Iteration accounting: ``item.attempts["pr_review_iter"]`` is the
  PER-LIFETIME audit trail (routing.py contract: attempts are never
  reset), so EVAL gates on the CYCLE-RELATIVE counter
  ``item.payload["pr_review_round"]``, reset by ``on_enter`` whenever a
  fresh implementation pass starts a new review cycle (keyed on
  ``attempts["implement"]``). ``attempts["pr_review_hard"]`` audits the
  extension rounds (rounds past the soft cap).
- Rounds advance in EVAL and ONLY for valid structural review audits.
  Missing or malformed audits never burn a round or touch labels
  (#911/#1554/#1794); they RETRY, bounded in-stage by
  ``payload["review_error_retries"]`` (cap :data:`REVIEW_ERROR_RETRY_CAP`
  consecutive failures, reset on any valid audit — the plan_review
  pattern). At the cap the item fails back ``agent_error`` (routes to
  implementation: a fresh implement pass, bounded by the ``implement``
  budget, is the doc's designated agent-error recovery).
- Review-thread ownership semantics: every open review thread—regardless of
  author—is implementation work. The implementation agent investigates and
  fixes each thread, then returns a concise reply. The host posts that reply
  only after the fix commit is pushed and never resolves the thread. The
  reviewer then performs a fresh review of the current change, prior review,
  implementation reply, and every open thread. The reviewer is the sole actor
  that can resolve a valid
  thread or post precise rejection feedback while leaving it open. Any open
  thread -> no-go label and address + re-review. A clean audit ->
  ``_write_go`` performs one final complete-thread live-read, requires a
  confirmed-unarmed live PR, and applies ``state:implementation-go``.
  The checkout GitJob-proven reviewed head accompanies that label;
  ``merge_wait`` verifies it before each bounded SHA-conditional normal merge
  attempt. Every
  real blocking round durably writes ``state:implementation-no-go`` before
  looping/regressing, non-fatally. Exhaustion -> durably
  apply ``state:skip`` [durable] -> SKIP.
- Downgraded-eligibility cost: when open automation threads invalidate an
  otherwise clean audit, this stage records the downgrade in EVAL and lets
  the NEXT round's POST re-count the live threads before dispatching the
  address leg, so a downgraded GO costs one extra review round. Chosen
  because POST live-checks the unresolved counts (a thread resolved
  out-of-band between rounds skips the address leg entirely) and the
  budget/extension gate stays a single chokepoint in EVAL.
- Progress metric (#1554 parity): the extension gate compares the total
  open-thread count. Only a reviewer resolution may demonstrate progress.
- POST publishes only genuinely new blocking audit findings. Validation does
  not recreate, replace, or suppress existing threads; it only gives the
  reviewer the authority to reconcile current implementation replies.
- Real-commit gating (#1575): PUSH_WAIT's commit_push result is inspected
  in EVAL. A push that produced NO commit (the fix agent punted or
  self-reported a phantom fix) is NOT treated as addressed: the address
  step is retried ONCE with the ``build_unaddressed_directive`` block
  (via ``get_address_review_prompt``'s ``unaddressed_findings``), and a
  second consecutive no-commit turn is evaluated as an unaddressed round.
- Reply-handoff recovery: an ambiguous GitHub transport/read failure after a
  fix reaches its verified head preserves the exact outstanding snapshot and
  reply batch for bounded host-only retry. A complete but mismatched read is
  stale evidence, not retryable: the host clears it and routes through fresh
  review so a changed conversation cannot wedge on an unreplayable snapshot.
- If the one-shot no-commit retry's address/push leg hard-fails, EVAL treats
  that as an explicit agent infrastructure failure, not as a second no-commit
  review round: it consumes the retry sentinel/directive, fails back
  ``agent_error`` without burning ``pr_review_iter``, and relies on the bounded
  implementation re-adoption path to run a fresh REVIEW->VALIDATE cycle.
- agent_error fail-backs (address failure, reviewer-error cap, missing
  PR/worktree) set ``payload["agent_error_failback"]`` so the
  implementation GATE consumes the ``implement`` budget on re-adoption —
  the cross-stage ping-pong bound (M1). ``review_error_retries`` is reset
  by ``on_enter`` on each fresh implementation cycle.
- Prompt functions (imported, never re-authored):
  ``prompts/pr_review.py get_pr_review_analysis_prompt`` /
  ``get_review_validation_prompt`` / ``get_comment_difficulty_prompt``,
  ``prompts/implementation.py get_impl_resume_feedback_prompt`` (fresh-PR
  address path), and ``prompts/address_review.py get_address_review_prompt``
  (existing-PR address path).
- The structural audit is parsed IN-WORKER (carried as the review job's
  ``parse`` callable; symbol-scoped zero-I/O exemption mirrors plan_review's).
  REVIEW_WAIT clears all stale round-scoped payload at submission so a failed
  later round can never replay an earlier audit or threads. Grades, summaries,
  and supplemental feedback are informational; only confirmed GitHub
  implementation-state label transitions control downstream admission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from hephaestus.automation.address_review_core import _parse_addressed_block
from hephaestus.automation.agent_config import (
    address_review_claude_timeout,
    implementer_claude_timeout,
    implementer_model,
    pr_reviewer_claude_timeout,
    reviewer_model,
)
from hephaestus.automation.prompts.address_review import get_address_review_prompt
from hephaestus.automation.prompts.implementation import get_impl_resume_feedback_prompt
from hephaestus.automation.prompts.pr_review import (
    BLOCKING_SEVERITIES,
    VALID_SEVERITIES,
    get_comment_difficulty_prompt,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)
from hephaestus.automation.review_audit import (
    ReviewAudit,
    has_reserved_finding_control,
    parse_review_audit,
)
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_COMMENT_CLASSIFIER,
    AGENT_IMPLEMENTER,
    AGENT_PR_REVIEWER,
)
from hephaestus.automation.state_labels import STATE_SKIP

from ..scope_retraction import (
    is_safe_scope_retraction_path,
    scope_retraction_paths_from_body,
)
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
    write_skip_label,
)
from .repo import is_full_commit_sha

logger = logging.getLogger(__name__)

_JSON_RESPONSE_BLOCK_RE = re.compile(
    r"^[ \t]*```json[ \t]*\r?\n(.*?)\r?\n^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _scope_retraction_paths(threads: list[dict[str, Any]]) -> tuple[str, ...] | None:
    """Extract host-enforced retractions from explicit scope-control findings."""
    paths: set[str] = set()
    for thread in threads:
        scope_paths = scope_retraction_paths_from_body(thread.get("body"))
        if scope_paths == ():
            continue
        path = thread.get("path")
        if (
            scope_paths is None
            or not is_safe_scope_retraction_path(path)
            or path not in scope_paths
        ):
            return None
        paths.update(scope_paths)
    return tuple(sorted(paths))


@dataclass(frozen=True)
class _ParsedReviewResponse:
    """Structural reviewer audit parsed inside the worker."""

    audit: ReviewAudit


def _parse_review_response(response: str) -> _ParsedReviewResponse:
    """Parse the review's strict structural audit without reading prose decisions."""
    return _ParsedReviewResponse(parse_review_audit(response))


# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
ADOPT_WORKTREE_WAIT = "ADOPT_WORKTREE_WAIT"
REVIEW_WAIT = "REVIEW_WAIT"
REVIEW_CHECKOUT_WAIT = "REVIEW_CHECKOUT_WAIT"
HOST_VERIFICATION_WAIT = "HOST_VERIFICATION_WAIT"
VALIDATE_WAIT = "VALIDATE_WAIT"
POST = "POST"
DIFFICULTY_WAIT = "DIFFICULTY_WAIT"
ADDRESS_WAIT = "ADDRESS_WAIT"
PUSH_WAIT = "PUSH_WAIT"
EVAL = "EVAL"
COMPACT_REVIEWER_WAIT = "COMPACT_REVIEWER_WAIT"
COMPACT_WRITER_WAIT = "COMPACT_WRITER_WAIT"

# A failed push with an unchanged live remote may be a transient local
# pre-push-hook or transport failure. Retry the already-created detached
# commit once; never ask the address agent to recreate it or discard it.
DIRECT_PUSH_RETRY_CAP = 1

# A changed remote requires a new review checkout because the previous one is
# a recovery artifact. One fresh pass handles a concurrent update without
# allowing a continuously advancing branch to accumulate unbounded agent runs
# and preserved worktrees in one coordinator invocation.
DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP = 1

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    ADOPT_WORKTREE_WAIT: "_adopt_worktree_wait",
    REVIEW_WAIT: "_review_wait",
    REVIEW_CHECKOUT_WAIT: "_review_checkout_wait",
    HOST_VERIFICATION_WAIT: "_host_verification_wait",
    VALIDATE_WAIT: "_validate_wait",
    POST: "_post",
    DIFFICULTY_WAIT: "_difficulty_wait",
    ADDRESS_WAIT: "_address",
    PUSH_WAIT: "_push_wait",
    EVAL: "_eval",
    COMPACT_REVIEWER_WAIT: "_compact_reviewer_wait",
    COMPACT_WRITER_WAIT: "_compact_writer_wait",
}


def _issue_number(item: WorkItem) -> int:
    """Return the issue number after the stage-level guard has run."""
    if item.issue is None:
        raise RuntimeError("pr_review stage reached without an issue number")
    return item.issue


def _pr_is_current_open_head(state: object, expected_head_sha: object) -> bool:
    """Return whether a fresh PR state proves one exact open, unarmed head."""
    return bool(
        is_full_commit_sha(expected_head_sha)
        and isinstance(state, dict)
        and state.get("state") == "OPEN"
        and state.get("autoMergeRequest") is None
        and state.get("headRefOid") == expected_head_sha
    )


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

#: A pushed implementation fix must receive its review-thread explanation
#: without requiring a second code change.  This bounded retry is host-only:
#: it replays the exact saved thread snapshots and agent prose, never asks an
#: implementation model to invent a new response.
IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP = 2
IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP = 2
_PENDING_IMPLEMENTATION_REPLY_HANDOFF = "pending_implementation_reply_handoff"
_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES = "pending_implementation_reply_handoff_retries"
_PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES = (
    "pending_implementation_reply_handoff_visibility_retries"
)
_IMPLEMENTATION_REPLY_BATCH_NONCE_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class _HostVerificationSpec:
    """One repository-owned verification command eligible for PR review."""

    changed_path: str | None
    argv: tuple[str, ...]
    descr: str


_HOST_SAFE_UNIT_TEST_ARGS = (
    "-o",
    "addopts=",
    "tests/unit",
    "-q",
    # These test the host's own mutation-capable Git/CI facilities or recurse
    # into host verification itself. They require GitHub CLI access or Bash's
    # global /tmp and are therefore intentionally left to normal CI, not this
    # read-only immutable reviewer boundary.
    "--ignore=tests/unit/ci/test_workflows.py",
    "--deselect=tests/unit/automation/pipeline/test_worker_pool.py::TestGitOps",
    "--deselect=tests/unit/automation/pipeline/test_worker_pool.py::"
    "TestWorkerPoolSubmitComplete::test_immutable_build_test_runs_from_disposable_head_snapshot",
)


#: Python reviews run read-only by design.  The host therefore performs the
#: complete fixed Python validation plan against an immutable snapshot before
#: the reviewer sees it.  These commands deliberately cover the local work
#: normally selected by ``$athena:pr-review`` without granting that agent a
#: writable source tree, cache, or temporary directory.
_PYTHON_HOST_VERIFICATION_SPECS: tuple[_HostVerificationSpec, ...] = (
    _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "ruff", "check", "hephaestus/", "tests/"),
        descr="review_python_ruff_check",
    ),
    _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "ruff", "format", "--check", "hephaestus/", "tests/"),
        descr="review_python_ruff_format",
    ),
    _HostVerificationSpec(
        changed_path=None,
        argv=(
            "uv",
            "run",
            "mypy",
            "--cache-dir=/dev/null",
            "hephaestus/",
            "scripts/",
            "tests/",
        ),
        descr="review_python_mypy",
    ),
    _HostVerificationSpec(
        changed_path=None,
        argv=("uv", "run", "pytest", *_HOST_SAFE_UNIT_TEST_ARGS),
        descr="review_python_unit_tests",
    ),
)
_PYTHON_VALIDATION_CONFIG_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "coverage.toml",
        "mypy.ini",
        "pytest.ini",
        "ruff.toml",
        "setup.cfg",
        "tox.ini",
    }
)
_INTEGRATION_HOST_VERIFICATION_SPEC = _HostVerificationSpec(
    changed_path=None,
    argv=("uv", "run", "pytest", "tests/integration", "-q"),
    descr="review_python_integration_tests",
)


#: Some Python regressions require additional bounded execution beyond the
#: baseline review plan.  Their path trigger is derived only from a real Git
#: diff header, never from reviewer or GitHub prose.
_PATH_HOST_VERIFICATION_SPECS: tuple[_HostVerificationSpec, ...] = (
    _HostVerificationSpec(
        changed_path="tests/performance/test_worker_pool_load.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/performance/test_worker_pool_load.py",
            "-q",
            "--load-report=../scratch/outputs/worker-pool.json",
        ),
        descr="review_stalled_consumer_verification",
    ),
)
HOST_VERIFICATION_TIMEOUT_S = 300
HOST_VERIFICATION_DIAGNOSTIC_MAX = 4_000


def _host_verification_specs(pr_diff: object) -> tuple[_HostVerificationSpec, ...]:
    """Return the complete fixed host plan activated by the verified diff."""
    if not isinstance(pr_diff, str):
        return ()
    changed_paths = {
        match.group(2)
        for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", pr_diff, flags=re.MULTILINE)
    }
    if not any(
        path.endswith(".py") or path in _PYTHON_VALIDATION_CONFIG_PATHS for path in changed_paths
    ):
        return ()
    return (
        *_PYTHON_HOST_VERIFICATION_SPECS,
        *(
            (_INTEGRATION_HOST_VERIFICATION_SPEC,)
            if any(path.startswith("tests/integration/") for path in changed_paths)
            else ()
        ),
        *(spec for spec in _PATH_HOST_VERIFICATION_SPECS if spec.changed_path in changed_paths),
    )


def _host_verification_receipt_matches(
    receipt: object, spec: _HostVerificationSpec, reviewed_head: str
) -> bool:
    """Return whether *receipt* was captured for this immutable head and command."""
    return bool(
        isinstance(receipt, dict)
        and receipt.get("head_sha") == reviewed_head
        and receipt.get("argv") == list(spec.argv)
        and receipt.get("immutable_source") is True
        and isinstance(receipt.get("ok"), bool)
        and isinstance(receipt.get("stdout_tail"), str)
        and isinstance(receipt.get("stderr_tail"), str)
    )


def _host_verification_receipts_match(
    receipts: object,
    specs: tuple[_HostVerificationSpec, ...],
    reviewed_head: str,
) -> bool:
    """Return whether every required fixed check has an exact-head receipt."""
    return bool(
        isinstance(receipts, list)
        and len(receipts) == len(specs)
        and all(
            _host_verification_receipt_matches(receipt, spec, reviewed_head)
            for receipt, spec in zip(receipts, specs, strict=True)
        )
    )


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
    "difficulty_tiers",
    "address_error",
    "address_output",
    "direct_push_retries",
    "detached_push_retry_head_sha",
    "push_no_commit",
    "no_commit_retry_done",
    "unaddressed_findings",
    "review_audit_failure",
    "review_refresh_required",
    "prior_comments_json",
    "validation_threads",
    "validation_receipt_fingerprints",
    "scope_retraction_paths",
    "reviewed_pr_base_sha",
    "host_verification_receipts",
    "host_verification_failure",
)


def _clear_round_review_state(item: WorkItem) -> None:
    """Discard review evidence that cannot survive a head-changing commit."""
    for key in _ROUND_PAYLOAD_KEYS:
        item.payload.pop(key, None)
    item.payload.pop("reviewed_pr_head_sha", None)
    item.payload.pop("pr_diff", None)


def _parse_validation_result(raw: Any) -> dict[str, Any] | None:
    """Parse the validator job's output into its verdict dict, tolerantly.

    The validation prompt asks for a single fenced JSON block at the END of
    the response (``{"resolved": [...], "unaddressed": [...]}``). When a
    response contains fenced blocks, only its complete final block can be a
    verdict; an earlier valid block must never authorize a malformed later
    answer. An unfenced response must be exactly one JSON object. Returns None
    when the final verdict does not parse — callers fail closed.

    Args:
        raw: The validation job's stored output (str, dict, or anything).

    Returns:
        The parsed verdict dict, or None when unparseable/absent.

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
    implementation agent replies after a real fix commit; the reviewer later
    performs a fresh review and resolves or returns the exact thread.
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
            for comment in comments:
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


def _address_replies(address_result: Any, threads: list[dict[str, Any]]) -> dict[str, str] | None:
    """Validate one implementation reply for every supplied open thread.

    An address pass is not complete when an agent silently omits a thread.
    The host therefore accepts only an exact, duplicate-free mapping of every
    snapshot ID to a bounded reply.  The agent may not resolve a thread; the
    returned prose is posted by the host only after its fix commit is pushed.
    """
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
    claimed_ids: list[str] = []
    for thread_id in addressed:
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None
        normalized_id = thread_id.strip()
        if normalized_id in claimed_ids:
            return None
        claimed_ids.append(normalized_id)
    if set(claimed_ids) != set(known_ids) or set(replies) != set(known_ids):
        return None
    normalized_replies: dict[str, str] = {}
    for thread_id in known_ids:
        reply = replies.get(thread_id)
        if not isinstance(reply, str) or not 0 < len(reply.strip()) <= 4_000:
            return None
        normalized_replies[thread_id] = reply.strip()
    return normalized_replies


def _implementation_reply_handoff(
    head_sha: object,
    threads: object,
    replies: object,
    batch_nonce: object,
) -> dict[str, Any] | None:
    """Return a replay-safe outstanding implementation-reply handoff.

    The persisted value is intentionally an exact host snapshot plus the
    model's already validated reply mapping.  It is neither a review receipt
    nor an authority to mutate: the adapter rechecks the live PR and thread
    state before each retry.
    """
    if (
        not is_full_commit_sha(head_sha)
        or not isinstance(threads, list)
        or not isinstance(replies, dict)
        or not isinstance(batch_nonce, str)
        or _IMPLEMENTATION_REPLY_BATCH_NONCE_RE.fullmatch(batch_nonce) is None
    ):
        return None
    snapshots = [dict(thread) for thread in threads if isinstance(thread, dict)]
    if len(snapshots) != len(threads):
        return None
    normalized_replies = _address_replies(
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
    }


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


def _address_review_feedback(item: WorkItem) -> str:
    """Serialize normalized live blocking threads for fresh-PR remediation."""
    threads = item.payload.get("remediation_threads")
    host_failure = item.payload.get("host_verification_failure")
    feedback: dict[str, object] = {"findings": threads if isinstance(threads, list) else []}
    if isinstance(host_failure, dict):
        feedback["host_verification_failure"] = host_failure
    return json.dumps(
        feedback,
        ensure_ascii=False,
        sort_keys=True,
    )


class PrReviewStage(Stage):
    """Stage: review -> validate -> post -> address -> EVAL.

    State machine (doc section "5. pr_review"):

    - ENTER: route to REVIEW_WAIT.
    - REVIEW_WAIT: clear stale round payload, submit the inline-review job
      (verdict parsed in-worker; review text is the verdict's ``raw``).
    - VALIDATE_WAIT: submit the prior-comment validation job (skipped
      straight to EVAL when the review job failed — the ERROR path burns
      no downstream work).
    - POST [M]: durably post surviving review threads, refresh the
      unresolved-thread counts; zero open review threads skip the
      address leg straight to EVAL.
    - DIFFICULTY_WAIT: submit the comment-difficulty classification job.
    - ADDRESS_WAIT: fresh-PR path resumes the implementer with the review
      feedback; existing-PR path runs the address-review session.
    - PUSH_WAIT: commit+push the addressing changes.
    - EVAL [M]: re-housed ``_evaluate_go_verdict`` + budget gate (see
      module docstring). A clean GO advances to ``merge_wait`` from EVAL.
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Hydrate review inputs, require an unarmed PR, and reset the round counter.

        ``attempts["pr_review_iter"]`` is per-lifetime (routing.py: attempts
        are never reset), so the per-cycle review budget is tracked in
        ``payload["pr_review_round"]``. The reset keys on
        ``attempts["implement"]`` (recorded in ``payload["pr_review_cycle"]``)
        so it fires exactly once per implementation pass: a same-cycle
        re-entry (e.g. the ERROR-path RETRY) keeps its round count and its
        progress trail. Idempotent — a literal double on_enter is a no-op.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None (always proceed to step()).

        """
        if item.pr is not None:
            # A work item can be restarted or directly seeded with stale
            # payload. Only the exact checkout barrier below may install a
            # reviewed-head proof for a new agent job.
            item.payload.pop("reviewed_pr_head_sha", None)
            arm_outcome = self._require_confirmed_unarmed(item.pr, ctx)
            if arm_outcome is not None:
                return arm_outcome
        cycle = item.attempts.get("implement", 0)
        if item.payload.get("pr_review_cycle") != cycle:
            item.payload["pr_review_cycle"] = cycle
            item.payload["pr_review_round"] = 0
            # Fresh implementation cycle: the consecutive reviewer-failure
            # streak restarts too (M1 — a re-entry after an agent_error
            # fail-back gets a fresh error budget; the implement budget,
            # consumed at the GATE, bounds the total number of cycles).
            item.payload.pop("review_error_retries", None)
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next PR-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if item.issue is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        if item.pr is None:
            # Nothing to review: fail back to implementation, whose
            # PR_CREATE step is the designated (re)creation path.
            logger.warning("pr_review:%d: no PR on item; failing back", item.issue)
            return self._fail_back_agent_error(item)
        if (
            item.state == "ENTER"
            and not item.worktree
            and (item.kind is ItemKind.PR or bool(item.payload.get("existing_pr")))
        ):
            # A PR-review entry has no adopted checkout yet. It must never be
            # reviewed from the shared repository root, including when an
            # issue seed was routed to an already-open PR by drive-green.
            # It also must not detour through fresh implementation merely to
            # obtain that checkout: issue-level state:skip is intentionally
            # absolute for fresh implementation but is not a reason to skip
            # an existing PR review.
            return self._adopt_direct_pr_worktree(item, ctx)

        handler_name = _STEP_HANDLER_NAMES.get(item.state)
        if handler_name is not None:
            handler = cast(
                Callable[[WorkItem, StageContext], StepResult],
                getattr(self, handler_name),
            )
            return handler(item, ctx)

        logger.warning("pr_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _enter(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ENTER advances to REVIEW_WAIT."""
        return Continue(next_state=REVIEW_WAIT)

    def _adopt_direct_pr_worktree(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Create a synchronized isolated checkout for an existing PR."""
        if item.pr is None:  # guarded by step(); keeps type narrowing local
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        branch = ctx.github.get_pr_head_branch(item.pr)
        if not branch:
            logger.error("pr_review:%s: no head branch for direct PR #%d", item.issue, item.pr)
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_no_head_branch")
        item.branch = branch
        item.payload["existing_pr"] = True
        generation = item.payload.get("direct_pr_worktree_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_generation_invalid")
        logger.info(
            "pr_review:%d: adopting direct PR #%d (branch %r) for review",
            _issue_number(item),
            item.pr,
            branch,
        )
        kwargs: dict[str, object] = {
            "issue_number": _issue_number(item),
            "branch_name": branch,
            "refresh_base": False,
            # This mutable review checkout cannot reuse the writer's branch
            # checkout.
            "isolated": True,
            "sync_to_remote": True,
            "pr_number": item.pr,
            "repo_root": str(ctx.paths.repo_root),
        }
        if generation:
            kwargs["isolated_generation"] = generation
        job = GitJob(
            repo=item.repo,
            op="create_worktree",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs=kwargs,
            descr="direct_pr_review_worktree",
        )
        # Coordinator completion callbacks run before on_done_state is
        # assigned. The marker makes that ordering explicit and fail-closed.
        item.payload["direct_pr_worktree_pending"] = True
        return JobRequest(job, on_done_state=ADOPT_WORKTREE_WAIT)

    def _adopt_worktree_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Advance only from a clean, synchronized direct-PR checkout."""
        del ctx
        if item.payload.pop("direct_pr_worktree_error", None):
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_failed")
        if not item.worktree:
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_unfinished")
        if item.payload.get("direct_pr_worktree_dirty"):
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_dirty")
        return Continue(next_state=REVIEW_WAIT)

    def _review_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Refresh review inputs, then bind the checkout before dispatch."""
        # Clear ALL round-scoped payload at submission (stale-result
        # guard, M3 pattern): a failed later round must never replay an
        # earlier round's verdict, threads, or address output.
        _clear_round_review_state(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        review_context = ctx.github.pr_review_context(item.pr)
        if review_context is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_review_context_unavailable")
        expected_head = str(review_context.get("pr_head_sha") or "")
        if not expected_head:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_review_head_unavailable")
        base_branch = str(review_context.get("pr_base_branch") or "main")
        item.payload.update(review_context)
        item.payload["review_checkout_expected_head"] = expected_head
        item.payload["review_checkout_pending"] = True
        job = GitJob(
            repo=item.repo,
            op="verify_pr_review_checkout",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs={
                "worktree_path": str(_worktree_path(item, ctx)),
                "branch": item.branch,
                "expected_head_sha": expected_head,
                "base_branch": base_branch,
                "pr_number": item.pr,
            },
            descr="verify_pr_review_checkout",
        )
        return JobRequest(job, on_done_state=REVIEW_CHECKOUT_WAIT)

    def _review_checkout_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit review only after the fresh snapshot matches a clean checkout."""
        expected_head = str(item.payload.pop("review_checkout_expected_head", "") or "")
        error = str(item.payload.pop("review_checkout_error", "") or "")
        ready = bool(item.payload.pop("review_checkout_ready", False))
        if error:
            return StageOutcome(Disposition.FINISH_FAIL, "review_checkout_unavailable")
        if not ready:
            retries = int(item.payload.get("review_checkout_retries", 0)) + 1
            item.payload["review_checkout_retries"] = retries
            if retries <= REVIEW_CHECKOUT_RETRY_CAP:
                return Continue(next_state=REVIEW_WAIT)
            return StageOutcome(Disposition.FINISH_FAIL, "review_checkout_head_drift")
        item.payload.pop("review_checkout_retries", None)
        item.payload["reviewed_pr_head_sha"] = expected_head
        prior_generation = item.payload.get("reviewed_pr_proof_generation", 0)
        if isinstance(prior_generation, bool) or not isinstance(prior_generation, int):
            prior_generation = 0
        item.payload["reviewed_pr_proof_generation"] = prior_generation + 1
        verifications = _host_verification_specs(item.payload.get("pr_diff"))
        if verifications:
            logger.info(
                "pr_review:%d: requesting %d host verifications",
                _issue_number(item),
                len(verifications),
            )
            item.payload["host_verification_receipts"] = []
            return self._submit_host_verification(item, ctx, verifications[0])
        return self._submit_review_job(item, ctx)

    @staticmethod
    def _submit_host_verification(
        item: WorkItem, ctx: StageContext, verification: _HostVerificationSpec
    ) -> JobRequest:
        """Submit one fixed host command from the immutable review plan."""
        return JobRequest(
            BuildTestJob(
                repo=item.repo,
                cwd=_worktree_path(item, ctx),
                argv=verification.argv,
                timeout_s=HOST_VERIFICATION_TIMEOUT_S,
                expected_head_sha=str(item.payload.get("reviewed_pr_head_sha") or ""),
                immutable_source=True,
                descr=verification.descr,
            ),
            on_done_state=HOST_VERIFICATION_WAIT,
        )

    def _submit_review_job(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Create the agent job after the checkout/head barrier succeeds."""
        issue = _issue_number(item)
        round_index = item.payload.get("pr_review_round", 0)
        logger.info(
            "pr_review:%d: requesting review job (round %d, PR #%d)",
            issue,
            round_index,
            item.pr,
        )
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_pr_review_analysis_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            # The normal $athena:pr-review skill is read-only, but its
            # declared workflow uses local Bash helpers and review subagents.
            # Keep that capability on the sole GO/NOGO review job only;
            # validation and difficulty jobs retain WorkerPool's read scope.
            allowed_tools="Read,Glob,Grep,Bash,Skill,Agent,WebFetch",
            # on_enter refreshes diff and body context through the stage
            # adapter before every review cycle.
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "pr_diff": item.payload.get("pr_diff", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "pr_description": item.payload.get("pr_description", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "include_nitpicks": bool(
                    getattr(
                        ctx.config,
                        "nitpick",
                        getattr(ctx.config, "include_nitpicks", False),
                    )
                ),
                "review_context_kind": _review_context_kind(item),
            },
            parse=_parse_review_response,  # structural audit parsed in-worker
            descr="review",
        )
        item.payload["review_job_pending"] = True
        return JobRequest(job, on_done_state=VALIDATE_WAIT)

    def _validate_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Perform a fresh review of implementation replies before resolution."""
        issue = _issue_number(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.payload.pop("review_failed", None):
            # The review job itself failed: skip the validate/post/
            # address leg — EVAL's missing-verdict ERROR path handles it
            # without burning a round.
            return Continue(next_state=EVAL)
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            receipts = (
                ctx.github.reviewer_validation_receipts(
                    item.pr,
                    reviewed_head_sha=reviewed_head,
                    threads=live_threads,
                )
                if is_full_commit_sha(reviewed_head)
                else []
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not fetch validation receipts (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        validation_threads = _validation_thread_snapshots(live_threads, receipts)
        receipt_fingerprints = _validation_receipt_fingerprints(receipts)
        if validation_threads is None or receipt_fingerprints is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["validation_threads"] = validation_threads
        item.payload["validation_receipt_fingerprints"] = receipt_fingerprints
        item.payload["prior_comments_json"] = json.dumps(
            validation_threads, ensure_ascii=False, sort_keys=True
        )
        logger.info("pr_review:%d: requesting validation job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_review_validation_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_PR_REVIEWER,
            resume_session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
            prompt_kwargs={
                "pr_number": item.pr,
                "issue_number": item.issue,
                "prior_comments_json": item.payload["prior_comments_json"],
                "diff_text": item.payload.get("pr_diff", ""),
                "host_verifications_json": json.dumps(
                    item.payload.get("host_verification_receipts", []), sort_keys=True
                ),
                "review_context_kind": _review_context_kind(item),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

    def _host_verification_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit primary review only after its host verification passed."""
        verifications = _host_verification_specs(item.payload.get("pr_diff"))
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        receipts = item.payload.get("host_verification_receipts")
        if not isinstance(receipts, list) or len(receipts) > len(verifications):
            return self._handle_host_verification_failure(
                item,
                ctx,
                None,
                "host_verification_receipt_invalid",
            )
        matched_receipts = cast(list[dict[str, Any]], receipts)
        for verification, receipt in zip(verifications, matched_receipts, strict=False):
            if not _host_verification_receipt_matches(receipt, verification, reviewed_head):
                return self._handle_host_verification_failure(
                    item,
                    ctx,
                    verification,
                    str(receipt.get("error") or "host_verification_receipt_invalid"),
                )
            if receipt["ok"]:
                continue
            return self._handle_host_verification_failure(
                item,
                ctx,
                verification,
                str(receipt.get("error") or "host_verification_failed"),
            )
        if len(matched_receipts) < len(verifications):
            return self._submit_host_verification(item, ctx, verifications[len(matched_receipts)])
        if not _host_verification_receipts_match(receipts, verifications, reviewed_head):
            return self._handle_host_verification_failure(
                item,
                ctx,
                None,
                "host_verification_receipt_invalid",
            )
        return self._submit_review_job(item, ctx)

    @staticmethod
    def _handle_host_verification_failure(
        item: WorkItem,
        ctx: StageContext,
        verification: _HostVerificationSpec | None,
        reason: str,
    ) -> StepResult:
        """Durably reject a failed host test without entering audit retries."""
        receipts = item.payload.get("host_verification_receipts")
        receipt = (
            receipts[-1]
            if isinstance(receipts, list) and receipts and isinstance(receipts[-1], dict)
            else None
        )
        diagnostic = {
            "argv": list(verification.argv) if verification is not None else [],
            "path": ((verification.changed_path or "") if verification is not None else ""),
            "head_sha": str(item.payload.get("reviewed_pr_head_sha") or ""),
            "error": reason[:HOST_VERIFICATION_DIAGNOSTIC_MAX],
            "stdout_tail": (
                str(receipt.get("stdout_tail") or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:]
                if isinstance(receipt, dict)
                else ""
            ),
            "stderr_tail": (
                str(receipt.get("stderr_tail") or "")[-HOST_VERIFICATION_DIAGNOSTIC_MAX:]
                if isinstance(receipt, dict)
                else ""
            ),
        }
        item.payload["host_verification_failure"] = diagnostic
        no_go_outcome = PrReviewStage._write_no_go(item, ctx)
        if no_go_outcome is not None:
            return no_go_outcome

        # Only a confirmed fixed-tool validation failure may be repaired by
        # the implementation agent. UV/sandbox/bootstrap errors share a
        # nonzero process status but are operator remediation, not code work.
        failure_kind = receipt.get("failure_kind") if isinstance(receipt, dict) else None
        if failure_kind in {"test", "validation"}:
            detail = (
                "Host verification failed for "
                f"{diagnostic['path']}: {reason}. Investigate and fix the test or "
                "implementation, then rerun the fixed verification command."
            )
            if item.payload.get("existing_pr"):
                item.payload["unaddressed_findings"] = [
                    {"path": diagnostic["path"], "line": None, "body": detail}
                ]
            return Continue(next_state=ADDRESS_WAIT)
        return StageOutcome(Disposition.FINISH_FAIL, "host_verification_failed")

    def _difficulty_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """DIFFICULTY_WAIT submits the comment-difficulty job."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: requesting difficulty job", issue)
        job = AgentJob(
            repo=item.repo,
            issue=issue,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_comment_difficulty_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_agent=AGENT_COMMENT_CLASSIFIER,
            sandbox="read-only",
            prompt_kwargs={
                "issue_number": item.issue,
                "comments_json": json.dumps(item.payload.get("remediation_threads", [])),
                "review_context_kind": _review_context_kind(item),
            },
            descr="difficulty",
        )
        return JobRequest(job, on_done_state=ADDRESS_WAIT)

    def _push_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """PUSH_WAIT submits the commit+push job for the addressing changes."""
        issue = _issue_number(item)
        logger.info("pr_review:%d: requesting push job", issue)
        agent = agent_provider(ctx)
        kwargs: dict[str, object] = {
            "issue_number": issue,
            "pr_number": item.pr,
            "repo_root": str(ctx.paths.repo_root),
            "worktree_path": item.worktree,
            "branch": item.branch,
            "agent": agent,
            "agent_model": stage_model(ctx, "implementer", implementer_model, provider=agent),
        }
        if item.payload.get("direct_pr_worktree"):
            # Direct review addresses findings from a detached checkout; the
            # coordinator may publish that exact HEAD only while the remote
            # still equals the checkout-proven reviewed head. This permits a
            # deliberate rebase without overwriting a concurrent writer.
            kwargs["publish_detached_head"] = True
            kwargs["expected_remote_sha"] = item.payload.get("reviewed_pr_head_sha")
            retry_head_sha = item.payload.get("detached_push_retry_head_sha")
            if retry_head_sha is not None:
                if not is_full_commit_sha(retry_head_sha):
                    return StageOutcome(
                        Disposition.FINISH_FAIL, "detached_push_retry_receipt_invalid"
                    )
                kwargs["detached_push_retry_head_sha"] = retry_head_sha
        scope_retraction_paths = item.payload.get("scope_retraction_paths")
        if scope_retraction_paths is not None:
            if not isinstance(scope_retraction_paths, tuple) or not all(
                is_safe_scope_retraction_path(path) for path in scope_retraction_paths
            ):
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_path_invalid")
            base_sha = item.payload.get("reviewed_pr_base_sha")
            if not is_full_commit_sha(base_sha):
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_base_unavailable")
            kwargs["scope_retraction_paths"] = scope_retraction_paths
            kwargs["scope_retraction_base_sha"] = base_sha
        git_job = GitJob(
            repo=item.repo,
            op="commit_push",
            timeout_s=GIT_JOB_TIMEOUT_S,
            kwargs=kwargs,
            descr="push_fixes",
        )
        return JobRequest(git_job, on_done_state=EVAL)

    def _compact_reviewer_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Compact the reviewer before the next retry continues its session."""
        if not item.worktree:
            return Continue(next_state=COMPACT_WRITER_WAIT)
        job = CompactJob(
            repo=item.repo,
            issue=_issue_number(item),
            agent=agent_provider(ctx),
            session_agent=AGENT_PR_REVIEWER,
            model=stage_model(ctx, "reviewer", reviewer_model),
            cwd=_worktree_path(item, ctx),
            timeout_s=pr_reviewer_claude_timeout(),
            session_id=item.session_ids.get(AGENT_PR_REVIEWER),
            sandbox="read-only",
        )
        return JobRequest(job, on_done_state=COMPACT_WRITER_WAIT)

    def _compact_writer_wait(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Compact the writer before the next retry continues its session."""
        if not item.worktree:
            return Continue(next_state=REVIEW_WAIT)
        session_agent = (
            AGENT_ADDRESS_REVIEW if item.payload.get("existing_pr") else AGENT_IMPLEMENTER
        )
        job = CompactJob(
            repo=item.repo,
            issue=_issue_number(item),
            agent=agent_provider(ctx),
            session_agent=session_agent,
            model=stage_model(ctx, "implementer", implementer_model),
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            session_id=item.session_ids.get(session_agent),
            sandbox="read-only",
        )
        return JobRequest(job, on_done_state=REVIEW_WAIT)

    def on_job_done(  # noqa: C901
        self, item: WorkItem, result: JobResult, ctx: StageContext
    ) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if self._consume_direct_worktree_result(item, result):
            return
        if self._consume_review_checkout_result(item, result):
            return
        if item.state == HOST_VERIFICATION_WAIT:
            self._store_host_verification_result(item, result)
            return

        review_job_pending = bool(item.payload.pop("review_job_pending", None))
        is_review_result = review_job_pending or item.state == REVIEW_WAIT
        if self._consume_failed_job(item, result, is_review_result):
            return

        if item.state == PUSH_WAIT:
            # Real-commit gate (#1575): commit_push reports whether a commit
            # was actually produced (value/changed True). A no-commit push
            # means the address turn was a phantom fix — EVAL must NOT treat
            # the round as addressed.
            push_receipt = result.value if isinstance(result.value, dict) else {}
            raw_published_head = push_receipt.get("head_sha")
            published_head = raw_published_head if isinstance(raw_published_head, str) else ""
            produced_commit = bool(push_receipt.get("pushed")) and is_full_commit_sha(
                published_head
            )
            if produced_commit:
                remediation_threads = item.payload.get("remediation_threads")
                threads = remediation_threads if isinstance(remediation_threads, list) else []
                snapshots = item.payload.get("remediation_thread_snapshots")
                thread_snapshots = snapshots if isinstance(snapshots, list) else []
                replies = _address_replies(item.payload.get("address_output"), threads)
                reply_contract_failed = bool(threads) and replies is None
                if reply_contract_failed:
                    logger.warning(
                        "pr_review:%s: implementation did not return one reply for every open "
                        "thread; refusing to accept a partial address pass",
                        item.issue,
                    )
                elif replies and item.pr is not None:
                    handoff = _implementation_reply_handoff(
                        published_head,
                        thread_snapshots,
                        replies,
                        secrets.token_hex(16),
                    )
                    if handoff is None:
                        logger.warning(
                            "pr_review:%s: could not preserve the exact implementation "
                            "reply handoff; refusing to infer a replacement response",
                            item.issue,
                        )
                    else:
                        # Keep the exact, already-validated agent output until
                        # GitHub proves every reply. _clear_round_review_state
                        # deliberately does not clear this handoff because the
                        # code commit has already changed the review head.
                        item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF] = handoff
                        item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
                    batch_nonce = handoff["batch_nonce"] if handoff is not None else ""
                    try:
                        # The worker returns the local commit it actually
                        # published.  A later arbitrary remote push must not
                        # acquire this implementation reply by being read as
                        # the current head.
                        state = ctx.github.gh_pr_state(item.pr)
                        if not _pr_is_current_open_head(state, published_head):
                            raise RuntimeError("published implementation head no longer current")
                        reply_result = ctx.github.post_implementation_thread_replies(
                            item.pr,
                            expected_head_sha=published_head,
                            threads=thread_snapshots,
                            replies=replies,
                            batch_nonce=batch_nonce,
                        )
                    except Exception as error:
                        logger.warning(
                            "pr_review:%s: could not post implementation replies (%s)",
                            item.issue,
                            type(error).__name__,
                        )
                    else:
                        replied = set(getattr(reply_result, "replied_thread_ids", ()))
                        blocked = set(getattr(reply_result, "blocked_thread_ids", ()))
                        receipts = list(getattr(reply_result, "receipts", ()))
                        retryable = bool(getattr(reply_result, "retryable", False))
                        expected_ids = set(replies)
                        remaining_ids = expected_ids - replied
                        result_retryable_ids = set(
                            getattr(reply_result, "retryable_thread_ids", ())
                        )
                        retryable_ids = (
                            result_retryable_ids
                            if result_retryable_ids and result_retryable_ids.issubset(remaining_ids)
                            else remaining_ids
                            if retryable and not result_retryable_ids
                            else set()
                        )
                        if replied != set(replies) or len(receipts) != len(replied):
                            logger.warning(
                                "pr_review:%s: some implementation replies could not be verified",
                                item.issue,
                            )
                            if retryable_ids and handoff is not None:
                                item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF] = (
                                    _implementation_reply_handoff(
                                        published_head,
                                        [
                                            snapshot
                                            for snapshot in thread_snapshots
                                            if str(snapshot.get("id") or "") in retryable_ids
                                        ],
                                        {
                                            thread_id: reply
                                            for thread_id, reply in replies.items()
                                            if thread_id in retryable_ids
                                        },
                                        batch_nonce,
                                    )
                                )
                            elif retryable_ids:
                                logger.info(
                                    "pr_review:%s: retaining an exact reply handoff after an "
                                    "ambiguous host failure",
                                    item.issue,
                                )
                            else:
                                # A verified mismatch means the conversation changed.  Replaying
                                # a stale snapshot can never repair it and eventually wedges the
                                # work item; the fresh reviewer cycle will obtain new facts.
                                item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
                                item.payload.pop(
                                    _PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None
                                )
                        else:
                            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
                            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
                        if blocked:
                            logger.info(
                                "pr_review:%s: %d changed thread(s) remain open for a new "
                                "implementation pass",
                                item.issue,
                                len(blocked),
                            )
                # The old audit and checkout receipt describe the pre-push
                # head. Discard the entire round before EVAL can bind the new
                # head to any implementation-state transition.
                _clear_round_review_state(item)
                item.payload["review_refresh_required"] = True
                if reply_contract_failed:
                    # The code commit may already be durable, but accepting an
                    # incomplete agent transcript would let one supplied open
                    # thread disappear from the implementation handoff. Route
                    # back through the normal bounded implementation recovery.
                    item.payload["address_error"] = True
            else:
                # Preserve the existing no-commit gate while requiring it to
                # re-confirm the unchanged remote head before a negative write.
                item.payload.pop("reviewed_pr_head_sha", None)
            item.payload["push_no_commit"] = not produced_commit
            return

        if is_review_result and result.value is not None:
            self._store_review_result(item, result.value)
        elif item.state == VALIDATE_WAIT and result.value is not None:
            item.payload["validation_result"] = result.value
        elif item.state == DIFFICULTY_WAIT and result.value is not None:
            item.payload["difficulty_tiers"] = str(result.value)
        elif item.state == ADDRESS_WAIT and result.value is not None:
            item.payload["address_output"] = result.value

    @staticmethod
    def _consume_direct_worktree_result(item: WorkItem, result: JobResult) -> bool:
        """Store a direct-worktree completion when one is pending."""
        if not item.payload.pop("direct_pr_worktree_pending", None):
            return False
        PrReviewStage._on_direct_pr_worktree_done(item, result)
        return True

    @staticmethod
    def _consume_review_checkout_result(item: WorkItem, result: JobResult) -> bool:
        """Store the review checkout barrier result when one is pending."""
        if not item.payload.pop("review_checkout_pending", None):
            return False
        if not result.ok:
            item.payload["review_checkout_error"] = result.error or "checkout job failed"
            return True
        value = result.value
        ready = bool(isinstance(value, dict) and value.get("ready"))
        review_diff = value.get("diff") if isinstance(value, dict) else None
        review_base = value.get("base") if isinstance(value, dict) else None
        if ready and not isinstance(review_diff, str):
            item.payload["review_checkout_error"] = "checkout job returned no bound diff"
            ready = False
        if ready:
            item.payload["pr_diff"] = review_diff
            if is_full_commit_sha(review_base):
                item.payload["reviewed_pr_base_sha"] = review_base
        item.payload["review_checkout_ready"] = ready
        return True

    @staticmethod
    def _store_host_verification_result(item: WorkItem, result: JobResult) -> None:
        """Append a bounded, head-bound receipt from the fixed host plan."""
        specs = _host_verification_specs(item.payload.get("pr_diff"))
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        receipts = item.payload.get("host_verification_receipts")
        if (
            not specs
            or not is_full_commit_sha(reviewed_head)
            or not isinstance(receipts, list)
            or len(receipts) >= len(specs)
        ):
            item.payload.pop("host_verification_receipts", None)
            return
        spec = specs[len(receipts)]
        receipts.append(
            {
                "argv": list(spec.argv),
                "head_sha": reviewed_head,
                "immutable_source": bool(
                    isinstance(result.value, dict)
                    and result.value.get("head_sha") == reviewed_head
                    and result.value.get("immutable_source") is True
                ),
                "failure_kind": (
                    result.value.get("failure_kind", "runner")
                    if isinstance(result.value, dict)
                    and result.value.get("failure_kind") in {"none", "runner", "test", "validation"}
                    else "runner"
                ),
                "ok": result.ok,
                "error": result.error or "",
                "stdout_tail": result.stdout_tail,
                "stderr_tail": result.stderr_tail,
            }
        )

    def _consume_failed_job(
        self, item: WorkItem, result: JobResult, is_review_result: bool
    ) -> bool:
        """Store a failed result and report whether completion handling is done."""
        if result.ok:
            return False
        if is_review_result:
            item.payload["review_failed"] = True
            return True
        self._on_job_failed(item, result)
        return True

    @staticmethod
    def _store_review_result(item: WorkItem, value: object) -> None:
        """Persist one structural reviewer result."""
        if isinstance(value, _ParsedReviewResponse):
            item.payload["review_audit"] = value.audit
            item.payload["review_feedback"] = value.audit.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.audit.findings]
            return
        if isinstance(value, ReviewAudit):
            item.payload["review_audit"] = value
            item.payload["review_feedback"] = value.raw_feedback
            item.payload["review_threads"] = [dict(comment) for comment in value.findings]
            return
        item.payload["review_audit_failure"] = True

    @staticmethod
    def _on_direct_pr_worktree_done(item: WorkItem, result: JobResult) -> None:
        """Record the exact checkout created for a direct PR review."""
        if not result.ok:
            logger.warning("pr_review:%s: direct PR worktree failed: %s", item.issue, result.error)
            item.worktree = ""
            item.payload["direct_pr_worktree_error"] = result.error or "worktree job failed"
            return
        value = result.value
        if isinstance(value, dict):
            item.worktree = str(value.get("path", ""))
            item.payload["direct_pr_worktree_dirty"] = bool(value.get("dirty"))
            if item.worktree and not item.payload["direct_pr_worktree_dirty"]:
                item.payload["direct_pr_worktree"] = item.worktree
        elif isinstance(value, str):
            item.worktree = value
            if item.worktree:
                item.payload["direct_pr_worktree"] = item.worktree
        else:
            item.payload["direct_pr_worktree_error"] = "worktree job returned no path"

    @staticmethod
    def _on_job_failed(item: WorkItem, result: JobResult) -> None:
        """Record the state-specific failure outcome for a non-git agent job."""
        logger.warning("pr_review:%s: job failed: %s", item.issue, result.error)
        if item.state == REVIEW_WAIT:
            # EVAL treats the missing audit as reviewer infrastructure failure;
            # the flag lets VALIDATE_WAIT skip the dead round.
            item.payload["review_failed"] = True
        elif item.state == PUSH_WAIT:
            receipt = result.value if isinstance(result.value, dict) else {}
            if receipt.get("scope_retraction_failure") is True:
                item.payload["scope_retraction_failure"] = True
                return
            failure = receipt.get("detached_push_failure")
            if failure in {
                "remote_changed",
                "remote_changed_unrecorded",
                "remote_unchanged",
                "remote_unconfirmed",
                "retry_checkout_changed",
                "retry_checkout_unconfirmed",
            }:
                item.payload["detached_push_failure"] = failure
                source_sha = receipt.get("detached_push_head_sha")
                if is_full_commit_sha(source_sha):
                    item.payload["detached_push_head_sha"] = source_sha
                return
            if item.payload.get("direct_pr_worktree") and item.worktree:
                # A direct-review checkout may hold an address commit even
                # when publication setup itself failed before it could return
                # a classified receipt. Preserve rather than failing back to
                # an agent re-adoption path that could orphan that commit.
                item.payload["detached_push_failure"] = "remote_unconfirmed"
                return
            item.payload["address_error"] = True
        elif item.state == ADDRESS_WAIT:
            item.payload["address_error"] = True

    def _post(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """Reconcile reviewer decisions, publish new findings, and queue remediation.

        Reviewer validation can only resolve or return current implementation
        reply receipts. Fresh audit findings are deduplicated against live
        threads, then every remaining open thread enters implementation
        remediation regardless of its author.
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        audit = item.payload.get("review_audit")
        if not isinstance(audit, ReviewAudit) or not audit.valid:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        raw_threads = [dict(t) for t in item.payload.get("review_threads") or []]
        # Validation controls only the reviewer-owned reconciliation of
        # implementation replies. Fresh audit findings are independently
        # deduplicated against live threads below; a validator must never
        # recreate, replace, or suppress an open review conversation.
        threads = raw_threads
        item.payload["raw_review_threads"] = raw_threads

        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        try:
            live_for_reconciliation = ctx.github.list_unresolved_review_threads(item.pr)
            validation_receipts = (
                ctx.github.reviewer_validation_receipts(
                    item.pr,
                    reviewed_head_sha=reviewed_head,
                    threads=live_for_reconciliation,
                )
                if is_full_commit_sha(reviewed_head)
                else []
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not refresh reviewer validation receipts (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        live_receipt_fingerprints = _validation_receipt_fingerprints(validation_receipts)
        validated_receipt_fingerprints = item.payload.get("validation_receipt_fingerprints")
        if live_receipt_fingerprints is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        if validated_receipt_fingerprints is not None and (
            not isinstance(validated_receipt_fingerprints, dict)
            or validated_receipt_fingerprints != live_receipt_fingerprints
        ):
            # The validation agent reviewed a different immutable receipt than
            # the one currently open on GitHub.  Its decision must not act on
            # a replacement reply, even if the durable thread ID is unchanged.
            item.payload.pop("validation_result", None)
            item.payload.pop("validation_threads", None)
            item.payload.pop("validation_receipt_fingerprints", None)
            logger.info(
                "pr_review:%s: implementation reply receipt changed after validation; "
                "revalidating before reconciliation",
                item.issue,
            )
            return Continue(next_state=VALIDATE_WAIT)
        if validation_receipts:
            decisions = _reviewer_thread_decisions(
                validation_receipts, item.payload.get("validation_result")
            )
            if decisions is None:
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            resolved_ids, feedback = decisions
            try:
                reconciliation = ctx.github.reconcile_reviewer_validated_threads(
                    item.pr,
                    reviewed_head_sha=reviewed_head,
                    receipts=validation_receipts,
                    resolved_thread_ids=resolved_ids,
                    feedback=feedback,
                )
            except Exception as error:
                logger.warning(
                    "pr_review:%s: reviewer thread reconciliation failed (%s)",
                    item.issue,
                    type(error).__name__,
                )
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            expected_ids = {_durable_thread_id(receipt) for receipt in validation_receipts}
            completed_ids = set(reconciliation.resolved_thread_ids) | set(
                reconciliation.feedback_thread_ids
            )
            if None in expected_ids or not completed_ids.issubset(expected_ids):
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
            if reconciliation.blocked_thread_ids:
                # Resolution status was not proven for at least one exact
                # receipt.  The adapter deliberately does not issue an
                # unresolve compensation mutation; discard this validator's
                # decisions and obtain a new audit/check-out proof instead.
                item.payload.pop("validation_result", None)
                item.payload.pop("validation_threads", None)
                item.payload.pop("validation_receipt_fingerprints", None)
                return Continue(next_state=REVIEW_WAIT)
            # A concurrent mutation can make one receipt ineligible after a
            # previous receipt was safely resolved or received feedback.  Do
            # not retain that stale, partially consumed receipt set: doing so
            # wedges the next validation pass because the completed thread is
            # no longer open.  Drop all receipts and rebuild remediation from
            # the fresh complete live snapshot below.  The adapter's snapshot
            # checks prevent duplicate mutations for the blocked IDs.
            if completed_ids != expected_ids:
                logger.info(
                    "pr_review:%s: reviewer reconciliation was partial; refreshing live threads",
                    item.issue,
                )
        try:
            live_before_post = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: review finding dedupe read failed (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        live_by_id = {
            thread_id: thread
            for thread in live_before_post
            if (thread_id := _durable_thread_id(thread)) is not None
        }
        threads = _without_duplicate_live_findings(threads, live_by_id)
        threads = [
            thread
            for thread in threads
            if str(thread.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        ]
        if any(not _is_postable_finding(thread) for thread in threads):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        # The surviving audit set is what gets posted. Classification and
        # addressing use the normalized live read-back installed below.
        item.payload["review_threads"] = threads
        if threads and is_full_commit_sha(reviewed_head):
            publication_guard = self._require_reviewed_unarmed(item, ctx)
            if publication_guard is not None:
                return publication_guard
        post_receipts: list[dict[str, Any]] = []
        if threads:
            try:
                post_receipts = list(
                    ctx.github.post_review_threads(
                        item.pr,
                        list(threads),
                        expected_head_sha=reviewed_head,
                    )
                )
            except Exception as error:
                logger.warning(
                    "pr_review:%s: review finding publication failed (%s)",
                    item.issue,
                    type(error).__name__,
                )
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
        if len(post_receipts) != len(threads):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["posted_thread_ids"] = [str(receipt["id"]) for receipt in post_receipts]
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: review finding live read-back failed (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        remediation_threads = _normalize_remediation_threads(live_threads)
        if len(remediation_threads) != len(live_threads):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["remediation_threads"] = remediation_threads
        item.payload["remediation_thread_snapshots"] = [dict(thread) for thread in live_threads]
        item.payload["unresolved_threads_before_address"] = len(remediation_threads)
        if not remediation_threads:
            return Continue(next_state=EVAL)
        return Continue(next_state=DIFFICULTY_WAIT)

    def _address(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """ADDRESS_WAIT: dispatch the fresh-PR or existing-PR address job.

        Fresh-PR path (this pipeline created the PR): resume the implementer
        session with the review feedback (doc step 5,
        ``get_impl_resume_feedback_prompt``). Existing-PR path (adopted by
        the implementation GATE fast path): run the address-review session
        against the PR's unresolved threads (``get_address_review_prompt``,
        with any carried ``unaddressed_findings`` rendering the
        ``build_unaddressed_directive`` retry block, #1575).

        Fail-closed worktree guard: address jobs EDIT code, so they must
        never run in the shared checkout (wrong branch — it would commit
        fixes onto whatever the shared tree has checked out). Without a
        worktree the item fails back to implementation, whose GATE/worktree
        leg is the designated recovery (bounded by the M1 agent_error
        budget consumption).
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.payload.get("existing_pr") and not ctx.github.pr_head_is_writable(item.pr):
            logger.warning(
                "pr_review:%s: PR #%d head is not writable through this repository; "
                "refusing to address a fork from the base origin",
                item.issue,
                item.pr,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "pr_head_not_writable")
        if not item.worktree:
            logger.warning(
                "pr_review:%s: no worktree for the address step; failing back "
                "(never edit in the shared checkout)",
                item.issue,
            )
            return self._fail_back_agent_error(item)
        if item.payload.get("existing_pr"):
            remediation_threads = item.payload.get("remediation_threads") or []
            if not isinstance(remediation_threads, list):
                return StageOutcome(Disposition.FINISH_FAIL, "remediation_threads_invalid")
            scope_retraction_paths = _scope_retraction_paths(remediation_threads)
            if scope_retraction_paths is None:
                return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_path_invalid")
            if scope_retraction_paths:
                base_sha = item.payload.get("reviewed_pr_base_sha")
                if not is_full_commit_sha(base_sha):
                    return StageOutcome(
                        Disposition.FINISH_FAIL,
                        "scope_retraction_base_unavailable",
                    )
                item.payload["scope_retraction_paths"] = scope_retraction_paths
            else:
                item.payload.pop("scope_retraction_paths", None)
            task_parts = [
                f"Linked issue #{item.issue}: {item.payload.get('issue_title', '')}".strip(),
                str(item.payload.get("issue_body", "")),
            ]
            pr_description = str(item.payload.get("pr_description", ""))
            if pr_description:
                task_parts.append(f"PR description:\n{pr_description}")
            job = AgentJob(
                repo=item.repo,
                issue=item.issue if item.issue is not None else 0,
                agent=agent_provider(ctx),
                model=stage_model(ctx, "implementer", implementer_model),
                prompt_builder=get_address_review_prompt,
                cwd=_worktree_path(item, ctx),
                timeout_s=address_review_claude_timeout(),
                session_agent=AGENT_ADDRESS_REVIEW,
                resume_session_id=item.session_ids.get(AGENT_ADDRESS_REVIEW),
                prompt_kwargs={
                    "pr_number": item.pr,
                    "issue_number": item.issue,
                    "worktree_path": item.worktree,
                    "threads_json": json.dumps(item.payload.get("remediation_threads", [])),
                    "todo_block": item.payload.get("difficulty_tiers", ""),
                    "task_block": "\n\n".join(part for part in task_parts if part),
                    "diff_text": str(item.payload.get("pr_diff", "")),
                    "scope_retraction_paths": scope_retraction_paths or (),
                    "host_verification_failure": item.payload.get("host_verification_failure"),
                    # No-commit retry directive (#1575): non-empty ONLY on
                    # the one retry after a no-commit address turn;
                    # get_address_review_prompt renders it via
                    # build_unaddressed_directive.
                    "unaddressed_findings": list(item.payload.get("unaddressed_findings") or []),
                },
                parse=_parse_addressed_block,
                descr="address",
            )
            return JobRequest(job, on_done_state=PUSH_WAIT)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue if item.issue is not None else 0,
            agent=agent_provider(ctx),
            model=stage_model(ctx, "implementer", implementer_model),
            prompt_builder=get_impl_resume_feedback_prompt,
            cwd=_worktree_path(item, ctx),
            timeout_s=implementer_claude_timeout(),
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=item.session_ids.get(AGENT_IMPLEMENTER),
            prompt_kwargs={
                "issue_number": item.issue,
                "prev_iteration": item.payload.get("pr_review_round", 0),
                "review_feedback": _address_review_feedback(item),
            },
            parse=_parse_addressed_block,
            descr="address",
        )
        return JobRequest(job, on_done_state=PUSH_WAIT)

    @staticmethod
    def _restart_direct_pr_review(item: WorkItem) -> StageOutcome | None:
        """Preserve a drifted checkout and route the PR through a fresh review."""
        if not item.worktree:
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_recovery_worktree_missing")
        generation = item.payload.get("direct_pr_worktree_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            return StageOutcome(Disposition.FINISH_FAIL, "direct_pr_worktree_generation_invalid")
        item.worktree = ""
        item.payload["existing_pr"] = True
        item.payload.pop("direct_pr_worktree", None)
        item.payload.pop("direct_pr_worktree_dirty", None)
        item.payload["direct_pr_worktree_generation"] = generation + 1
        item.session_ids.pop(AGENT_PR_REVIEWER, None)
        item.session_ids.pop(AGENT_ADDRESS_REVIEW, None)
        _clear_round_review_state(item)
        return None

    @staticmethod
    def _retry_pending_implementation_reply_handoff(item: WorkItem, ctx: StageContext) -> str:
        """Retry one exact post-push reply batch without invoking an agent.

        Returns ``none`` when no handoff exists, ``completed`` when every
        reply has a host receipt, ``visibility_wait`` while GitHub is briefly
        catching up with the pushed head, ``stale`` when the exact pushed head
        can no longer safely receive the saved response, ``invalid`` for
        malformed persisted state, and ``retry`` for a bounded
        transient/incomplete host operation.  No outcome grants reviewer
        authority; normal fresh review still validates and resolves the replies.
        """
        raw_handoff = item.payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF)
        if raw_handoff is None:
            return "none"
        handoff = _implementation_reply_handoff(
            raw_handoff.get("head_sha") if isinstance(raw_handoff, dict) else None,
            raw_handoff.get("threads") if isinstance(raw_handoff, dict) else None,
            raw_handoff.get("replies") if isinstance(raw_handoff, dict) else None,
            raw_handoff.get("batch_nonce") if isinstance(raw_handoff, dict) else None,
        )
        if handoff is None or item.pr is None:
            return "invalid"
        head_sha = handoff["head_sha"]
        threads = handoff["threads"]
        replies = handoff["replies"]
        batch_nonce = handoff["batch_nonce"]
        try:
            state = ctx.github.gh_pr_state(item.pr)
            if not _pr_is_current_open_head(state, head_sha):
                if (
                    isinstance(state, dict)
                    and state.get("state") == "OPEN"
                    and state.get("autoMergeRequest") is None
                ):
                    visibility_retries = item.payload.get(
                        _PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, 0
                    )
                    if (
                        isinstance(visibility_retries, int)
                        and not isinstance(visibility_retries, bool)
                        and visibility_retries >= 0
                        and visibility_retries < IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP
                    ):
                        visibility_retries += 1
                        item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES] = (
                            visibility_retries
                        )
                        item.payload["retry_delay_s"] = float(2 ** (visibility_retries - 1))
                        logger.info(
                            "pr_review:%s: waiting for pushed implementation head visibility "
                            "before replying to review threads (%d/%d)",
                            item.issue,
                            visibility_retries,
                            IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRY_CAP,
                        )
                        return "visibility_wait"
                item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
                item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
                item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
                return "stale"
            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES, None)
            result = ctx.github.post_implementation_thread_replies(
                item.pr,
                expected_head_sha=head_sha,
                threads=threads,
                replies=replies,
                batch_nonce=batch_nonce,
            )
        except Exception as error:
            logger.warning(
                "pr_review:%s: implementation reply handoff retry failed (%s)",
                item.issue,
                type(error).__name__,
            )
            return "retry"

        expected_ids = set(replies)
        replied = set(getattr(result, "replied_thread_ids", ()))
        receipts = list(getattr(result, "receipts", ()))
        if replied == expected_ids and len(receipts) == len(replied):
            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            return "completed"
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
            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF, None)
            item.payload.pop(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, None)
            return "stale"
        if not replied.issubset(expected_ids) or len(receipts) != len(replied):
            return "invalid"
        replacement = _implementation_reply_handoff(
            head_sha,
            [snapshot for snapshot in threads if str(snapshot.get("id") or "") in retryable_ids],
            {
                thread_id: reply
                for thread_id, reply in replies.items()
                if thread_id in retryable_ids
            },
            batch_nonce,
        )
        if replacement is None:
            return "invalid"
        item.payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF] = replacement
        return "retry"

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901 - state-machine gate
        """EVAL [M]: apply the structural-audit gate and review budget.

        Every durable write below happens BEFORE the outcome that causes a
        queue push. The round counters (lifetime ``attempts`` audit trail
        and cycle-relative ``payload`` gate) advance here, and only for real
        audits — never for malformed or missing audits (#911/#1554/#1794).
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        payload = item.payload

        if payload.pop("scope_retraction_failure", False):
            logger.warning(
                "pr_review:%d: refusing to publish incomplete scope retraction",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "scope_retraction_incomplete")

        handoff_status = self._retry_pending_implementation_reply_handoff(item, ctx)
        if handoff_status == "visibility_wait":
            return StageOutcome(Disposition.RETRY, "implementation_reply_handoff_visibility_wait")
        if handoff_status == "invalid":
            logger.error(
                "pr_review:%d: refusing to replay malformed implementation reply handoff",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
        if handoff_status == "retry":
            retries = payload.get(_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES, 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_invalid")
            retries += 1
            payload[_PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES] = retries
            if retries <= IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP:
                logger.warning(
                    "pr_review:%d: retrying exact implementation reply handoff %d/%d",
                    item.issue,
                    retries,
                    IMPLEMENTATION_REPLY_HANDOFF_RETRY_CAP,
                )
                return StageOutcome(Disposition.RETRY, "implementation_reply_handoff_retry")
            logger.error(
                "pr_review:%d: implementation reply handoff retry cap reached",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_reply_handoff_failed")

        detached_push_failure = payload.pop("detached_push_failure", None)
        if detached_push_failure == "remote_unchanged":
            source_sha = payload.pop("detached_push_head_sha", None)
            if not is_full_commit_sha(source_sha):
                logger.warning(
                    "pr_review:%d: detached push retry receipt lacks an exact local head",
                    item.issue,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "detached_push_retry_receipt_invalid")
            retries = int(payload.get("direct_push_retries", 0))
            if retries < DIRECT_PUSH_RETRY_CAP:
                payload["direct_push_retries"] = retries + 1
                payload["detached_push_retry_head_sha"] = source_sha
                logger.warning(
                    "pr_review:%d: detached push failed with unchanged remote; "
                    "retrying exact commit",
                    item.issue,
                )
                return Continue(next_state=PUSH_WAIT)
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push retry cap reached; preserving checkout",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
        if detached_push_failure == "remote_changed":
            restarts = payload.get("direct_push_remote_changed_restarts", 0)
            if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
                return StageOutcome(
                    Disposition.FINISH_FAIL,
                    "detached_push_recovery_receipt_invalid",
                )
            if restarts >= DIRECT_PUSH_REMOTE_CHANGED_RESTART_CAP:
                payload["detached_push_failure"] = detached_push_failure
                logger.warning(
                    "pr_review:%d: detached push remote-change recovery cap reached; "
                    "preserving checkout",
                    item.issue,
                )
                return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
            payload["direct_push_remote_changed_restarts"] = restarts + 1
            recovery = self._restart_direct_pr_review(item)
            if recovery is not None:
                return recovery
            logger.warning(
                "pr_review:%d: detached push saw changed remote head; "
                "restarting from a fresh checkout",
                item.issue,
            )
            return Continue(next_state=ENTER)
        if detached_push_failure == "remote_changed_unrecorded":
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push remote-change receipt could not be recorded; "
                "preserving checkout",
                item.issue,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")
        if detached_push_failure in {
            "remote_unconfirmed",
            "retry_checkout_changed",
            "retry_checkout_unconfirmed",
        }:
            payload["detached_push_failure"] = detached_push_failure
            logger.warning(
                "pr_review:%d: detached push recovery state %s; preserving checkout",
                item.issue,
                detached_push_failure,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_failed")

        address_error = self._handle_address_error(item)
        if address_error is not None:
            return address_error

        if payload.pop("review_refresh_required", False):
            # A successful address push changed the reviewed head. Cross the
            # checkout barrier and obtain a new audit before consulting live
            # threads or writing any implementation-state label.
            return self._compact_before_next_review(item, ctx)

        # Real-commit gate (#1575, M4): a no-commit push retries the address
        # once with the directive; the second no-commit turn falls through
        # and is evaluated as an unaddressed round.
        no_commit_retry = self._gate_no_commit(item)
        if no_commit_retry is not None:
            return no_commit_retry

        audit = payload.get("review_audit")
        if payload.pop("review_audit_failure", False) or not isinstance(audit, ReviewAudit):
            return self._handle_error_verdict(item, ReviewAudit(None, "", (), "", valid=False))
        if not audit.valid:
            return self._handle_error_verdict(item, audit)
        if not item.payload.get("reviewed_pr_head_sha"):
            # Addressing a finding or pushing a new commit clears the prior
            # head proof. A fresh negative transition may still be based on
            # durable blocking-thread facts, but a clean result must return
            # through REVIEW_WAIT so its positive label is bound to a fresh
            # checkout/review.
            try:
                live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            except Exception:
                return self._compact_before_next_review(item, ctx)
            unresolved_count = len(live_threads)
            if not unresolved_count:
                return self._compact_before_next_review(item, ctx)
            bind_outcome = self._bind_current_head_for_negative(item, ctx)
            if bind_outcome is not None:
                return bind_outcome
            payload["review_error_retries"] = 0
            round_done = payload.get("pr_review_round", 0) + 1
            payload["pr_review_round"] = round_done
            item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
            return self._handle_non_go(
                item,
                ctx,
                audit,
                unresolved_count,
                unresolved_count,
                round_done,
                ctx.budget("pr_review_iter"),
                ctx.budget("pr_review_hard"),
            )

        # A fresh total open-thread count after the address/push leg is the
        # only thread fact that can downgrade a GO decision.
        try:
            live_threads = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: fresh review-thread read failed (%s)",
                item.issue,
                type(error).__name__,
            )
            return self._handle_error_verdict(item, None)
        item.payload["unresolved_threads"] = [dict(thread) for thread in live_threads]
        open_thread_count = len(live_threads)

        # A valid structural audit is a real review result. Grade, summary,
        # and supplemental feedback never select the implementation state.
        payload["review_error_retries"] = 0
        round_done = payload.get("pr_review_round", 0) + 1
        payload["pr_review_round"] = round_done
        item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
        soft_cap = ctx.budget("pr_review_iter")
        hard_cap = ctx.budget("pr_review_hard")
        if round_done > soft_cap:
            # Audit trail of progress-earned extension rounds (4..hard_cap).
            item.attempts["pr_review_hard"] = item.attempts.get("pr_review_hard", 0) + 1

        if not open_thread_count:
            return self._handle_clean_go(item, ctx)

        return self._handle_non_go(
            item,
            ctx,
            audit,
            open_thread_count,
            open_thread_count,
            round_done,
            soft_cap,
            hard_cap,
        )

    def _handle_non_go(
        self,
        item: WorkItem,
        ctx: StageContext,
        verdict: Any,
        open_thread_count: int,
        unresolved_count: int,
        round_done: int,
        soft_cap: int,
        hard_cap: int,
    ) -> StepResult:
        """Persist a non-GO round and choose its bounded retry or terminal route."""
        if item.pr is None or item.issue is None:  # guarded by _eval; type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        guard_outcome = self._write_no_go(item, ctx)
        if guard_outcome is not None:
            return guard_outcome
        # #1863: prev_unresolved is THIS round's pre-address snapshot
        # (POST's unresolved_threads_before_address) so the extension gate compares
        # pre-address vs post-address WITHIN the round being evaluated —
        # progress landing on the soft-cap round is no longer invisible
        # to a stale cross-round comparison.
        prev_unresolved = item.payload.get("unresolved_threads_before_address")
        if round_done < soft_cap:
            logger.info(
                "pr_review:%d: %s (round %d/%d, %d unresolved); re-reviewing",
                item.issue,
                "structured audit",
                round_done,
                soft_cap,
                unresolved_count,
            )
            return self._compact_before_next_review(item, ctx)
        made_progress = prev_unresolved is not None and open_thread_count < prev_unresolved
        if round_done < hard_cap and made_progress:
            # #1554 progress-aware extension: rounds soft_cap+1..hard_cap are
            # admitted only while the total open-thread count strictly decreases.
            logger.info(
                "pr_review:%d: extension round %d/%d earned (%s -> %d open threads)",
                item.issue,
                round_done + 1,
                hard_cap,
                prev_unresolved,
                open_thread_count,
            )
            return self._compact_before_next_review(item, ctx)

        logger.warning(
            "pr_review:%d: exhausted at round %d (open threads %s -> %d); applying %s",
            item.issue,
            round_done,
            prev_unresolved,
            open_thread_count,
            STATE_SKIP,
        )
        # Reuse the exact-head guard after the durable NO-GO write: a push in
        # that window invalidates this exhaustion decision and must re-review
        # the newer head instead of applying state:skip to it.
        arm_outcome = self._require_reviewed_unarmed(item, ctx)
        if arm_outcome is not None:
            return arm_outcome
        write_skip_label(
            item.issue,
            ctx,
            f"PR review rounds exhausted at round {round_done} with the "
            f"open-thread count stuck "
            f"({prev_unresolved} -> {open_thread_count}); further re-review "
            f"cannot make progress. Push new commits addressing the review "
            f"feedback, then remove this label to re-enter the loop.",
        )
        return StageOutcome(Disposition.SKIP, "exhaustion")

    @staticmethod
    def _compact_before_next_review(item: WorkItem, ctx: StageContext) -> Continue:
        """Compact both persisted sessions before continuing the next review round."""
        if item.worktree:
            return Continue(next_state=COMPACT_REVIEWER_WAIT)
        return Continue(next_state=REVIEW_WAIT)

    def _handle_address_error(self, item: WorkItem) -> StageOutcome | None:
        """Fail back hard address/push errors with explicit retry cleanup."""
        payload = item.payload
        if not payload.pop("address_error", None):
            return None

        if payload.get("no_commit_retry_done") or payload.get("unaddressed_findings"):
            payload.pop("push_no_commit", None)
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
            logger.warning(
                "pr_review:%d: no-commit retry address/push leg failed; "
                "consuming retry directive and failing back agent_error without "
                "burning a review round",
                item.issue,
            )
            return self._fail_back_agent_error(item)

        # The address/push leg hard-failed: the doc's agent_error route —
        # back to implementation for a fresh implement pass (bounded by
        # the implement budget). No labels, no round burned.
        logger.warning("pr_review:%d: address step failed; failing back", item.issue)
        return self._fail_back_agent_error(item)

    def _handle_clean_go(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Apply review GO only after the complete unresolved-thread read is empty."""
        if item.pr is None or item.issue is None:  # guarded by caller; narrowing
            return self._fail_back_agent_error(item)
        logger.info(
            "pr_review:%d: clean structural audit; advancing PR #%d to merge wait",
            item.issue,
            item.pr,
        )
        return self._write_go(item, ctx)

    @staticmethod
    def _gate_no_commit(item: WorkItem) -> Continue | None:
        """Apply the real-commit gate (#1575): a no-commit push is never "addressed".

        A push that produced NO commit means the address turn self-reported
        a phantom fix. The FIRST such turn retries the address once, carrying
        the still-open threads as ``unaddressed_findings`` (rendered by
        ``build_unaddressed_directive`` inside ``get_address_review_prompt``)
        to re-ground the resumed session. A SECOND consecutive no-commit turn
        returns None so EVAL treats it as an unaddressed round. A real commit
        spends/clears the retry directive (legacy: "a progress round clears
        the retry directive").

        Args:
            item: The work item under evaluation.

        Returns:
            ``Continue(ADDRESS_WAIT)`` for the one retry, else None.

        """
        payload = item.payload
        no_commit = payload.pop("push_no_commit", None)
        if no_commit:
            if not payload.get("no_commit_retry_done"):
                payload["no_commit_retry_done"] = True
                retry_threads = payload.get("remediation_threads") or []
                payload["unaddressed_findings"] = [dict(t) for t in retry_threads]
                logger.warning(
                    "pr_review:%s: address turn produced NO commit; retrying the "
                    "address once with the unaddressed-findings directive (#1575)",
                    item.issue,
                )
                return Continue(next_state=ADDRESS_WAIT)
            logger.warning(
                "pr_review:%s: address retry still produced no commit; "
                "treating this as an unaddressed round",
                item.issue,
            )
        elif no_commit is False:
            payload.pop("no_commit_retry_done", None)
            payload.pop("unaddressed_findings", None)
        return None

    def _handle_error_verdict(self, item: WorkItem, verdict: Any) -> StageOutcome:
        """Handle a missing/ERROR verdict: bounded RETRY, then fail back.

        Reviewer-infrastructure failure: labels untouched, no round burned,
        RETRY — bounded by the consecutive-failure cap (plan_review
        pattern), then fail back ``agent_error`` (#911/#1554/#1794).

        Args:
            item: The work item under evaluation.
            verdict: The stored verdict (None or an ERROR verdict).

        Returns:
            RETRY below the cap; the flagged agent_error fail-back at it.

        """
        payload = item.payload
        reason = "no review audit found" if verdict is None else "review audit format failure"
        retries = payload.get("review_error_retries", 0) + 1
        payload["review_error_retries"] = retries
        if retries > REVIEW_ERROR_RETRY_CAP:
            logger.error(
                "pr_review:%s: %s; %d consecutive reviewer failures (cap %d)"
                " — failing back to implementation",
                item.issue,
                reason,
                retries,
                REVIEW_ERROR_RETRY_CAP,
            )
            return self._fail_back_agent_error(item)
        logger.warning(
            "pr_review:%s: %s; retry %d/%d (no round burned)",
            item.issue,
            reason,
            retries,
            REVIEW_ERROR_RETRY_CAP,
        )
        return StageOutcome(Disposition.RETRY, reason)

    @staticmethod
    def _fail_back_agent_error(item: WorkItem) -> StageOutcome:
        """FAIL_BACK ``agent_error``, flagging the re-entry for the M1 bound.

        Every agent_error fail-back marks
        ``payload["agent_error_failback"]`` so the implementation GATE's
        existing-PR adoption consumes the ``implement`` budget — without a
        moving counter the fail-back -> adopt -> ADVANCE cycle would
        ping-pong forever.

        Args:
            item: The work item failing back.

        Returns:
            The FAIL_BACK(``agent_error``) outcome.

        """
        item.payload["agent_error_failback"] = True
        return StageOutcome(Disposition.FAIL_BACK, "agent_error")

    @staticmethod
    def _require_reviewed_unarmed(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Verify the live unarmed PR is the exact head reviewed this round.

        No pipeline stage owns auto-merge. A non-null or unreadable request is
        consequently an external or ambiguous state, so this method is a
        strict non-mutation boundary. A missing or changed head invalidates
        the in-memory review proof and sends the item back through REVIEW_WAIT.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        pr_state = ctx.github.gh_pr_state(pr_number)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        live_head = str(pr_state.get("headRefOid") or "")
        if not reviewed_head or not live_head or reviewed_head != live_head:
            item.payload.pop("reviewed_pr_head_sha", None)
            return Continue(next_state=REVIEW_WAIT)
        return None

    @staticmethod
    def _revalidate_go_write(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Check the nonconditional GO write against fresh state and labels.

        GitHub exposes no conditional label mutation. A push or external
        label write can therefore race after the pre-write guard. A read after
        our write cannot prove who owns an exclusive GO label, so a changed or
        missing reviewed head only discards this process's proof and restarts
        review. A complete thread read after the label write detects review
        activity in the remaining admission window. This run cannot establish
        ownership of a label after that race, so it preserves the live
        threads for a fresh automation pass and makes no further label
        mutation.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        try:
            state = ctx.github.gh_pr_state(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if isinstance(state, dict) and state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
        live_head = str(state.get("headRefOid") or "") if isinstance(state, dict) else ""
        if not reviewed_head or not live_head or reviewed_head != live_head:
            item.payload.pop("reviewed_pr_head_sha", None)
            return Continue(next_state=REVIEW_WAIT)
        try:
            live_threads = ctx.github.list_unresolved_review_threads(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to reread review threads after GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if live_threads:
            return PrReviewStage._handle_late_threads_after_go_write(
                item,
                len(live_threads),
                ctx,
            )
        try:
            has_go, has_no_go = ctx.github.pr_has_implementation_state_label(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to revalidate GO write on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")
        if _is_confirmed_open_unarmed(state) and has_go and not has_no_go:
            return None
        return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_readback_failed")

    @staticmethod
    def _handle_late_threads_after_go_write(
        item: WorkItem,
        unresolved_threads: int,
        ctx: StageContext,
    ) -> StageOutcome:
        """Stand down after a post-GO thread race without touching state labels.

        The GO write is non-conditional. A concurrent actor may own the current
        implementation state by the time the late thread is observed, so
        clearing or replacing a label would be an unsafe mutation. The next
        loop invocation must start a new review proof before it can validate
        and reconcile those threads.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        logger.warning(
            "pr_review:%d: %d review thread(s) appeared during GO admission on PR #%d; "
            "standing down without label changes",
            item.issue,
            unresolved_threads,
            item.pr,
        )
        return StageOutcome(Disposition.FINISH_FAIL, "review_activity_changed")

    @staticmethod
    def _bind_current_head_for_negative(item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Bind the current open head for a negative-only transition."""
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        state = ctx.github.gh_pr_state(item.pr)
        if state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        head = str(state.get("headRefOid") or "")
        if not head:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_head_unavailable")
        item.payload["reviewed_pr_head_sha"] = head
        return None

    @staticmethod
    def _require_confirmed_unarmed(pr_number: int, ctx: StageContext) -> StageOutcome | None:
        """Verify a live PR is open and unarmed before an unrelated mutation."""
        pr_state = ctx.github.gh_pr_state(pr_number)
        if pr_state is None:
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
        if pr_state.get("autoMergeRequest") is not None:
            return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
        if not _is_confirmed_open_unarmed(pr_state):
            return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
        return None

    @staticmethod
    def _write_no_go(item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Durably mark NO-GO after fresh exact-head and label checks.

        Label writes have no compare-and-set operation.  Re-read the live PR
        state and exclusive implementation labels after the write, and never
        attempt a compensating mutation if that proof is lost: a concurrent
        actor may own the current state by then.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        arm_outcome = PrReviewStage._require_reviewed_unarmed(item, ctx)
        if arm_outcome is not None:
            return arm_outcome
        try:
            ctx.github.mark_pr_implementation_no_go(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to mark PR #%d implementation-no-go: %s",
                pr_number,
                error,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_label_failed")
        post_write_guard = PrReviewStage._require_reviewed_unarmed(item, ctx)
        if post_write_guard is not None:
            return post_write_guard
        try:
            has_go, has_no_go = ctx.github.pr_has_implementation_state_label(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: failed to verify PR #%d implementation-no-go: %s",
                pr_number,
                error,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_readback_failed")
        if has_go or not has_no_go:
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_no_go_readback_failed")
        return None

    @staticmethod
    def _write_go(item: WorkItem, ctx: StageContext) -> StepResult:
        """Apply GO only to the exact live head reviewed in this process.

        Args:
            item: Work item carrying the in-memory reviewed-head proof.
            ctx: Stage context carrying the GitHub accessor.

        Returns:
            The next stage result.

        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        try:
            live_threads = ctx.github.list_unresolved_review_threads(pr_number)
        except Exception as error:
            logger.warning(
                "pr_review: final review-thread read failed on PR #%d (%s)",
                pr_number,
                type(error).__name__,
            )
            return StageOutcome(Disposition.FINISH_FAIL, "review_threads_unavailable")
        if live_threads:
            logger.info(
                "pr_review: clean GO recheck found %d open thread(s) on PR #%d; "
                "restarting automation review",
                len(live_threads),
                pr_number,
            )
            return Continue(next_state=REVIEW_WAIT)
        arm_outcome = PrReviewStage._require_reviewed_unarmed(item, ctx)
        if arm_outcome is not None:
            return arm_outcome
        try:
            ctx.github.mark_pr_implementation_go(pr_number)
        except Exception as error:
            logger.error("pr_review: failed to mark PR #%d implementation-go: %s", pr_number, error)
            return StageOutcome(Disposition.FINISH_FAIL, "implementation_go_label_failed")
        postwrite_outcome = PrReviewStage._revalidate_go_write(item, ctx)
        if postwrite_outcome is not None:
            return postwrite_outcome
        return StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
