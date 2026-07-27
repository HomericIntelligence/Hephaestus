"""PR-review stage: review, validate, post, address, and evaluate.

Re-houses the legacy implementation-review semantics now isolated in
``_review_loop.ReviewLoopCoordinator`` and
``_review_conflict_resolver.ReviewConflictResolver``. The queue stage remains
the live implementation of the review/validate/address state machine. Its
collaborators include
(``pr_reviewer.review_pr_inline``, ``review_validator
.validate_prior_comments_addressed``, ``address_review
.run_address_fix_session``) as a pipeline stage
(docs/architecture.md §5.5 "pr_review" is the binding
contract):

- States: ENTER -> REVIEW_WAIT -> VALIDATE_WAIT -> POST -> DIFFICULTY_WAIT
  -> ADDRESS_WAIT -> PUSH_WAIT -> EVAL -> COMPACT_REVIEWER_WAIT
  -> COMPACT_WRITER_WAIT -> REVIEW_WAIT or terminal advance to ``merge_wait``.
  The legacy follow-up mini-states have been retired (#2140); a clean GO
  advances to ``merge_wait`` from EVAL.
- Budgets: ``pr_review_iter`` = 3 (soft cap), ``pr_review_hard`` = 6 (hard
  cap; rounds 4-6 are admitted ONLY while the unresolved-thread count
  strictly decreases — the #1554 progress-aware extension, legacy
  ``_review_thread_count_decreased`` +
  :class:`_review_loop.ReviewLoopCoordinator`'s progress-extension contract).
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
- EVAL structural-audit semantics: eligibility requires ZERO unresolved
  blocking automation threads (#1152). Any open HUMAN thread -> HUMAN_BLOCKED:
  an explanatory PR comment is posted [durable, before the outcome] naming the
  blocking human thread count and automation stands down, then finish failed
  without mutating implementation-state labels (a human must act; automation
  cannot prove ownership of a concurrent label transition). Any open blocking
  automation thread -> no-go label and
  address + re-review. A clean audit ->
  ``_write_go`` performs one final complete-thread live-read, requires a
  confirmed-unarmed live PR, and applies ``state:implementation-go``.
  The checkout GitJob-proven reviewed head accompanies that label;
  ``merge_wait`` verifies it before each bounded SHA-conditional normal merge
  attempt. Every
  real blocking round durably writes ``state:implementation-no-go`` before
  looping/regressing, non-fatally. Exhaustion -> durably
  apply ``state:skip`` [durable] -> SKIP.
- Downgraded-GO cost (DELIBERATE 2-round divergence from legacy): legacy
  downgraded a GO with open automation threads and ran the address step in
  the SAME iteration; this stage records the downgrade in EVAL and lets
  the NEXT round's POST re-count the live threads before dispatching the
  address leg, so a downgraded GO costs one extra review round. Chosen
  because POST live-checks the unresolved counts (a thread resolved
  out-of-band between rounds skips the address leg entirely) and the
  budget/extension gate stays a single chokepoint in EVAL.
- Progress metric (#1554 parity): the extension gate compares AUTOMATION
  unresolved counts only — a human resolving their own thread is not
  automation progress and must not earn extension rounds.
- POST posts only SURVIVING threads: the round's reviewer threads are
  filtered through the validation job's verdict
  (:func:`_surviving_threads`, re-housed ``review_validator`` semantics —
  ``wont_fix`` findings are accepted and dropped, ``unaddressed`` prior
  findings are re-opened as new postable threads; an unparseable
  validator output filters nothing, the legacy fail-open).
- Real-commit gating (#1575): PUSH_WAIT's commit_push result is inspected
  in EVAL. A push that produced NO commit (the fix agent punted or
  self-reported a phantom fix) is NOT treated as addressed: the address
  step is retried ONCE with the ``build_unaddressed_directive`` block
  (via ``get_address_review_prompt``'s ``unaddressed_findings``), and a
  second consecutive no-commit turn is evaluated as an unaddressed round.
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

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

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
    render_review_audit,
)
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_COMMENT_CLASSIFIER,
    AGENT_IMPLEMENTER,
    AGENT_PR_REVIEWER,
)
from hephaestus.automation.state_labels import STATE_SKIP

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

_STEP_HANDLER_NAMES: dict[str, str] = {
    ENTER: "_enter",
    ADOPT_WORKTREE_WAIT: "_adopt_worktree_wait",
    REVIEW_WAIT: "_review_wait",
    REVIEW_CHECKOUT_WAIT: "_review_checkout_wait",
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
    "validation_process_threads",
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
    the response (``{"unaddressed": [...], "wont_fix": [...]}``); the parser
    takes the LAST parseable block (legacy last-block-wins convention), then
    falls back to treating the whole output as JSON. Returns None when
    nothing parses — callers fail open.

    Args:
        raw: The validation job's stored output (str, dict, or anything).

    Returns:
        The parsed verdict dict, or None when unparseable/absent.

    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    for candidate in (*reversed(blocks), raw):
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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


def _surviving_threads(
    threads: list[dict[str, Any]],
    validation_result: Any,
) -> list[dict[str, Any]]:
    """Filter the round's reviewer threads through the validator's verdict.

    Re-housed ``review_validator`` consumption semantics (m1):

    - ``wont_fix`` entries are documented-by-design decisions — accepted,
      so any reviewer thread re-raising one of those thread ids is DROPPED
      (never re-posted; the legacy recurrence-acceptance path, #1329).
    - ``unaddressed`` entries are prior findings the current diff does not
      address. They are represented in the next audit, but a still-open
      process thread is handed to a human rather than being resolved or
      replaced by automation.

    Fail-open: a missing/unparseable validator output filters nothing — a
    validator blip must never suppress the reviewer's own findings (the
    legacy fail-open pattern).

    Args:
        threads: The round's reviewer-produced thread dicts.
        validation_result: The validation job's stored output.

    Returns:
        The surviving thread list to durably post.

    """
    surviving = [dict(t) for t in threads]
    parsed = _parse_validation_result(validation_result)
    if parsed is None:
        return surviving
    wont_fix_ids = _thread_ids(parsed.get("wont_fix"))
    if wont_fix_ids:
        surviving = [
            t for t in surviving if str(t.get("thread_id") or t.get("id") or "") not in wont_fix_ids
        ]
    present_ids = {str(t.get("thread_id") or t.get("id") or "") for t in surviving}
    unaddressed = parsed.get("unaddressed")
    if isinstance(unaddressed, list):
        for entry in unaddressed:
            if not isinstance(entry, dict):
                continue
            thread_id = str(entry.get("thread_id") or entry.get("id") or "")
            if thread_id and thread_id in present_ids:
                continue  # reviewer already re-raised it this round
            detail = (
                str(entry.get("detail") or "").strip()
                or str(entry.get("original_body") or "").strip()
                or "prior review comment not addressed"
            )
            surviving.append(
                {
                    "path": entry.get("path") or "",
                    "line": entry.get("line"),
                    "side": "RIGHT",
                    "severity": "major",
                    "body": f"Reopened (prior round, still unaddressed): {detail}",
                    # The live reconciliation below uses this host-presented
                    # identity to suppress a replacement only when the old
                    # receipt is still actually open. A stale validation
                    # snapshot alone must never lose a finding.
                    "prior_thread_id": thread_id,
                }
            )
    return surviving


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


def _thread_is_automation_owned(thread: dict[str, Any]) -> bool:
    """Classify a fresh GitHub thread by its durable author facts."""
    if isinstance(thread.get("automation_owned"), bool):
        return bool(thread["automation_owned"])
    authors: set[str] = set()
    for key in ("author", "authors"):
        value = thread.get(key)
        if isinstance(value, str):
            authors.add(value.strip())
        elif isinstance(value, list):
            authors.update(str(author).strip() for author in value if str(author).strip())
    for comment in thread.get("comments", []):
        if isinstance(comment, dict) and comment.get("author"):
            authors.add(str(comment["author"]).strip())
    return bool(
        authors
        & {
            "github-actions[bot]",
            "hephaestus[bot]",
        }
    )


def _durable_thread_id(thread: dict[str, Any]) -> str | None:
    """Return one non-empty durable GraphQL thread id, if present."""
    value = thread.get("id") or thread.get("thread_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _thread_comment_signature(thread: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    """Return a complete immutable participant/body signature for a thread.

    The GitHub accessor fails closed rather than returning a truncated comment
    page. Requiring this snapshot to remain exact means a later reply cannot
    be mistaken for the process's original single-comment thread, even when a
    human happens to use the same login as the automation host.
    """
    comments = thread.get("comments")
    if not isinstance(comments, list) or not comments:
        return None
    signature: list[tuple[str, str]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            return None
        author = comment.get("author")
        body = comment.get("body")
        if not isinstance(author, str) or not author.strip() or not isinstance(body, str):
            return None
        signature.append((author.strip(), body))
    return tuple(signature)


def _is_process_thread_receipt(raw: dict[str, Any]) -> bool:
    """Return whether a post-time or host-normalized stale receipt is exact."""
    review_id = raw.get("review_id")
    line = raw.get("line")
    comments = raw.get("comments")
    initial = comments[0] if isinstance(comments, list) and len(comments) == 1 else None
    external_bot = raw.get("external_bot") is True
    return bool(
        _durable_thread_id(raw)
        and isinstance(review_id, str)
        and review_id.strip()
        and isinstance(comments, list)
        and len(comments) == 1
        and isinstance(initial, dict)
        and isinstance(initial.get("id"), str)
        and initial["id"].strip()
        and initial.get("review_id") == review_id
        and (
            (isinstance(line, int) and not isinstance(line, bool) and line > 0)
            or (line is None and raw.get("restart_stale_line") is True)
        )
        and _thread_comment_signature(raw) is not None
        and (
            not external_bot
            or (
                raw.get("author_type") == "Bot"
                and isinstance(initial, dict)
                and initial.get("author_type") == "Bot"
            )
        )
    )


def _process_thread_records(item: WorkItem) -> dict[str, dict[str, Any]] | None:
    """Load the process-only post receipts, rejecting malformed identities."""
    raw_records = item.payload.get("process_review_threads", [])
    if not isinstance(raw_records, list):
        return None
    records: dict[str, dict[str, Any]] = {}
    for raw in raw_records:
        if not isinstance(raw, dict) or not _is_process_thread_receipt(raw):
            return None
        thread_id = _durable_thread_id(raw)
        if thread_id is None or thread_id in records:
            return None
        records[thread_id] = raw
    return records


def _adopt_live_process_receipts(
    records: dict[str, dict[str, Any]], live_threads: list[dict[str, Any]]
) -> dict[str, dict[str, Any]] | None:
    """Merge host-normalized restart receipts into the single receipt set.

    ``PipelineGitHub`` attaches ``process_receipt`` only after proving the
    marker, sole-comment shape, automated-review parent, and review commit.
    The stage still verifies the complete receipt against the same live thread
    before letting the validator describe it as addressed.
    """
    adopted = dict(records)
    for live in live_threads:
        if not isinstance(live, dict):
            return None
        raw_receipt = live.get("process_receipt")
        if raw_receipt is None:
            continue
        if not isinstance(raw_receipt, dict) or not _is_process_thread_receipt(raw_receipt):
            return None
        thread_id = _durable_thread_id(raw_receipt)
        if thread_id is None or thread_id != _durable_thread_id(live):
            return None
        if _thread_comment_signature(raw_receipt) != _thread_comment_signature(live):
            return None
        existing = adopted.get(thread_id)
        if existing is not None and existing != raw_receipt:
            return None
        adopted[thread_id] = raw_receipt
    return adopted


def _handled_process_receipts(
    item: WorkItem,
) -> tuple[list[dict[str, Any]], dict[str, str]] | None:
    """Return strictly validated receipts the validator marked as handled.

    The validator never expands mutation scope: every id must be a host-read
    receipt that was presented to that validation job.  Missing or malformed
    validation output therefore leaves the existing human handoff intact.
    """
    records = _process_thread_records(item)
    validation_threads = item.payload.get("validation_process_threads")
    parsed = _parse_validation_result(item.payload.get("validation_result"))
    if records is None or not isinstance(validation_threads, list) or parsed is None:
        return None
    unaddressed = parsed.get("unaddressed")
    wont_fix = parsed.get("wont_fix")
    if not isinstance(unaddressed, list) or not isinstance(wont_fix, list):
        return None
    validation_ids: list[str] = []
    for thread in validation_threads:
        if not isinstance(thread, dict):
            return None
        thread_id = _durable_thread_id(thread)
        if thread_id is None or thread_id not in records:
            return None
        if _thread_comment_signature(thread) != _thread_comment_signature(records[thread_id]):
            return None
        validation_ids.append(thread_id)
    if len(validation_ids) != len(set(validation_ids)):
        return None
    unaddressed_ids = _thread_ids(unaddressed)
    wont_fix_ids = _thread_ids(wont_fix)
    known_ids = set(validation_ids)
    if (
        not unaddressed_ids.issubset(known_ids)
        or not wont_fix_ids.issubset(known_ids)
        or unaddressed_ids & wont_fix_ids
    ):
        return None
    reviewed_head = item.payload.get("reviewed_pr_head_sha")
    dispositions = {
        thread_id: "addressed"
        for thread_id in validation_ids
        if (
            thread_id not in unaddressed_ids
            and thread_id not in wont_fix_ids
            and isinstance(reviewed_head, str)
            and reviewed_head.strip()
            and isinstance(records[thread_id].get("created_head_sha"), str)
            and records[thread_id]["created_head_sha"].strip()
            and records[thread_id]["created_head_sha"] != reviewed_head
        )
    }
    return ([records[thread_id] for thread_id in dispositions], dispositions)


def _live_process_threads(
    records: dict[str, dict[str, Any]], live_threads: list[dict[str, Any]]
) -> dict[str, dict[str, Any]] | None:
    """Return only fresh live threads that exactly still match process receipts.

    A retained id is not enough to call a thread a live process receipt: a
    human may have replied through the same login or otherwise changed it.
    Callers use this helper for duplicate suppression, so returning only exact
    participant/body matches ensures a validator finding is re-posted whenever
    the prior thread was resolved or has ceased to be this process's receipt.
    """
    live_by_id: dict[str, dict[str, Any]] = {}
    for thread in live_threads:
        if not isinstance(thread, dict):
            return None
        thread_id = _durable_thread_id(thread)
        if thread_id is None or thread_id in live_by_id:
            return None
        live_by_id[thread_id] = thread
    matched: dict[str, dict[str, Any]] = {}
    for thread_id, receipt in records.items():
        live = live_by_id.get(thread_id)
        if live is None:
            continue
        receipt_signature = _thread_comment_signature(receipt)
        if receipt_signature is not None and receipt_signature == _thread_comment_signature(live):
            matched[thread_id] = live
    return matched


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


def _without_duplicate_live_process_findings(
    findings: list[dict[str, Any]], live_process_threads: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep genuinely new findings while retaining their open process twins."""
    existing = {
        key for thread in live_process_threads.values() if (key := _finding_key(thread)) is not None
    }
    retained: list[dict[str, Any]] = []
    for finding in findings:
        prior_thread_id = finding.get("prior_thread_id")
        if isinstance(prior_thread_id, str) and prior_thread_id in live_process_threads:
            continue
        key = _finding_key(finding)
        if key is not None and key in existing:
            continue
        retained.append(finding)
        if key is not None:
            existing.add(key)
    return retained


def _thread_is_blocking(thread: dict[str, Any]) -> bool:
    """Recover the durable blocking severity from a fresh thread fact."""
    severity = str(thread.get("severity") or "").strip().lower()
    if severity in VALID_SEVERITIES:
        return severity in BLOCKING_SEVERITIES
    body = str(thread.get("body") or "")
    for line in body.splitlines():
        marker = "<!-- hephaestus-severity:"
        stripped = line.strip()
        if stripped.startswith(marker) and stripped.endswith("-->"):
            recovered = stripped[len(marker) : -3].strip().lower()
            if recovered not in VALID_SEVERITIES:
                return True
            return recovered in BLOCKING_SEVERITIES
    return True


def _thread_counts(threads: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return fresh ``(blocking automation, advisory automation, human)`` counts."""
    blocking = advisory = human = 0
    for thread in threads:
        if not _thread_is_automation_owned(thread):
            human += 1
        elif _thread_is_blocking(thread):
            blocking += 1
        else:
            advisory += 1
    return blocking, advisory, human


def _normalize_blocking_remediation_threads(
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize live blocking automation threads for address prompts.

    The reviewer audit contains proposed findings, not durable GitHub thread
    identities. Address jobs must instead consume the live post/read-back
    snapshot so pre-existing blockers are included and every presented finding
    carries the GraphQL thread id used to identify the required human
    verification and resolution handoff.
    Threads without a durable id are omitted; POST verifies the normalized
    count against the live blocking count and fails closed on any mismatch.
    """
    normalized: list[dict[str, Any]] = []
    for thread in threads:
        if not _thread_is_automation_owned(thread) or not _thread_is_blocking(thread):
            continue
        thread_id = str(thread.get("id") or thread.get("thread_id") or "").strip()
        if not thread_id:
            continue
        line = thread.get("line")
        normalized.append(
            {
                "thread_id": thread_id,
                "path": str(thread.get("path") or ""),
                "line": (
                    line
                    if isinstance(line, int) and not isinstance(line, bool) and line > 0
                    else None
                ),
                "body": str(thread.get("body") or ""),
            }
        )
    return normalized


def _address_review_feedback(item: WorkItem) -> str:
    """Serialize normalized live blocking threads for fresh-PR remediation."""
    threads = item.payload.get("remediation_threads")
    return json.dumps(
        {"findings": threads if isinstance(threads, list) else []},
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
      unresolved-thread counts; zero open automation threads skip the
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
        return self._submit_review_job(item, ctx)

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
        """VALIDATE_WAIT either skips the dead round or submits validation."""
        issue = _issue_number(item)
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        if item.payload.pop("review_failed", None):
            # The review job itself failed: skip the validate/post/
            # address leg — EVAL's missing-verdict ERROR path handles it
            # without burning a round.
            return Continue(next_state=EVAL)
        records = _process_thread_records(item)
        if records is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        if records:
            try:
                live_threads = ctx.github.list_unresolved_review_threads(item.pr)
            except Exception as error:
                logger.warning(
                    "pr_review:%s: could not fetch complete process-thread facts "
                    "for validation (%s)",
                    item.issue,
                    type(error).__name__,
                )
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
        else:
            try:
                live_threads = ctx.github.list_restart_process_review_threads(item.pr)
            except Exception as error:
                logger.warning(
                    "pr_review:%s: could not fetch complete process-thread facts "
                    "for restart validation (%s)",
                    item.issue,
                    type(error).__name__,
                )
                item.payload["review_audit_failure"] = True
                return Continue(next_state=EVAL)
        records = _adopt_live_process_receipts(records, live_threads)
        if records is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["process_review_threads"] = list(records.values())
        live_process = _live_process_threads(records, live_threads)
        if live_process is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        validation_threads = list(live_process.values())
        item.payload["validation_process_threads"] = [dict(thread) for thread in validation_threads]
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
                "review_context_kind": _review_context_kind(item),
            },
            descr="validate",
        )
        return JobRequest(job, on_done_state=POST)

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
        kwargs: dict[str, object] = {
            "issue_number": issue,
            "worktree_path": item.worktree,
            "branch": item.branch,
            "agent": agent_provider(ctx),
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

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
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

        review_job_pending = bool(item.payload.pop("review_job_pending", None))
        is_review_result = review_job_pending or item.state == REVIEW_WAIT
        if self._consume_failed_job(item, result, is_review_result):
            return

        if item.state == PUSH_WAIT:
            # Real-commit gate (#1575): commit_push reports whether a commit
            # was actually produced (value/changed True). A no-commit push
            # means the address turn was a phantom fix — EVAL must NOT treat
            # the round as addressed.
            produced_commit = bool(result.value)
            if produced_commit:
                # The old audit and checkout receipt describe the pre-push
                # head. Discard the entire round before EVAL can bind the new
                # head to any implementation-state transition.
                _clear_round_review_state(item)
                item.payload["review_refresh_required"] = True
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
            item.payload["address_output"] = str(result.value)

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
        if ready and not isinstance(review_diff, str):
            item.payload["review_checkout_error"] = "checkout job returned no bound diff"
            ready = False
        if ready:
            item.payload["pr_diff"] = review_diff
        item.payload["review_checkout_ready"] = ready
        return True

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
            failure = receipt.get("detached_push_failure")
            if failure in {
                "remote_changed",
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
            item.payload["address_error"] = True
        elif item.state == ADDRESS_WAIT:
            item.payload["address_error"] = True

    @staticmethod
    def _reconcile_process_threads_before_post(
        item: WorkItem, ctx: StageContext, threads: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | StepResult:
        """Suppress duplicate findings and preserve process-thread boundaries.

        Guarded receipt reconciliation runs immediately before this helper.
        A verified process receipt may proceed only when the validator
        explicitly reports it unaddressed; POST then routes its fenced finding
        through the normal address path. Any changed, replied, malformed,
        user, or validator-ambiguous thread remains a hard handoff.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        records = _process_thread_records(item)
        if records is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        if not records:
            return threads
        try:
            current_live = ctx.github.list_unresolved_review_threads(item.pr)
        except Exception as error:
            logger.warning(
                "pr_review:%s: could not refresh process threads (%s)",
                item.issue,
                type(error).__name__,
            )
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        live_process = _live_process_threads(records, current_live)
        if live_process is None:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        process_ids = set(live_process)
        if process_ids:
            parsed = _parse_validation_result(item.payload.get("validation_result"))
            if parsed is None:
                return PrReviewStage._handle_automation_threads_requiring_human_resolution(
                    item, len(process_ids), ctx
                )
            unaddressed = _thread_ids(parsed.get("unaddressed"))
            wont_fix = _thread_ids(parsed.get("wont_fix"))
            if process_ids & wont_fix or not process_ids.issubset(unaddressed):
                return PrReviewStage._handle_automation_threads_requiring_human_resolution(
                    item, len(process_ids), ctx
                )
        return _without_duplicate_live_process_findings(threads, live_process)

    @staticmethod
    def _record_posted_process_threads(item: WorkItem, post_receipts: list[dict[str, Any]]) -> bool:
        """Append only immutable receipts produced at the post boundary.

        A later full unresolved-thread read is deliberately not a source of
        receipts: a same-login human reply can arrive in that window. The
        adapter returns each process-created thread only after proving its
        sole initial comment and owning review identity.
        """
        if not post_receipts:
            return _process_thread_records(item) is not None
        created_head = item.payload.get("reviewed_pr_head_sha")
        if not isinstance(created_head, str) or not created_head.strip():
            return False
        new_records: dict[str, dict[str, Any]] = {}
        for receipt in post_receipts:
            if not isinstance(receipt, dict):
                return False
            thread_id = _durable_thread_id(receipt)
            if thread_id is None or thread_id in new_records:
                return False
            new_records[thread_id] = {**receipt, "created_head_sha": created_head}
        if any(not _is_process_thread_receipt(receipt) for receipt in new_records.values()):
            return False
        records = _process_thread_records(item)
        if records is None or any(thread_id in records for thread_id in new_records):
            return False
        # Keep the original post receipt intact. A later human reply must not
        # be absorbed into the baseline used to prove this is still our thread.
        item.payload["process_review_threads"] = [*records.values(), *new_records.values()]
        return True

    def _post(self, item: WorkItem, ctx: StageContext) -> StepResult:  # noqa: C901
        """POST [M]: durably post SURVIVING threads, refresh unresolved counts.

        The thread post is the round's durable write (doc step 3). The
        reviewer's threads (parsed by the worker/coordinator (#1817) into
        ``payload["review_threads"]``) are first filtered through the
        validation job's verdict (:func:`_surviving_threads`, m1): wont_fix
        findings are dropped, unaddressed prior findings are re-opened.
        Zero open blocking automation threads skip the address leg straight to
        EVAL. Advisory findings remain in the audit summary but are never
        published as inline threads: GitHub conversation resolution would make
        such a thread a merge blocker that this loop must not resolve.
        """
        if item.pr is None:  # guarded by step(); kept for restart safety
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        audit = item.payload.get("review_audit")
        if not isinstance(audit, ReviewAudit) or not audit.valid:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        raw_threads = [dict(t) for t in item.payload.get("review_threads") or []]
        threads = _surviving_threads(raw_threads, item.payload.get("validation_result"))
        item.payload["raw_review_threads"] = raw_threads

        handled = _handled_process_receipts(item)
        if handled is not None:
            receipts, dispositions = handled
            if dispositions:
                reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
                if not reviewed_head:
                    item.payload["review_audit_failure"] = True
                    return Continue(next_state=EVAL)
                try:
                    resolution = ctx.github.reply_and_resolve_process_review_threads(
                        item.pr,
                        reviewed_head_sha=reviewed_head,
                        receipts=receipts,
                        dispositions=dispositions,
                    )
                except Exception as error:
                    logger.warning(
                        "pr_review:%s: process review-thread reconciliation failed (%s)",
                        item.issue,
                        type(error).__name__,
                    )
                    item.payload["review_audit_failure"] = True
                    return Continue(next_state=EVAL)
                resolved_ids = set(getattr(resolution, "resolved_thread_ids", ()))
                blocked_ids = set(getattr(resolution, "blocked_thread_ids", ()))
                if resolved_ids != set(dispositions) or blocked_ids:
                    return PrReviewStage._handle_automation_threads_requiring_human_resolution(
                        item,
                        len(dispositions),
                        ctx,
                    )
                item.payload["process_review_threads"] = [
                    receipt
                    for receipt in item.payload.get("process_review_threads", [])
                    if _durable_thread_id(receipt) not in resolved_ids
                ]

        reconciled = self._reconcile_process_threads_before_post(item, ctx, threads)
        if not isinstance(reconciled, list):
            return reconciled
        threads = [
            thread
            for thread in reconciled
            if str(thread.get("severity") or "").strip().lower() in BLOCKING_SEVERITIES
        ]
        if any(not _is_postable_finding(thread) for thread in threads):
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        # The surviving audit set is what gets posted. Classification and
        # addressing use the normalized live read-back installed below.
        item.payload["review_threads"] = threads
        try:
            post_receipts = list(
                ctx.github.post_review_threads(
                    item.pr,
                    list(threads),
                    self._final_review_comment(audit),
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
        if not self._record_posted_process_threads(item, post_receipts):
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
        blocking_auto, minor_auto, human_unresolved = _thread_counts(live_threads)
        remediation_threads = _normalize_blocking_remediation_threads(live_threads)
        if len(remediation_threads) != blocking_auto:
            item.payload["review_audit_failure"] = True
            return Continue(next_state=EVAL)
        item.payload["remediation_threads"] = remediation_threads
        item.payload["unresolved_auto"] = blocking_auto + minor_auto
        item.payload["unresolved_human"] = human_unresolved
        if blocking_auto == 0:
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
                    # No-commit retry directive (#1575): non-empty ONLY on
                    # the one retry after a no-commit address turn;
                    # get_address_review_prompt renders it via
                    # build_unaddressed_directive.
                    "unaddressed_findings": list(item.payload.get("unaddressed_findings") or []),
                },
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
        preserved = item.payload.setdefault("preserved_direct_worktrees", [])
        if not isinstance(preserved, list):
            return StageOutcome(Disposition.FINISH_FAIL, "detached_push_recovery_receipt_invalid")
        if item.worktree not in preserved:
            preserved.append(item.worktree)
        item.worktree = ""
        item.payload["existing_pr"] = True
        item.payload.pop("direct_pr_worktree", None)
        item.payload.pop("direct_pr_worktree_dirty", None)
        item.payload["direct_pr_worktree_generation"] = generation + 1
        item.session_ids.pop(AGENT_PR_REVIEWER, None)
        item.session_ids.pop(AGENT_ADDRESS_REVIEW, None)
        _clear_round_review_state(item)
        return None

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
            recovery = self._restart_direct_pr_review(item)
            if recovery is not None:
                return recovery
            logger.warning(
                "pr_review:%d: detached push saw changed remote head; "
                "restarting from a fresh checkout",
                item.issue,
            )
            return Continue(next_state=ENTER)
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
            blocking, advisory, human = _thread_counts(live_threads)
            if not blocking and not advisory and not human:
                return self._compact_before_next_review(item, ctx)
            bind_outcome = self._bind_current_head_for_negative(item, ctx)
            if bind_outcome is not None:
                return bind_outcome
            if human:
                return PrReviewStage._handle_human_blocked(item, human, ctx)
            if blocking or advisory:
                return PrReviewStage._handle_automation_threads_requiring_human_resolution(
                    item,
                    blocking + advisory,
                    ctx,
                )
            payload["review_error_retries"] = 0
            round_done = payload.get("pr_review_round", 0) + 1
            payload["pr_review_round"] = round_done
            item.attempts["pr_review_iter"] = item.attempts.get("pr_review_iter", 0) + 1
            return self._handle_non_go(
                item,
                ctx,
                audit,
                blocking,
                blocking,
                round_done,
                ctx.budget("pr_review_iter"),
                ctx.budget("pr_review_hard"),
            )

        # Fresh counts AFTER the address/push leg, split by severity so a GO is
        # downgraded only by BLOCKING automation threads (#1856 / re-introduced #1554).
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
        blocking_auto, minor_auto, human_unresolved = _thread_counts(live_threads)
        automation_unresolved = blocking_auto + minor_auto  # progress-trail parity (#1554)
        unresolved = automation_unresolved + human_unresolved

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

        if human_unresolved:
            logger.info(
                "pr_review:%d: audit blocked by %d human thread(s); finishing (unlabeled)",
                item.issue,
                human_unresolved,
            )
            return PrReviewStage._handle_human_blocked(item, human_unresolved, ctx)

        if blocking_auto == 0 and minor_auto == 0:
            return self._handle_clean_go(item, ctx)

        if minor_auto and not blocking_auto:
            return self._handle_automation_threads_requiring_human_resolution(
                item,
                minor_auto,
                ctx,
            )

        return self._handle_non_go(
            item,
            ctx,
            audit,
            automation_unresolved,
            unresolved,
            round_done,
            soft_cap,
            hard_cap,
        )

    def _handle_non_go(
        self,
        item: WorkItem,
        ctx: StageContext,
        verdict: Any,
        automation_unresolved: int,
        unresolved: int,
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
        # #1554 parity (m2): the progress trail counts AUTOMATION threads
        # only — a human resolving their own thread is not automation
        # progress and must not earn extension rounds.
        # #1863: prev_unresolved is THIS round's pre-address snapshot
        # (POST's unresolved_auto) so the extension gate compares
        # pre-address vs post-address WITHIN the round being evaluated —
        # progress landing on the soft-cap round is no longer invisible
        # to a stale cross-round comparison.
        prev_unresolved = item.payload.get("unresolved_auto")
        if round_done < soft_cap:
            logger.info(
                "pr_review:%d: %s (round %d/%d, %d unresolved); re-reviewing",
                item.issue,
                "structured audit",
                round_done,
                soft_cap,
                unresolved,
            )
            return self._compact_before_next_review(item, ctx)
        made_progress = prev_unresolved is not None and automation_unresolved < prev_unresolved
        if round_done < hard_cap and made_progress:
            # #1554 progress-aware extension: rounds soft_cap+1..hard_cap are
            # admitted only while the AUTOMATION unresolved count strictly
            # decreases.
            logger.info(
                "pr_review:%d: extension round %d/%d earned (%s -> %d automation unresolved)",
                item.issue,
                round_done + 1,
                hard_cap,
                prev_unresolved,
                automation_unresolved,
            )
            return self._compact_before_next_review(item, ctx)

        logger.warning(
            "pr_review:%d: exhausted at round %d (automation unresolved %s -> %d); applying %s",
            item.issue,
            round_done,
            prev_unresolved,
            automation_unresolved,
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
            f"automation-unresolved thread count stuck "
            f"({prev_unresolved} -> {automation_unresolved}); further re-review "
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
        ownership of a label after that race, so it only posts a neutral human
        handoff and makes no further label mutation.
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
        blocking, advisory, human_unresolved = _thread_counts(live_threads)
        if human_unresolved:
            return PrReviewStage._handle_late_threads_after_go_write(
                item,
                blocking + advisory + human_unresolved,
                ctx,
            )
        if blocking or advisory:
            return PrReviewStage._handle_late_threads_after_go_write(
                item,
                blocking + advisory,
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

        The GO write is non-conditional. A concurrent human or automation actor
        may own the current implementation state by the time the late thread is
        observed, so clearing or replacing a label would be an unsafe mutation.
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
        body = (
            "**Automation stand-down: review activity changed during GO admission.**\n\n"
            f"{unresolved_threads} unresolved review thread(s) were observed after the "
            "implementation state write. Automation cannot prove it still owns the current "
            "labels, so it made no further label changes. A human must verify the current "
            "diff, reply if appropriate, and resolve the review thread(s) before another "
            "review/merge attempt."
        )
        try:
            ctx.github.post_pr_comment(item.pr, body)
        except Exception as error:
            logger.warning(
                "pr_review: failed to post late-thread handoff on PR #%d (non-fatal): %s",
                item.pr,
                error,
            )
        return StageOutcome(Disposition.FINISH_FAIL, "late_threads_require_human_resolution")

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
    def _post_human_blocked_comment(
        pr_number: int, human_unresolved: int, ctx: StageContext
    ) -> None:
        """Post the HUMAN_BLOCKED stand-down comment, non-fatally [durable].

        Written BEFORE the FINISH_FAIL outcome so the reason automation
        stood down is durably visible on the PR (M3): without it, an
        unlabeled PR that automation stops touching looks abandoned.

        Args:
            pr_number: GitHub PR number blocked by human threads.
            human_unresolved: Count of unresolved human-owned review threads.
            ctx: Stage context carrying the GitHub accessor.

        """
        body = (
            "**Automation stand-down: unresolved human review thread(s) prevent a transition.**\n\n"
            f"The implementation review cannot transition while {human_unresolved} "
            "unresolved review thread(s) opened by a human remain on this PR. "
            "Automation will not resolve human threads and cannot act on them, "
            "so it is standing down without changing implementation-state labels. "
            "Automation does not arm auto-merge. Once the human thread(s) are resolved, "
            "the next automation pass will re-review this PR."
        )
        try:
            ctx.github.post_pr_comment(pr_number, body)
        except Exception as e:
            logger.warning(
                "pr_review: failed to post HUMAN_BLOCKED comment on PR #%d (non-fatal): %s",
                pr_number,
                e,
            )

    @staticmethod
    def _handle_human_blocked(
        item: WorkItem, human_unresolved: int, ctx: StageContext
    ) -> StepResult:
        """Stand down without mutating labels whose current owner is unknowable.

        GitHub exposes only unconditional label deletion.  A human or another
        actor can write an implementation-state label after the thread read and
        before this method could delete it, so neither a precondition read nor
        a post-delete readback can prove that deletion is safe.  The terminal
        human-thread outcome itself prevents this work item from advancing;
        later runs lack this process's reviewed-head proof and must re-review.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        pr_number = item.pr
        PrReviewStage._post_human_blocked_comment(pr_number, human_unresolved, ctx)
        return StageOutcome(Disposition.FINISH_FAIL, "human_blocked")

    @staticmethod
    def _handle_automation_threads_requiring_human_resolution(
        item: WorkItem,
        automation_unresolved: int,
        ctx: StageContext,
    ) -> StepResult:
        """Record a fail-closed handoff for open threads that cannot be proven safe.

        The guarded adapter may reply and resolve a canonical receipt from any
        loop invocation after revalidating it immediately before each mutation.
        A remaining changed, human-owned, malformed, or otherwise unprovable
        thread must remain a human gate.
        """
        if item.pr is None:
            return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
        no_go = PrReviewStage._write_no_go(item, ctx)
        if no_go is not None:
            return no_go
        body = (
            "**Automation stand-down: unresolved automation review thread(s) require "
            "human resolution.**\n\n"
            f"{automation_unresolved} automation-created review thread(s) remain open on this "
            "PR without a verifiable immutable receipt. The guarded loop could not prove "
            "that a reply and resolution would leave human activity untouched. "
            "The PR is marked `state:implementation-no-go`; automation does not arm auto-merge. "
            "A human must "
            "verify the fixes and resolve the thread(s); a fresh automation pass can then "
            "re-review the current head."
        )
        try:
            ctx.github.post_pr_comment(item.pr, body)
        except Exception as error:
            logger.warning(
                "pr_review: failed to post automation-thread handoff on PR #%d (non-fatal): %s",
                item.pr,
                error,
            )
        return StageOutcome(Disposition.FINISH_FAIL, "automation_threads_require_human_resolution")

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
        blocking, advisory, human_unresolved = _thread_counts(live_threads)
        if human_unresolved:
            logger.info(
                "pr_review: clean GO recheck found %d late human thread(s) on PR #%d; "
                "not advancing",
                human_unresolved,
                pr_number,
            )
            return PrReviewStage._handle_human_blocked(item, human_unresolved, ctx)
        if blocking or advisory:
            return PrReviewStage._handle_automation_threads_requiring_human_resolution(
                item,
                blocking + advisory,
                ctx,
            )
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

    @staticmethod
    def _final_review_comment(audit: ReviewAudit) -> str:
        """Build an audit-only review comment; labels carry eligibility."""
        return render_review_audit(audit)
