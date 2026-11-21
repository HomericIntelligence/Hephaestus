"""Plan-review stage: review, amend, and approve issue plans (issue #1814).

The single plan-review control flow as a pipeline stage
(docs/architecture.md §5.3 "plan_review" is the
binding contract). It fully owns the review/amend/learn loop that the legacy
planner review loop used to run before ``hephaestus-plan-issues`` was
re-pointed at the pipeline (#1820):

- States: ENTER -> REVIEW_WAIT -> EVAL -> AMEND_WAIT -> (loop) -> ADVANCE.
- Budgets: ``plan_review_iter`` = 3 (max review iterations per cycle),
  ``plan_cycles`` = 2 (max plan->review->amend cycles before giving up).
- Iteration accounting: ``item.attempts["plan_review_iter"]`` is the
  PER-LIFETIME audit trail (routing.py contract: attempts are never reset),
  so EVAL gates on the CYCLE-RELATIVE counter
  ``item.payload["review_round"]``, reset by ``on_enter`` whenever the item
  enters a new plan cycle (keyed on ``attempts["plan_cycles"]``). Cycle 2
  therefore gets its full ``plan_review_iter`` reviews/amends and
  ``get_plan_loop_review_prompt`` always sees a 0-based iteration in
  ``0..plan_review_iter-1``; ``plan_cycles`` bounds the number of cycles.
- Both counters advance in EVAL and ONLY for real verdicts
  (GO/NOGO). ERROR, unsupported, and missing verdicts never burn an iteration
  (the shared ``review_types.ReviewVerdict`` doctrine: "the reviewer never ran" must
  not be mistaken for a judgement, #911/#1554/#1794); they RETRY with
  labels untouched, bounded in-stage by
  ``item.payload["review_error_retries"]`` (cap
  ``REVIEW_ERROR_RETRY_CAP`` consecutive failures, reset on any real
  verdict or on entry into a new plan cycle, #1869). At the cap the item
  FINISH_FAILs — a reviewer-infrastructure
  failure is not fixed by replanning, so failing back to planning would
  only spend ``plan_cycles`` on more doomed reviews; labels stay untouched
  on the whole ERROR path.
- Owned labels: ``state:plan-go`` (GO) and ``state:plan-no-go`` (NOGO)
  [durable]. GO/NOGO are
  computed by the shared pure ``state_labels.apply_plan_verdict`` and applied
  through one fail-closed, mutually-exclusive label transition.
- Verdict parsed IN-WORKER by :func:`parse_plan_review_verdict` from one exact
  final state-label token
  (carried as the review job's ``parse`` callable). REVIEW_WAIT clears any
  stale ``payload["review_verdict"]`` at submission so a failed later round
  can never replay an earlier round's verdict.
- Prompt functions (imported, never re-authored):
  ``prompts/planning.py get_plan_loop_review_prompt``,
  ``prompts/planning.py get_plan_prompt`` (composed with the reviewer
  feedback block by :func:`build_amend_prompt` for amends, then
  upserted as the durable plan comment before the next review).
- Current plan and review comments are actor-owned canonical records. Restart
  repairs a missing pending review from the current plan before another job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from subprocess import SubprocessError
from typing import Final

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.model_selection import AgentModelSelection, parse_model_selection
from hephaestus.agents.pi_session import AgentSessionBinding
from hephaestus.agents.runtime import agent_uses_configured_model_default
from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation.agent_config import (
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    plan_reviewer_claude_timeout,
    planner_claude_timeout,
    planner_model,
    reviewer_model,
)
from hephaestus.automation.comment_identity import (
    CommentAliasConflictError,
)
from hephaestus.automation.current_plan import read_current_plan
from hephaestus.automation.pipeline.job_failures import (
    agent_failure_diagnostic,
    agent_failure_reason,
)
from hephaestus.automation.plan_decomposition import (
    WorkUnit,
    normalize_work_tree,
    topological_layers,
    tree_digest,
    work_tree_errors,
)
from hephaestus.automation.plan_review_session import (
    PlanReviewSessionLostError,
    decomposition_generation_session_key,
    decomposition_review_session_key,
)
from hephaestus.automation.prompts._shared import fence_content
from hephaestus.automation.prompts.planning import (
    get_plan_decomposition_prompt,
    get_plan_decomposition_review_prompt,
    get_plan_loop_review_prompt,
)
from hephaestus.automation.protocol import (
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.review_journal import (
    MAX_CURRENT_REVISION_CONTEXT_CHARS,
    STALE_PLAN_RECHECK_BLOCK_MARKER,
    STALE_PLAN_RECHECK_MARKER,
    CommentJournalReadError,
    IssueComment,
    JournalSnapshot,
    PlanDiscoveryStatus,
    complete_current_plan_context,
    current_plan_context,
    is_pending_review,
    journal_snapshot,
    newest_plan_comment_identity,
    parse_plan_review_state,
    plan_fingerprint,
    render_current_review,
    render_stale_plan_recheck_block,
    render_stale_plan_recheck_record,
)
from hephaestus.automation.review_types import ReviewVerdict
from hephaestus.automation.state_labels import (
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    apply_plan_verdict,
    enter_planning_transition,
    is_exclusive_plan_state,
)
from hephaestus.prompts import PromptCatalog

from ...coordinator_sessions import agent_session_lifecycle
from ...diagnostics import bounded_pipeline_diagnostic, redact_diagnostic_text
from ...github_jobs import (
    DecompositionChildLabelsApplied,
    DecompositionChildLabelsRequest,
    DecompositionChildrenCreated,
    DecompositionChildrenRequest,
    DecompositionParentTransitioned,
    DecompositionParentTransitionRequest,
    FrozenJson,
)
from ...plan_journal import (
    DecompositionBindings,
    DecompositionIntent,
    DecompositionReviewReceipt,
    DecompositionSeal,
    DecompositionTransitionReceipt,
    PlanNogoRecord,
    plan_nogo_count,
    plan_nogo_digest,
    plan_nogo_records,
    publish_decomposition_bindings,
    publish_decomposition_draft,
    publish_decomposition_intent,
    publish_decomposition_review_receipt,
    publish_decomposition_seal,
    publish_decomposition_transition,
    publish_plan_nogo,
    publish_plan_revision,
    reconcile_plan_journal,
    recover_decomposition_bindings,
    recover_decomposition_draft,
    recover_decomposition_intent,
    recover_decomposition_seal,
    recover_decomposition_transition,
    render_decomposition_summary_body,
    requirements_epoch,
)
from ...stage_base import (
    AgentJob,
    Continue,
    Disposition,
    GitHubJob,
    JobRequest,
    JobResult,
    Stage,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _require_issue_labels,
    _worktree_path,
    agent_provider,
    planning_source_workspace_binding,
    records_retry_exceeded_cap,
    stage_model,
    stage_network_deadline,
    stage_timeout,
)
from ..planning import (
    _finalized_plan_authority_is_current,
    _finalized_plan_state_is_current,
    build_plan_prompt,
)


def _review_job_cwd(
    item: WorkItem,
    ctx: StageContext,
    workspace: WorkspaceBinding | None,
) -> Path:
    """Return the prepared lane, or the configured checkout for a preview."""
    return workspace.cwd if workspace is not None else _worktree_path(item, ctx)


_MODEL_SELECTION_FORMAT = 1


def _durable_reviewer_selection(provider: str, model: str) -> tuple[str, dict[str, object]]:
    """Split a direct-provider selection into JSON-safe journal fields."""
    if agent_uses_configured_model_default(provider):
        selection = parse_model_selection(model)
        return selection.model, {
            "model_selection_format": _MODEL_SELECTION_FORMAT,
            "reasoning_effort": selection.reasoning_effort,
        }
    return model, {}


def _restore_reviewer_selection(
    provider: str,
    model: str,
    reviewer_config: dict[str, object],
) -> str:
    """Restore structured direct-provider model metadata from the journal."""
    if agent_uses_configured_model_default(provider):
        if reviewer_config.get("model_selection_format") != _MODEL_SELECTION_FORMAT:
            raise PlanReviewSessionLostError("reviewer model selection format is invalid")
        effort = reviewer_config.get("reasoning_effort", "")
        if not isinstance(effort, str):
            raise PlanReviewSessionLostError("reviewer reasoning effort is invalid")
        return AgentModelSelection(model, effort)
    return model


logger = logging.getLogger(__name__)

# In-memory mini-states (stage-local strings, never GitHub labels).
ENTER = "ENTER"
REVIEW_WAIT = "REVIEW_WAIT"
EVAL = "EVAL"
AMEND_WAIT = "AMEND_WAIT"
FEEDBACK_WAIT = "FEEDBACK_WAIT"

#: Non-blocking severity findings inside one GO review (comet#5394 shape:
#: "Verdict: GO (with the two medium changes above)"). The review rubric
#: forces state:plan-no-go for critical/major findings, so severity-tagged
#: findings under a GO are exactly the accepted-with-feedback signal that
#: drives the one bounded final planner integration pass (#3643).
_GO_FEEDBACK_RE = re.compile(r"(?im)^(?:[-*]\s*)?(?:critical|major|medium|low|minor):\s")

#: Payload key that records the accepted review's findings for the
#: implementation prompt (non-blocking block) and the integration pass.
_PLAN_REVIEW_FINDINGS = "plan_review_findings"

#: Payload guard: the final planner integration pass runs at most once per
#: item lifetime. Set before the pass is requested so a restart cannot
#: mint a second pass.
_PLAN_FEEDBACK_INTEGRATED = "plan_feedback_integrated"

#: Max CONSECUTIVE reviewer-infrastructure failures (ERROR verdicts or
#: failed/valueless review jobs) tolerated before the item FINISH_FAILs.
#: Bounds the in-stage ERROR retry loop without burning ``plan_review_iter``
#: or stamping labels (#911). Reset whenever a real verdict arrives.
REVIEW_ERROR_RETRY_CAP = 2

#: The convergence trigger: decomposition starts after this many confirmed
#: real NOGO records in one requirements epoch (issue #2982).
NOGO_DECOMPOSITION_THRESHOLD: Final[int] = 5

# --- Decomposition state machine (issue #2982) -----------------------------
# Stage-local mini-states, never GitHub labels. The decomposition flow runs
# inside the plan-review stage: generation -> independent review -> sealed
# tree -> host-owned GitHub mutations -> coordinator admission.
DECOMP_GEN_WAIT = "DECOMP_GEN_WAIT"
DECOMP_GEN_EVAL = "DECOMP_GEN_EVAL"
DECOMP_REVIEW_WAIT = "DECOMP_REVIEW_WAIT"
DECOMP_REVIEW_EVAL = "DECOMP_REVIEW_EVAL"
DECOMP_CHILDREN_WAIT = "DECOMP_CHILDREN_WAIT"
DECOMP_CHILDREN_EVAL = "DECOMP_CHILDREN_EVAL"
DECOMP_PARENT_WAIT = "DECOMP_PARENT_WAIT"
DECOMP_PARENT_EVAL = "DECOMP_PARENT_EVAL"
DECOMP_LABELS_WAIT = "DECOMP_LABELS_WAIT"
DECOMP_LABELS_EVAL = "DECOMP_LABELS_EVAL"

#: Bounded model rounds (generation or independent review) before the stage
#: gives up and applies the operator-owned blocked path (#2982).
DECOMPOSITION_MODEL_ROUNDS: Final[int] = 3

#: Bounded retries for a failed decomposition GitHub job before fail closed.
DECOMPOSITION_GITHUB_RETRIES: Final[int] = 2

#: The generated-child same-run deferral reason (issue #2982).
GENERATED_CHILD_DEFERRAL: Final[str] = "generated-child decomposition deferred to a later run"

_DECOMP_TRIGGER_KEY = "decomposition_trigger"
_DECOMP_INTENT_KEY = "decomposition_intent"
_DECOMP_OUTPUT_KEY = "decomposition_output"
_DECOMP_DRAFT_KEY = "decomposition_draft"
_DECOMP_DRAFT_DIGEST_KEY = "decomposition_draft_digest"
_DECOMP_REJECTED_KEY = "decomposition_rejected_draft"
_DECOMP_ERRORS_KEY = "decomposition_validation_errors"
_DECOMP_GEN_ROUNDS_KEY = "decomposition_generation_rounds"
_DECOMP_REVIEW_OUTPUT_KEY = "decomposition_review_output"
_DECOMP_REVIEW_ROUNDS_KEY = "decomposition_review_rounds"
_DECOMP_FINDINGS_KEY = "decomposition_review_findings"
_DECOMP_CHILDREN_JOB_KEY = "decomposition_children_created"
_DECOMP_CHILDREN_RETRIES_KEY = "decomposition_children_retries"
_DECOMP_PARENT_JOB_KEY = "decomposition_parent_transition"
_DECOMP_PARENT_RETRIES_KEY = "decomposition_parent_retries"
_DECOMP_LABELS_JOB_KEY = "decomposition_child_labels"
_DECOMP_LABELS_RETRIES_KEY = "decomposition_labels_retries"
_DECOMP_CHILDREN_KEY = "decomposition_children"
_DECOMP_EPOCH_KEY = "decomposition_requirements_epoch"

_DECOMPOSITION_REVIEW_GO_PREFIX = "decomposition-review: GO"

_REVIEWED_PLAN_COMMENT_BODY = "reviewed_plan_comment_body"


def _fleet_finalized_plan_outcome(item: WorkItem, ctx: StageContext) -> StageOutcome:
    """Check the admitted sealed plan through its existing authenticated reader."""
    if item.issue is None:
        return StageOutcome(Disposition.BLOCKED, "fleet_finalized_plan_changed")
    current = read_current_plan(item.issue, ctx.github)
    body = current.plan.plan_text
    if (
        not current.finalized_body
        or current.plan.status is not PlanDiscoveryStatus.FOUND
        or not isinstance(body, str)
        or body != item.payload.get("issue_body")
        or hashlib.sha256(body.encode()).hexdigest() != item.payload.get("_fleet_plan_sha256")
    ):
        return StageOutcome(Disposition.BLOCKED, "fleet_finalized_plan_changed")
    return StageOutcome(Disposition.ADVANCE, "Admitted finalized plan remains approved")


@dataclass(frozen=True)
class _AcceptedPlanReview:
    """Keep one review identity and its publication progress."""

    verdict: ReviewVerdict
    revision: int
    plan_comment_body: str
    charged_round: int | None = None
    comment_published: bool = False
    label_proposed: bool = False


def _reviewer_failure(item: WorkItem, reason: str) -> StageOutcome:
    """Bound consecutive reviewer failures without charging a plan revision."""
    retries = int(item.payload.get("review_error_retries", 0)) + 1
    item.payload["review_error_retries"] = retries
    cause = item.payload.get("review_provider_error")
    detail = f"{reason}: {cause}" if cause and cause != reason else reason
    if retries > REVIEW_ERROR_RETRY_CAP:
        logger.error("plan_review:%s: reviewer failures exhausted: %s", item.issue, detail)
        return StageOutcome(
            Disposition.FINISH_FAIL,
            f"reviewer error retries exhausted ({detail})",
        )
    logger.warning(
        "plan_review:%s: %s; retry %d/%d",
        item.issue,
        detail,
        retries,
        REVIEW_ERROR_RETRY_CAP,
    )
    item.state = REVIEW_WAIT
    return StageOutcome(Disposition.RETRY, detail)


def _selected_reviewer_model(item: WorkItem, ctx: StageContext) -> str:
    """Return the active override or the configured reviewer model."""
    if "review_model_override" in item.payload:
        return str(item.payload["review_model_override"])
    return stage_model(ctx, "reviewer", reviewer_model)


def _provider_failure_outcome(item: WorkItem, ctx: StageContext) -> StageOutcome:
    """Try each remaining reviewer model, then hold without a verdict label."""
    cause = str(item.payload.get("review_provider_error") or "unknown provider error")
    if "review_fallback_models" in item.payload:
        remaining = [str(model) for model in item.payload["review_fallback_models"]]
    else:
        primary = _selected_reviewer_model(item, ctx)
        seen = {parse_model_selection(primary)}
        remaining = []
        for candidate in (ctx.config.fallback_model, ""):
            selection = parse_model_selection(candidate)
            if selection not in seen:
                seen.add(selection)
                remaining.append(candidate)
    if not remaining:
        logger.error(
            "plan_review:%s: reviewer provider unavailable after the fallback chain; "
            "holding for operator recovery: %s",
            item.issue,
            cause,
        )
        return StageOutcome(
            Disposition.BLOCKED,
            f"review provider unavailable after configured fallback and provider default: {cause}",
        )

    selected = remaining.pop(0)
    item.payload["review_model_override"] = selected
    item.payload["review_fallback_models"] = remaining
    if item.issue is not None:
        ctx.plan_review_session_resets.add(item.issue)
    item.state = REVIEW_WAIT
    logger.warning(
        "plan_review:%s: reviewer provider failed: %s; retrying with %s",
        item.issue,
        cause,
        selected or "the provider default model",
    )
    return StageOutcome(
        Disposition.RETRY,
        f"review provider unavailable; retrying with {selected or 'provider default'}: {cause}",
    )


def _missing_plan_admission_outcome(
    item: WorkItem,
    labels: Sequence[str],
) -> StageOutcome:
    """Route a missing canonical plan without changing stale GO authority."""
    assert item.issue is not None  # noqa: S101 - admission validates the issue
    if is_exclusive_plan_state(labels, STATE_PLAN_GO):
        logger.warning(
            "plan_review:%d: approved canonical plan missing at admission; "
            "forcing a new planning epoch",
            item.issue,
        )
        return StageOutcome(Disposition.FAIL_BACK, "plan_changed")
    item.payload.pop("plan_text", None)
    item.payload.pop("plan_revision", None)
    item.payload.pop(_REVIEWED_PLAN_COMMENT_BODY, None)
    item.payload.pop("prior_review", None)
    logger.warning(
        "plan_review:%d: canonical plan missing at admission; replanning",
        item.issue,
    )
    return StageOutcome(Disposition.FAIL_BACK, "plan_missing")


def _finalized_plan_admission_outcome(
    item: WorkItem,
    ctx: StageContext,
    labels: Sequence[str],
) -> StageOutcome | None:
    """Admit a durable Athena finalized plan whose plan comments are gone (#3665).

    Planning seals one accepted GO planning epoch into the issue body, and
    that finalize record is durable planning authority even after the
    intermediate plan/review comments are deleted. When this stage re-enters
    (for example after a loop restart re-seeds the item) and the comment
    journal no longer carries the plan, the finalized authority must advance
    instead of forcing a new planning epoch. Otherwise planning fast-forwards
    on the same seal, this stage fails back again, and the two stages
    ping-pong while every restart re-seed resets the fail-back counter.

    Fail-closed: a missing finalize marker, a non-current label state (an
    operator ``state:skip`` always wins), or any live body or label drift
    falls through to the ordinary missing-plan admission.
    """
    if not item.payload.get("athena_finalized_plan"):
        return None
    if not _finalized_plan_state_is_current(labels):
        return None
    assert item.issue is not None  # noqa: S101 - on_enter validates the issue
    try:
        authority_current = _finalized_plan_authority_is_current(
            item,
            ctx.github.gh_issue_json(item.issue),
            require_current_labels=not ctx.dry_run,
        )
    except RuntimeError as exc:
        logger.warning(
            "plan_review:%d: finalized authority rebind failed: %s",
            item.issue,
            exc,
        )
        return None
    if not authority_current:
        logger.warning(
            "plan_review:%d: finalized plan authority drifted; replanning",
            item.issue,
        )
        return None
    item.payload.pop("plan_text", None)
    item.payload.pop("plan_revision", None)
    item.payload.pop(_REVIEWED_PLAN_COMMENT_BODY, None)
    item.payload.pop("prior_review", None)
    logger.info(
        "plan_review:%d: Athena finalized plan remains approved; advancing",
        item.issue,
    )
    return StageOutcome(Disposition.ADVANCE, "Athena finalized plan remains approved")


_PLAN_REVIEW_LABELS = {
    STATE_PLAN_GO: "GO",
    STATE_PLAN_NO_GO: "NOGO",
}


def _plan_is_oversized(comments: Sequence[IssueComment]) -> bool:
    """Return whether the complete current plan cannot fit the amendment budget.

    The lossless-size trigger renders the complete normalized plan without the
    amendment truncation and compares it with
    ``MAX_CURRENT_REVISION_CONTEXT_CHARS`` (issue #2982).
    """
    return len(complete_current_plan_context(comments)) > MAX_CURRENT_REVISION_CONTEXT_CHARS


def _issue_body(item: WorkItem) -> str:
    """Return the authoritative issue body carried by the work item."""
    body = item.payload.get("issue_body")
    return body if isinstance(body, str) else ""


def _nogo_findings(item: WorkItem) -> tuple[str, ...]:
    """Return the bounded unresolved findings carried by the prior review."""
    review = item.payload.get("prior_review")
    if not isinstance(review, str) or not review.strip():
        return ()
    return (review.strip()[:500],)


def recorded_at() -> str:
    """Return the durable record timestamp for one publication."""
    return datetime.now(UTC).isoformat()


def decomposition_required(
    item: WorkItem,
    comments: Sequence[IssueComment],
    *,
    epoch: str | None = None,
) -> str | None:
    """Return the decomposition trigger name, or None when the plan still amends.

    Triggers (issue #2982):

    - ``lossless-size``: the complete plan cannot fit the amendment context
      budget and at least one real NOGO is confirmed for the epoch (a first
      review can still approve the large plan).
    - ``convergence``: five confirmed real NOGO records exist for the epoch.

    Infrastructure failures never persist a record, so they never count.
    """
    epoch = epoch or requirements_epoch(_issue_body(item))
    count = plan_nogo_count(comments, epoch)
    if count >= NOGO_DECOMPOSITION_THRESHOLD:
        return "convergence"
    if count >= 1 and _plan_is_oversized(comments):
        return "lossless-size"
    return None


def parent_requirement_texts(issue_body: str) -> tuple[str, ...]:
    """Return the deterministic parent requirement texts for one issue body.

    Every non-empty bullet line of the authoritative issue body is one parent
    requirement. The intent manifest freezes their stable identifiers at the
    first trigger, so later body edits cannot silently change one open tree.
    """
    texts: list[str] = []
    seen: set[str] = set()
    for line in issue_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*")):
            continue
        text = stripped.lstrip("-*").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
    return tuple(texts)


def _finding_texts(comments: Sequence[IssueComment], epoch: str) -> tuple[str, ...]:
    """Return the deduplicated findings of one epoch's confirmed NOGO records."""
    texts: list[str] = []
    seen: set[str] = set()
    for record in plan_nogo_records(comments):
        if record.requirements_epoch != epoch:
            continue
        for finding in record.findings:
            if finding not in seen:
                seen.add(finding)
                texts.append(finding)
    return tuple(texts)


def parse_decomposition_tree_output(text: str) -> tuple[WorkUnit, ...] | None:
    """Parse one model-supplied generation output into a normalized tree.

    The output must carry exactly one fenced ```` ```json ```` block holding a
    JSON array (or ``{"nodes": [...]}``). Anything else is untrusted invalid
    data and returns ``None``.
    """
    marker = "```json"
    start = text.find(marker)
    if start == -1:
        return None
    payload_start = text.index("\n", start) + 1
    payload_end = text.find("```", payload_start)
    if payload_end == -1:
        return None
    try:
        value = json.loads(text[payload_start:payload_end].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(value, dict):
        value = value.get("nodes")
    return normalize_work_tree(value)


def parse_decomposition_review_receipt(text: str, draft_digest: str) -> str | None:
    """Return the accepted receipt verdict, or None for an invalid receipt.

    Only ``decomposition-review: GO <draft_digest>`` as the final receipt line
    accepts the exact draft digest. A NOGO line or any other text returns
    ``None`` (an infrastructure-style invalid result that burns no record).
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("decomposition-review: NOGO"):
            return None
        if line.startswith(_DECOMPOSITION_REVIEW_GO_PREFIX):
            digest = line[len(_DECOMPOSITION_REVIEW_GO_PREFIX) :].strip()
            return "GO" if digest == draft_digest else None
    return None


def build_decomposition_intent(
    item: WorkItem,
    comments: Sequence[IssueComment],
    epoch: str,
    trigger: str,
) -> DecompositionIntent:
    """Build the immutable intent for one parent, epoch, and first trigger."""
    from hephaestus.automation.plan_decomposition import (
        build_finding_manifest,
        build_requirement_manifest,
    )

    requirements = build_requirement_manifest(parent_requirement_texts(_issue_body(item)))
    findings = build_finding_manifest(_finding_texts(comments, epoch))
    return DecompositionIntent(
        repository=item.repo,
        parent=item.issue or 0,
        requirements_epoch=epoch,
        trigger=trigger,
        source_plan_revision=int(item.payload.get("plan_revision") or 1),
        source_plan_fingerprint=plan_fingerprint(str(item.payload.get("plan_text") or "")),
        requirement_manifest=tuple(ref.as_dict() for ref in requirements),
        finding_manifest=tuple(ref.as_dict() for ref in findings),
        created_at=recorded_at(),
    )


def _tree_json(units: Sequence[WorkUnit]) -> str:
    """Return the canonical JSON of one normalized tree."""
    return json.dumps(
        sorted((unit.as_dict() for unit in units), key=lambda unit: str(unit["node_id"])),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decomposition_retry(
    item: WorkItem,
    key: str,
    *,
    wait_state: str,
    cap: int,
    reason: str,
) -> StepResult:
    """Bounded retry for one failed decomposition job, fail closed at the cap."""
    if records_retry_exceeded_cap(item.payload, key, cap):
        logger.error("plan_review:%d: %s retries exhausted", item.issue, reason)
        return StageOutcome(Disposition.BLOCKED, f"plan_decomposition_{reason}_unavailable")
    item.payload["retry_delay_s"] = 2.0
    return Continue(next_state=wait_state)


def _safe_agent_failure_diagnostic(stderr_tail: str) -> str | None:
    """Classify agent stderr for logs without exposing subprocess output."""
    return agent_failure_diagnostic(stderr_tail)


def _safe_agent_failure_reason(error: str | None) -> str:
    """Render an agent failure reason for logs without exposing error text."""
    return agent_failure_reason(error)


def parse_plan_review_verdict(text: str) -> ReviewVerdict:
    """Parse the one exact state label that terminates a plan review."""
    state = parse_plan_review_state(text)
    if state is None:
        return ReviewVerdict(grade=None, verdict="ERROR", raw=text)
    return ReviewVerdict(grade=None, verdict=_PLAN_REVIEW_LABELS[state], raw=text)


def _normalize_review_comment(review: str, *, revision: int | None = None) -> str:
    """Normalize reviewer output for the canonical issue comment."""
    return render_current_review(review, revision=revision or 1)


def _current_revision(comments: Sequence[IssueComment]) -> int:
    """Recover the current plan revision from durable issue comments."""
    return journal_snapshot(comments).revision


def _plan_history(comments: Sequence[IssueComment]) -> str:
    """Return a bounded prior-plan excerpt for a resumed planner amendment."""
    return current_plan_context(comments)


def _confirm_pending_amendment_transition(
    item: WorkItem,
    ctx: StageContext,
) -> StageOutcome | None:
    """Confirm a revised plan's exclusive NEEDS_PLAN state before review."""
    if not item.payload.get("needs_plan_transition_pending"):
        return None
    assert item.issue is not None  # noqa: S101 - stage validates the issue
    # A resumed transition can write a label without re-running the review.
    # Validate both shared roles before it changes that durable state.
    journal_snapshot(ctx.github.issue_comments(item.issue))
    labels = _require_issue_labels(item, ctx)
    if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
        add, remove = enter_planning_transition()
        ctx.github.edit_labels(item.issue, add=add, remove=remove)
        labels = _require_issue_labels(item, ctx)
        if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
            return StageOutcome(
                Disposition.RETRY,
                "exclusive needs-plan label was not confirmed",
            )
    item.payload.pop("needs_plan_transition_pending", None)
    return None


def _restore_review_conversation(
    item: WorkItem,
    ctx: StageContext,
    *,
    plan_text: str,
    revision: int,
    has_prior_review: bool,
) -> StageOutcome | None:
    """Recover or create the cycle-scoped reviewer conversation."""
    store = ctx.plan_review_sessions
    if store is None or item.issue is None or not plan_text:
        return None
    reset = item.issue in ctx.plan_review_session_resets
    if reset:
        ctx.plan_review_session_resets.discard(item.issue)
    try:
        try:
            active = None if reset else store.recover_active(repo=item.repo, issue=item.issue)
        except PlanReviewSessionLostError as exc:
            # The GitHub journal is the canonical plan/review record.  A local
            # provider conversation can be replaced after that record was
            # freshly reconciled; it never authorizes a prior verdict.
            logger.warning(
                "plan_review:%d: replacing unusable local review session: %s",
                item.issue,
                exc,
            )
            active = None
            reset = True
        cycle_number = item.attempts.get("plan_cycles", 0)
        if active is None and has_prior_review and not reset:
            logger.info(
                "plan_review:%d: starting a fresh review cycle after local session loss",
                item.issue,
            )
            reset = True
        if active is not None and not reset:
            selected_provider = agent_provider(ctx, "reviewer")
            selected_model = _selected_reviewer_model(item, ctx)
            previous_model = _restore_reviewer_selection(
                active.provider, active.reviewer_model, active.reviewer_config
            )
            if active.provider != selected_provider or parse_model_selection(
                previous_model
            ) != parse_model_selection(selected_model):
                raise PlanReviewSessionLostError(
                    "reviewer tool or model changed; reset the review session"
                )
        if active is not None and active.canonical_cwd != str(Path(ctx.paths.worktree).resolve()):
            raise PlanReviewSessionLostError("reviewer checkout identity changed")
        if active is not None and active.plan_fingerprint != plan_fingerprint(plan_text):
            active = store.append_artifact(
                active.cycle_id,
                kind="amendment",
                content=plan_text,
                round_index=active.round_index,
                plan_revision=revision,
                plan_fingerprint=plan_fingerprint(plan_text),
            )
        if active is None or reset:
            provider = agent_provider(ctx, "reviewer")
            selected_model = _selected_reviewer_model(item, ctx)
            stored_model, selection_config = _durable_reviewer_selection(provider, selected_model)
            active = store.start_cycle(
                repo=item.repo,
                issue=item.issue,
                provider=provider,
                model=stored_model,
                reviewer_config=selection_config,
                cwd=ctx.paths.worktree,
                plan_revision=revision,
                plan_fingerprint=plan_fingerprint(plan_text),
                reset=reset,
            )
        item.payload.update(
            plan_review_cycle_id=active.cycle_id,
            plan_review_session_id=active.session_id,
            plan_review_cycle_number=cycle_number,
        )
    except PlanReviewSessionLostError as exc:
        logger.error("plan_review:%d: review-session-lost: %s", item.issue, exc)
        item.payload["review_session_error"] = "review-session-lost"
        return _reviewer_failure(item, "review-session-lost")
    return None


def _retry_after_provider_unavailable(item: WorkItem) -> None:
    """Clear a recorded provider failure so the cycle is retried in place.

    A provider outage before any reviewer session was bound leaves nothing to
    recover, so the same cycle is retried instead of starting a new
    conversation that would fail the same way (#3578).
    """
    if item.payload.pop("review_session_error", None) != "review-provider-unavailable":
        return
    logger.warning(
        "plan_review:%s: retrying the bound review cycle after a provider failure: %s",
        item.issue,
        item.payload.pop("review_provider_error", "unknown provider error"),
    )


def _restart_review_conversation(item: WorkItem, ctx: StageContext) -> StageOutcome | None:
    """Replace a lost session within the current review failure budget."""
    if item.issue is None:
        return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
    failure = _reviewer_failure(item, "review-session-lost")
    if failure.disposition is Disposition.FINISH_FAIL:
        return failure
    snapshot = journal_snapshot(reconcile_plan_journal(item.issue, ctx.github))
    if not snapshot.current_plan:
        return StageOutcome(Disposition.FAIL_BACK, "plan_missing")
    item.payload["plan_text"] = snapshot.current_plan
    item.payload["plan_revision"] = snapshot.revision
    item.payload[_REVIEWED_PLAN_COMMENT_BODY] = snapshot.current_plan_body
    item.payload.pop("prior_review", None)
    if snapshot.current_review_revision == snapshot.revision:
        item.payload["prior_review"] = snapshot.current_review
    ctx.plan_review_session_resets.add(item.issue)
    outcome = _restore_review_conversation(
        item,
        ctx,
        plan_text=snapshot.current_plan,
        revision=snapshot.revision,
        has_prior_review=True,
    )
    if outcome is not None:
        return outcome
    item.payload.pop("review_session_error", None)
    item.payload.pop("review_verdict", None)
    item.payload.pop("accepted_plan_review", None)
    return None


def _complete_review_cycle(item: WorkItem, ctx: StageContext) -> None:
    """Close the active durable cycle when the logical review conversation ends."""
    cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
    if ctx.plan_review_sessions is not None and cycle_id:
        ctx.plan_review_sessions.complete(cycle_id)


def _checkpoint_review_identity(item: WorkItem, result: JobResult, ctx: StageContext) -> bool:
    """Persist provider identity and return whether recovery is now required."""
    cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
    store = ctx.plan_review_sessions
    if store is not None and cycle_id and result.session_id and item.state == "REVIEW_WAIT":
        try:
            bound = store.bind_session(cycle_id, result.session_id, result.session_binding)
            item.payload["plan_review_session_id"] = bound.session_id
        except PlanReviewSessionLostError:
            store.mark_recovery_required(cycle_id)
            item.payload["review_session_error"] = "review-session-lost"
            return True
    if result.session_lost:
        if item.state == AMEND_WAIT:
            # The lost conversation is the planner amendment, not the reviewer.
            # The reviewer session stays bound to this cycle so the stage can
            # recover the amendment from durable plan and review artifacts
            # instead of re-reviewing the unchanged plan on a new round.
            item.payload["planner_amendment_session_error"] = "planner-amendment-session-lost"
            logger.warning(
                "plan_review:%s: planner amendment session lost; the reviewer session "
                "remains bound to this cycle",
                item.issue,
            )
            return True
        if store is not None and cycle_id:
            store.mark_recovery_required(cycle_id)
        item.payload["review_session_error"] = "review-session-lost"
        return True
    if (
        not result.ok
        and store is not None
        and cycle_id
        and item.state == "REVIEW_WAIT"
        and store.load(cycle_id).session_id is None
    ):
        # No provider session was ever bound, so there is no conversation to
        # lose. The job failed for another reason, such as a provider outage.
        # Keep the cycle resumable and report the cause the worker reported;
        # marking it recovery-required would destroy a healthy cycle for a
        # transient fault (#3578).
        reason = redact_diagnostic_text((result.error or "reviewer-failed").strip())
        reason = reason or "reviewer-failed"
        logger.warning(
            "plan_review:%s: reviewer unavailable before a session was bound: %s",
            item.issue,
            reason,
        )
        item.payload["review_session_error"] = "review-provider-unavailable"
        item.payload["review_provider_error"] = reason[:500]
        # EVAL owns the single recovery decision for this failed job. Charging
        # here as well consumed two retry units and hid the provider cause.
        return False
    return False


def _review_session_checkpoint(
    ctx: StageContext,
    cycle_id: str,
) -> Callable[[str, AgentSessionBinding | None], None]:
    """Build the worker-side durable identity checkpoint for one cycle."""

    def checkpoint(session_id: str, binding: AgentSessionBinding | None) -> None:
        store = ctx.plan_review_sessions
        if store is None:
            return
        try:
            store.bind_session(cycle_id, session_id, binding)
        except PlanReviewSessionLostError as exc:
            store.mark_recovery_required(cycle_id)
            raise AgentSessionLostError("reviewer identity checkpoint failed") from exc

    return checkpoint


def _record_review_value(item: WorkItem, value: object, ctx: StageContext) -> None:
    """Record a parsed review and its immutable transcript artifact."""
    item.payload["review_verdict"] = value
    raw_review = getattr(value, "raw", "")
    if getattr(value, "verdict", None) == "ERROR" and isinstance(raw_review, str):
        item.payload["unclassified_review_text"] = raw_review.strip()
    cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
    if ctx.plan_review_sessions is not None and cycle_id:
        ctx.plan_review_sessions.append_artifact(
            cycle_id,
            kind="review",
            content=str(getattr(value, "raw", "")),
            round_index=int(item.payload.get("review_round") or 0),
            plan_revision=int(item.payload.get("plan_revision") or 1),
            plan_fingerprint=plan_fingerprint(str(item.payload.get("plan_text") or "")),
        )
    if getattr(value, "verdict", None) == "NOGO":
        raw_review = getattr(value, "raw", None)
        assert isinstance(raw_review, str), (  # noqa: S101 - explicit worker contract
            "plan_review REVIEW_WAIT NOGO verdict must expose raw review text"
        )
        item.payload["prior_review"] = raw_review


def _record_amendment_value(item: WorkItem, plan_text: str, ctx: StageContext) -> None:
    """Publish an amended plan and append its durable handoff artifact."""
    assert item.issue is not None  # noqa: S101 - caller validates issue
    publication = publish_plan_revision(item.issue, plan_text, ctx.github, require_change=True)
    item.payload["plan_text"] = publication.plan
    item.payload["plan_revision"] = publication.revision
    if publication.canonical_body:
        item.payload[_REVIEWED_PLAN_COMMENT_BODY] = publication.canonical_body
    cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
    if ctx.plan_review_sessions is not None and cycle_id:
        ctx.plan_review_sessions.append_artifact(
            cycle_id,
            kind="amendment",
            content=publication.plan,
            round_index=int(item.payload.get("review_round") or 0),
            plan_revision=publication.revision,
            plan_fingerprint=plan_fingerprint(publication.plan),
        )
    if publication.is_stuck:
        logger.warning(
            "plan_review:%s: planning made no progress: %s",
            item.issue,
            publication.no_progress_reason,
        )
        item.payload["no_progress_reason"] = publication.no_progress_reason
        return
    add, remove = enter_planning_transition()
    ctx.github.edit_labels(item.issue, add=add, remove=remove)
    item.payload["needs_plan_transition_pending"] = True
    item.payload.pop("no_progress_reason", None)


def _record_feedback_value(item: WorkItem, plan_text: str, ctx: StageContext) -> None:
    """Keep the planner's final integration output for the implementer.

    The final planner pass is advisory and non-blocking (#3643). The plan
    journal supersedes a revision only under an authoritative exclusive
    ``state:plan-no-go`` label, so the GO-approved plan artifact stays
    exactly as reviewed: the integration output travels in the payload to
    the implementation prompt beside the review findings. A planner that
    kept the plan (fingerprint unchanged) adds nothing and is skipped.

    Args:
        item: The work item carrying the accepted plan identity.
        plan_text: The planner's candidate plan text.
        ctx: The stage context.

    """
    assert item.issue is not None  # noqa: S101 - the stage validates the issue
    current = str(item.payload.get("plan_text") or "")
    if plan_fingerprint(plan_text) == plan_fingerprint(current):
        logger.info(
            "plan_review:%d: planner kept the plan (no feedback integration needed)",
            item.issue,
        )
        return
    item.payload["plan_feedback_integration"] = plan_text
    logger.info(
        "plan_review:%d: captured the final feedback integration for the implementer",
        item.issue,
    )
    cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
    if ctx.plan_review_sessions is not None and cycle_id:
        ctx.plan_review_sessions.append_artifact(
            cycle_id,
            kind="feedback-integration",
            content=plan_text,
            round_index=int(item.payload.get("review_round") or 0),
            plan_revision=int(item.payload.get("plan_revision") or 1),
            plan_fingerprint=plan_fingerprint(plan_text),
        )


def build_amend_prompt(
    issue_number: int,
    prior_review: str,
    issue_title: str = "",
    issue_body: str = "",
    advise_findings: str = "",
    plan_history: str = "",
) -> str:
    """Compose the amend prompt: task-aware plan prompt + reviewer feedback block.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``. The
    feedback block appends the prior reviewer critique to the same
    task-aware plan prompt used by initial planning (doc section 3 step 3:
    "resume planner session with feedback block").

    Args:
        issue_number: GitHub issue number being re-planned.
        prior_review: The previous review round's NOGO critique text.
        issue_title: Source issue title.
        issue_body: Source issue body.
        advise_findings: Advise-step findings; empty string means no block.

    Returns:
        The full amend prompt with the feedback block appended.

    """
    prompt = build_plan_prompt(issue_number, issue_title, issue_body, advise_findings)
    fenced = fence_content()
    feedback = PromptCatalog.current().render(
        "planning/amend_feedback.j2",
        untrusted_notice=fenced.untrusted_notice,
        prior_review_block=fenced.fence("PRIOR_REVIEW", prior_review),
        plan_history_block=(
            fenced.fence("PLAN_HISTORY", plan_history) if plan_history else "_(first revision)_"
        ),
    )
    return prompt + feedback


def build_feedback_integrate_prompt(
    issue_number: int,
    prior_review: str,
    issue_title: str = "",
    issue_body: str = "",
    advise_findings: str = "",
    plan_history: str = "",
) -> str:
    """Compose the GO-feedback prompt: plan prompt + accepted-review findings.

    Module-level composed builder (NOT a closure): :class:`AgentJob` is
    frozen and prompt builders run in-worker, so the builder must be a
    top-level function receiving everything via ``prompt_kwargs``.

    The reviewer accepted the plan (GO) and added non-blocking findings.
    The planner integrates each finding that improves the plan and keeps
    the plan unchanged otherwise; the stage tolerates either outcome.

    Args:
        issue_number: GitHub issue number being refined.
        prior_review: The accepted review's findings text.
        issue_title: Source issue title.
        issue_body: Source issue body.
        advise_findings: Advise-step findings; empty string means no block.
        plan_history: Bounded current-plan context; empty string means none.

    Returns:
        The full plan prompt with the accepted-review findings block.

    """
    prompt = build_plan_prompt(issue_number, issue_title, issue_body, advise_findings)
    fenced = fence_content()
    findings = PromptCatalog.current().render(
        "planning/feedback_integrate.j2",
        untrusted_notice=fenced.untrusted_notice,
        findings_block=fenced.fence("REVIEW_FINDINGS", prior_review),
    )
    return prompt + findings


class PlanReviewStage(Stage):
    """Stage for reviewing and iterating on a plan.

    State machine (doc section "3. plan_review"):

    - ENTER: fast-forward at-or-past ``state:plan-go`` -> ADVANCE (mirrors
      planning's on_enter guard, defends against a restart re-seeding the
      item directly into plan_review); otherwise reset the cycle-relative
      review counter when a new plan cycle begins, then route to
      REVIEW_WAIT.
    - REVIEW_WAIT: clear any stale verdict, then submit the reviewer job;
      the verdict is parsed in-worker by ``parse_plan_review_verdict`` and lands
      in ``item.payload["review_verdict"]``.
    - EVAL [M]: real verdicts (GO/NOGO) advance both iteration
      counters; ERROR/missing verdicts never do. GO with non-blocking
      severity findings -> one bounded final planner integration pass
      (FEEDBACK_WAIT, #3643); clean GO -> durably apply
      ``state:plan-go`` (write BEFORE the advancing outcome), emit an immutable
      learning intent, then ADVANCE; NOGO within the cycle-relative iteration budget ->
      durably apply ``state:plan-no-go`` and enter AMEND_WAIT within budget,
      or FAIL_BACK("nogo") while plan cycles remain;
      or FAIL_BACK("plan_cycles_exhausted") once exhausted; ERROR/missing
      verdict -> labels untouched, RETRY (bounded by
      ``REVIEW_ERROR_RETRY_CAP`` consecutive failures, then FINISH_FAIL).
    - AMEND_WAIT: submit the planner amend job (:func:`build_amend_prompt`
      carries the reviewer feedback block), loop to REVIEW_WAIT.
    - FEEDBACK_WAIT: submit the one final planner integration job
      (:func:`build_feedback_integrate_prompt` carries the accepted
      review's findings). Non-blocking: the integrated output is captured
      for the implementer and EVAL re-runs, whose guard routes straight to
      the GO completion. The plan journal artifact stays exactly as
      reviewed (#3643).
    Fail routes (ROUTES): "nogo" -> planning, "plan_cycles_exhausted" ->
    finished(fail).
    """

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Fast-forward at-or-past state:plan-go, else reset the cycle-relative review counter.

        A restart can re-seed an item directly into plan_review carrying
        state:plan-go (product_to_work_item classifies issues into arbitrary
        entry stages), bypassing planning's on_enter guard entirely. Without
        this check the item would run a full reviewer job again instead of
        fast-forwarding, wasting an invocation and risking a spurious
        re-verdict (#1870).

        ``attempts["plan_review_iter"]`` is per-lifetime (routing.py:
        attempts are never reset), so the per-cycle review budget is
        tracked in ``payload["review_round"]``. The reset keys on
        ``attempts["plan_cycles"]`` (recorded in ``payload["review_cycle"]``)
        so it fires exactly once per fail-back cycle: a same-cycle re-entry
        (e.g. the ERROR-path RETRY) matches the recorded cycle and keeps its
        round count. A fresh cycle also restarts the consecutive
        reviewer-failure streak (``payload["review_error_retries"]``,
        #1869) — otherwise a leftover count from a prior transient failure
        could let the first ERROR of the new cycle immediately exceed
        ``REVIEW_ERROR_RETRY_CAP``. Idempotent — a literal double on_enter
        is a no-op.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            A StageOutcome to fast-forward past review, or None to proceed
            to step().

        """
        if item.payload.get("_fleet_attempt_digest"):
            return _fleet_finalized_plan_outcome(item, ctx)
        if item.issue is not None:
            labels = _require_issue_labels(item, ctx)
            try:
                comments = reconcile_plan_journal(item.issue, ctx.github)
            except CommentJournalReadError as exc:
                logger.warning(
                    "plan_review:%d: plan journal reconciliation read failed: %s",
                    item.issue,
                    exc,
                )
                return StageOutcome(
                    Disposition.RETRY,
                    f"plan journal read failed: {exc}",
                )
            snapshot = journal_snapshot(comments)
            if not snapshot.current_plan:
                finalized_outcome = _finalized_plan_admission_outcome(item, ctx, labels)
                if finalized_outcome is not None:
                    return finalized_outcome
                return _missing_plan_admission_outcome(item, labels)

            if is_exclusive_plan_state(labels, STATE_PLAN_GO):
                item.payload["plan_text"] = snapshot.current_plan
                item.payload["plan_revision"] = snapshot.revision
                item.payload[_REVIEWED_PLAN_COMMENT_BODY] = snapshot.current_plan_body
                if item.payload.get("stale_plan_recheck"):
                    # An armed stale-plan recheck runs one review round: the
                    # plan stays valid, the reviewer names tweaks, and a
                    # NO-GO blocks for human intervention instead of
                    # failing back to re-planning.
                    logger.info(
                        "plan_review:%d: stale-plan recheck armed; running the "
                        "recheck round against the current tree",
                        item.issue,
                    )
                else:
                    logger.info("plan_review:%d: already plan-go; advancing", item.issue)
                    return StageOutcome(Disposition.ADVANCE, "plan already approved")

            item.payload["plan_text"] = snapshot.current_plan
            item.payload["plan_revision"] = snapshot.revision
            item.payload[_REVIEWED_PLAN_COMMENT_BODY] = snapshot.current_plan_body
            if snapshot.current_review and snapshot.current_review_revision == snapshot.revision:
                item.payload["prior_review"] = snapshot.current_review

            session_outcome = _restore_review_conversation(
                item,
                ctx,
                plan_text=snapshot.current_plan,
                revision=snapshot.revision,
                has_prior_review=(
                    bool(snapshot.current_review)
                    and not is_pending_review(
                        snapshot.current_review,
                        revision=snapshot.current_review_revision or snapshot.revision,
                    )
                ),
            )
            if session_outcome is not None:
                return session_outcome

        cycle = item.attempts.get("plan_cycles", 0)
        if item.payload.get("review_cycle") != cycle:
            item.payload["review_cycle"] = cycle
            item.payload["review_round"] = 0
            # Fresh plan cycle: the consecutive reviewer-failure streak
            # restarts too (#1869) — a leftover count from a prior
            # transient reviewer failure must not let the first ERROR of
            # a new cycle immediately exceed REVIEW_ERROR_RETRY_CAP.
            item.payload.pop("review_error_retries", None)
            item.payload.pop("unclassified_review_text", None)
            item.payload.pop("review_verdict_followup_attempted", None)
            item.payload.pop("review_verdict_followup_pending", None)
        return None

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next plan-review action for the item's current state.

        Args:
            item: The work item with current state.
            ctx: Stage context.

        Returns:
            Continue, JobRequest, or StageOutcome.

        """
        if not item.issue:
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")

        if item.payload.get("review_session_error") == "review-session-lost":
            recovery_outcome = _restart_review_conversation(item, ctx)
            if recovery_outcome is not None:
                return recovery_outcome
            return Continue(next_state="REVIEW_WAIT")

        # A provider outage before any session was bound leaves nothing to
        # recover, so the same cycle is retried in place (#3578). Clear the
        # marker only on the submit path, so an item that resumes in ENTER
        # keeps the recorded provider cause.
        if item.state == "ENTER":
            return Continue(next_state="REVIEW_WAIT")

        if item.state == "REVIEW_WAIT":
            return self._review_wait_step(item, ctx)

        if item.state == "EVAL":
            return self._publish_accepted_review(item, ctx)

        if item.state == "AMEND_WAIT":
            return self._submit_amend_job(item, ctx)

        if item.state == FEEDBACK_WAIT:
            return self._submit_feedback_integrate_job(item, ctx)

        if item.state.startswith("DECOMP_"):
            decomposition_outcome = self._decomposition_step(item, ctx)
            if decomposition_outcome is not None:
                return decomposition_outcome

        logger.warning("plan_review:%d: unknown state %r", item.issue, item.state)
        return StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {item.state}")

    def _review_wait_step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Own the REVIEW_WAIT submission path and its restart reconciliation."""
        if (
            item.payload.get("review_session_error") == "review-provider-unavailable"
            and "review_model_override" in item.payload
        ):
            session_outcome = _restore_review_conversation(
                item,
                ctx,
                plan_text=str(item.payload.get("plan_text") or ""),
                revision=int(item.payload.get("plan_revision") or 1),
                has_prior_review=bool(item.payload.get("prior_review")),
            )
            if session_outcome is not None:
                return session_outcome
        _retry_after_provider_unavailable(item)
        transition_outcome = _confirm_pending_amendment_transition(item, ctx)
        if transition_outcome is not None:
            return transition_outcome

        no_progress_reason = str(item.payload.get("no_progress_reason") or "")
        if no_progress_reason:
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "planning made no progress; the plan did not change",
            )

        # Restart reconciliation (issue #2982): a prior run that already
        # reached a decomposition trigger starts or resumes decomposition
        # without another plan review. Durable NOGO records and the
        # complete-plan size are the only authority.
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        comments = ctx.github.issue_comments(item.issue)
        epoch = requirements_epoch(_issue_body(item))
        trigger = decomposition_required(item, comments, epoch=epoch)
        if trigger is not None and item.issue not in ctx.generated_issue_ids:
            return self._start_or_resume_decomposition(item, ctx, trigger)

        # Counters advance in EVAL, and only for real verdicts — a
        # submission is not an iteration (#1554/#1794). The 0-based
        # prompt iteration is the cycle-relative round count.
        round_index = item.payload.get("review_round", 0)
        store = ctx.plan_review_sessions
        cycle_id = str(item.payload.get("plan_review_cycle_id") or "")
        try:
            review_session = store.load(cycle_id) if store is not None and cycle_id else None
            transcript = store.transcript(cycle_id) if review_session is not None else ""
        except PlanReviewSessionLostError as exc:
            logger.error("plan_review:%d: review-session-lost: %s", item.issue, exc)
            assert store is not None and cycle_id  # noqa: S101 - guarded store access above
            store.mark_recovery_required(cycle_id)
            item.payload["review_session_error"] = "review-session-lost"
            recovery_outcome = _restart_review_conversation(item, ctx)
            if recovery_outcome is not None:
                return recovery_outcome
            return Continue(next_state="REVIEW_WAIT")
        # Clear any stale verdict at submission so a failed later round
        # can never replay an earlier round's verdict in EVAL.
        item.payload.pop("review_verdict", None)
        item.payload.pop("accepted_plan_review", None)
        logger.info(
            "plan_review:%d: requesting review job cycle=%s session=%s round=%d revision=%s",
            item.issue,
            cycle_id or "legacy",
            (review_session.session_id if review_session is not None else None) or "pending",
            round_index,
            item.payload.get("plan_revision", 1),
        )
        workspace = planning_source_workspace_binding(item, ctx)
        verdict_followup = bool(item.payload.pop("review_verdict_followup_pending", False))
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=(
                review_session.provider
                if review_session is not None
                else agent_provider(ctx, "reviewer")
            ),
            model=(
                _restore_reviewer_selection(
                    review_session.provider,
                    review_session.reviewer_model,
                    review_session.reviewer_config,
                )
                if review_session is not None
                else _selected_reviewer_model(item, ctx)
            ),
            prompt_builder=get_plan_loop_review_prompt,
            cwd=_review_job_cwd(item, ctx, workspace),
            timeout_s=stage_timeout(ctx, "reviewer", plan_reviewer_claude_timeout),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLAN_REVIEWER,
            session_key=review_session.session_key if review_session is not None else "",
            require_new_session=review_session is not None,
            resume_session_id=(review_session.session_id if review_session is not None else None),
            resume_binding=(
                AgentSessionBinding.from_json(review_session.session_binding)
                if review_session is not None and review_session.session_binding
                else None
            ),
            resume_selection=(
                (
                    review_session.provider,
                    str(
                        _restore_reviewer_selection(
                            review_session.provider,
                            review_session.reviewer_model,
                            review_session.reviewer_config,
                        )
                    ),
                )
                if review_session is not None
                else None
            ),
            session_checkpoint=_review_session_checkpoint(ctx, cycle_id),
            execution_request=ExecutionRequest(
                AgentRole.PLAN_REVIEWER,
                AgentOperation.PLAN_REVIEW,
                (
                    SessionLifecycle.RESUME_REQUIRED
                    if review_session is not None and review_session.session_id
                    else SessionLifecycle.START_NEW
                ),
            ),
            # get_plan_loop_review_prompt takes a 0-based iteration index
            # (full-sweep suffix on the final iteration). Issue title/body
            # are seeded into item.payload by the coordinator (#1817).
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "plan_text": item.payload.get("plan_text", ""),
                # Populated from the payload when present; wiring the
                # legacy capture_planner_learnings step into the pipeline
                # is deferred to the coordinator slice (#1817).
                "learnings": item.payload.get("learnings", ""),
                "iteration": round_index,
                "prior_review": item.payload.get("prior_review") or None,
                "advise_findings": item.payload.get("advise_findings", ""),
                # The reviewer already receives the complete current plan
                # and direct prior critique; superseded journal entries
                # would only duplicate stale feedback.
                "plan_history": "",
                "planning_cycle_id": cycle_id,
                "reviewer_session_id": (
                    review_session.session_id or "pending" if review_session is not None else ""
                ),
                "plan_revision": int(item.payload.get("plan_revision") or 1),
                "plan_fingerprint": plan_fingerprint(str(item.payload.get("plan_text") or "")),
                "review_transcript": (
                    str(item.payload.get("unclassified_review_text") or "")
                    if verdict_followup
                    else transcript
                ),
                "verdict_followup": verdict_followup,
            },
            parse=parse_plan_review_verdict,  # verdict parsed in-worker
            descr="review",
        )
        return JobRequest(job, on_done_state="EVAL")

    def _publish_accepted_review(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Retry bounded publication failures with the same accepted verdict."""
        try:
            outcome = self._eval(item, ctx)
        except CommentAliasConflictError:
            raise
        except (RuntimeError, OSError, SubprocessError) as exc:
            if not isinstance(item.payload.get("accepted_plan_review"), _AcceptedPlanReview):
                raise
            logger.warning("plan_review:%s: publication failed: %s", item.issue, exc)
            outcome = StageOutcome(Disposition.RETRY, "review publication failed")
        if (
            isinstance(outcome, StageOutcome)
            and outcome.disposition is Disposition.RETRY
            and item.state == EVAL
            and isinstance(item.payload.get("accepted_plan_review"), _AcceptedPlanReview)
        ):
            retries = int(item.payload.get("review_publication_retries", 0)) + 1
            item.payload["review_publication_retries"] = retries
            if retries > REVIEW_ERROR_RETRY_CAP:
                return StageOutcome(Disposition.FINISH_FAIL, "review publication retries exhausted")
            item.payload["retry_delay_s"] = float(2 ** (retries - 1))
        return outcome

    def _capture_accepted_review(
        self, item: WorkItem, ctx: StageContext, issue_number: int
    ) -> _AcceptedPlanReview | StageOutcome | None:
        """Bind one new verdict to its exact reviewed plan comment."""
        verdict = item.payload.pop("review_verdict", None)
        if verdict is None:
            return None
        if not isinstance(verdict, ReviewVerdict) or verdict.verdict not in {"GO", "NOGO"}:
            item.payload.pop("accepted_plan_review", None)
            raw_review = str(item.payload.get("unclassified_review_text") or "").strip()
            if raw_review and not item.payload.get("review_verdict_followup_attempted"):
                item.payload["review_verdict_followup_attempted"] = True
                item.payload["review_verdict_followup_pending"] = True
                item.state = REVIEW_WAIT
                logger.warning(
                    "plan_review:%s: review has no GO or NOGO; asking the reviewer "
                    "to classify its own review once",
                    item.issue,
                )
                return StageOutcome(Disposition.RETRY, "review verdict clarification requested")
            return _reviewer_failure(item, "reviewer returned no GO or NOGO after clarification")
        item.payload.pop("unclassified_review_text", None)
        item.payload.pop("review_verdict_followup_attempted", None)
        item.payload.pop("review_verdict_followup_pending", None)
        revision = int(item.payload.get("plan_revision") or 0)
        plan_comment_body = item.payload.get(_REVIEWED_PLAN_COMMENT_BODY)
        if not isinstance(plan_comment_body, str) or not plan_comment_body:
            # Preserve marker-conflict detection for a legacy in-flight item
            # that predates the exact-body binding.
            journal_snapshot(ctx.github.issue_comments(issue_number))
            item.payload.pop("accepted_plan_review", None)
            return _reviewer_failure(item, "reviewed plan identity is unavailable")
        review = _AcceptedPlanReview(verdict, revision, plan_comment_body)
        item.payload["accepted_plan_review"] = review
        item.payload.pop("review_publication_retries", None)
        return review

    def _eval(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Publish one accepted review without charging its retries again."""
        if item.issue is None:  # guarded by step(); kept for type narrowing
            return StageOutcome(Disposition.FINISH_FAIL, "no issue number")
        issue_number = item.issue
        review = self._capture_accepted_review(item, ctx, issue_number)
        if isinstance(review, StageOutcome):
            return review
        if review is None:
            review = item.payload.get("accepted_plan_review")
        if not isinstance(review, _AcceptedPlanReview):
            if item.payload.get("review_session_error") == "review-provider-unavailable":
                return _provider_failure_outcome(item, ctx)
            return _reviewer_failure(item, "no verdict found")
        verdict = review.verdict

        if identity_outcome := self._review_identity_outcome(item, ctx, review):
            return identity_outcome
        if review.charged_round is None:
            round_done = int(item.payload.get("review_round", 0)) + 1
            review = replace(review, charged_round=round_done)
            item.payload["accepted_plan_review"] = review
            item.payload["review_round"] = round_done
            item.attempts["plan_review_iter"] = item.attempts.get("plan_review_iter", 0) + 1
            item.payload["review_error_retries"] = 0
        # GO/NOGO audit text is durable before its proposed label. Regardless
        # of prose, only the confirmed exclusive label below can route.
        if not review.comment_published:
            ctx.github.upsert_issue_comment(
                issue_number,
                PLAN_REVIEW_CANONICAL_MARKER,
                _normalize_review_comment(verdict.raw, revision=review.revision),
            )
            review = replace(review, comment_published=True)
            item.payload["accepted_plan_review"] = review
        if identity_outcome := self._review_identity_outcome(item, ctx, review):
            return identity_outcome

        if verdict.is_go:
            return self._complete_go_with_feedback(item, ctx, review, issue_number)
        return self._complete_nogo(item, ctx, review)

    def _complete_go_with_feedback(
        self, item: WorkItem, ctx: StageContext, review: _AcceptedPlanReview, issue_number: int
    ) -> StepResult:
        """Run one bounded feedback integration before completing an accepted GO."""
        verdict = review.verdict
        # One bounded final planner pass integrates the accepted review's
        # findings when they can improve the plan (#3643). Either outcome
        # (changed plan, kept plan, failed pass) completes the GO below;
        # the guard keys the pass to at most one attempt per lifetime.
        if not item.payload.get(_PLAN_FEEDBACK_INTEGRATED) and _GO_FEEDBACK_RE.search(verdict.raw):
            item.payload[_PLAN_FEEDBACK_INTEGRATED] = True
            item.payload[_PLAN_REVIEW_FINDINGS] = verdict.raw
            logger.info(
                "plan_review:%d: GO with findings; requesting one final planner integration pass",
                issue_number,
            )
            return Continue(next_state=FEEDBACK_WAIT)
        return self._complete_go(item, ctx, review)

    def _submit_amend_job(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Submit the planner amend job with the reviewer feedback block.

        Mirrors the AMEND_WAIT contract: build_amend_prompt composes
        get_plan_prompt with the reviewer feedback block in-worker, and the
        worker resumes the planner session (#1817 wires that setup).
        """
        assert item.issue is not None  # noqa: S101 - the stage validates the issue
        logger.info("plan_review:%d: requesting amend job", item.issue)
        workspace = planning_source_workspace_binding(item, ctx)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx, "planner"),
            model=stage_model(ctx, "planner", planner_model),
            prompt_builder=build_amend_prompt,
            cwd=_review_job_cwd(item, ctx, workspace),
            timeout_s=stage_timeout(ctx, "planner", planner_claude_timeout),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLANNER,
            execution_request=ExecutionRequest(
                AgentRole.PLANNER,
                (
                    AgentOperation.AMEND
                    if AGENT_PLANNER in item.session_bindings or AGENT_PLANNER in item.session_ids
                    else AgentOperation.PLAN
                ),
                agent_session_lifecycle(item, AGENT_PLANNER),
            ),
            resume_session_id=item.session_ids.get(AGENT_PLANNER),
            resume_binding=item.session_bindings.get(AGENT_PLANNER),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
                "prior_review": item.payload.get("prior_review", ""),
                "plan_history": _plan_history(ctx.github.issue_comments(item.issue)),
            },
            descr="amend",
        )
        return JobRequest(job, on_done_state="REVIEW_WAIT")

    def _submit_feedback_integrate_job(self, item: WorkItem, ctx: StageContext) -> JobRequest:
        """Submit the one bounded final planner integration job (#3643).

        The pass resumes the planner session with the accepted review's
        findings. The stage tolerates either outcome: the integrated plan
        text is captured for the implementer, and EVAL's guard routes a
        second GO straight to the GO completion.
        """
        assert item.issue is not None  # noqa: S101 - the stage validates the issue
        logger.info("plan_review:%d: requesting feedback-integrate job", item.issue)
        workspace = planning_source_workspace_binding(item, ctx)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx, "planner"),
            model=stage_model(ctx, "planner", planner_model),
            prompt_builder=build_feedback_integrate_prompt,
            cwd=_review_job_cwd(item, ctx, workspace),
            timeout_s=stage_timeout(ctx, "planner", planner_claude_timeout),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLANNER,
            execution_request=ExecutionRequest(
                AgentRole.PLANNER,
                (
                    AgentOperation.AMEND
                    if AGENT_PLANNER in item.session_bindings or AGENT_PLANNER in item.session_ids
                    else AgentOperation.PLAN
                ),
                agent_session_lifecycle(item, AGENT_PLANNER),
            ),
            resume_session_id=item.session_ids.get(AGENT_PLANNER),
            resume_binding=item.session_bindings.get(AGENT_PLANNER),
            # build_feedback_integrate_prompt composes the plan prompt with
            # the accepted-review findings block in-worker. The pass is
            # non-blocking: either outcome re-enters EVAL, whose guard
            # routes straight to the GO completion.
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "advise_findings": item.payload.get("advise_findings", ""),
                "prior_review": item.payload.get(_PLAN_REVIEW_FINDINGS, ""),
                "plan_history": _plan_history(ctx.github.issue_comments(item.issue)),
            },
            descr="feedback-integrate",
        )
        return JobRequest(job, on_done_state="EVAL")

    def _complete_nogo(
        self, item: WorkItem, ctx: StageContext, review: _AcceptedPlanReview
    ) -> StepResult:
        """Confirm NOGO and apply the remaining review and plan budgets."""
        assert item.issue is not None  # noqa: S101 - EVAL validates the issue
        round_done = review.charged_round
        assert round_done is not None  # noqa: S101 - EVAL charges the review
        verdict = review.verdict

        # A stale-plan recheck NO-GO never re-plans: the accepted plan stays
        # the authority, the item blocks, and a human decides.
        if item.payload.pop("stale_plan_recheck", None) is not None:
            reason = verdict.raw or verdict.verdict
            logger.warning(
                "plan_review:%d: stale-plan recheck failed (%s); blocking for "
                "human intervention — the plan is not redone",
                item.issue,
                verdict.verdict,
            )
            try:
                ctx.github.upsert_issue_comment(
                    item.issue,
                    STALE_PLAN_RECHECK_BLOCK_MARKER,
                    render_stale_plan_recheck_block(str(reason)),
                )
            except RuntimeError as exc:
                logger.warning(
                    "plan_review:%d: stale-plan recheck block comment write failed: %s",
                    item.issue,
                    exc,
                )
            _complete_review_cycle(item, ctx)
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "stale plan recheck failed: the accepted plan no longer "
                "applies to the current tree; human intervention required "
                "(plan kept, not redone)",
            )

        # Every NOGO is durable control state, including rounds that can still
        # amend. The replacement plan publication transitions back to
        # state:needs-plan only after both canonical comments are updated.
        review = replace(review, label_proposed=True)
        item.payload["accepted_plan_review"] = review
        self._write_verdict_labels(item.issue, ctx, is_go=False)
        if not is_exclusive_plan_state(
            _require_issue_labels(item, ctx),
            STATE_PLAN_NO_GO,
        ):
            return StageOutcome(Disposition.RETRY, "plan-no-go label was not confirmed")
        if identity_outcome := self._review_identity_outcome(item, ctx, review):
            return identity_outcome

        # Every real NOGO verdict is durable control state bound to its review
        # identity. The record never persists for infrastructure failures, so
        # the convergence count only reads real judgements (#2982).
        epoch = requirements_epoch(_issue_body(item))
        record = PlanNogoRecord(
            repository=item.repo,
            issue=item.issue,
            requirements_epoch=epoch,
            plan_revision=review.revision,
            review_cycle=item.attempts.get("plan_cycles", 0),
            review_round=review.charged_round or 0,
            review_digest=plan_nogo_digest(
                plan_revision=review.revision,
                review_cycle=item.attempts.get("plan_cycles", 0),
                review_round=review.charged_round or 0,
                review_raw=verdict.raw,
            ),
            findings=_nogo_findings(item),
            recorded_at=recorded_at(),
        )
        publish_plan_nogo(item.issue, record, ctx.github)
        if item.issue in ctx.generated_issue_ids:
            # Same-run generated-child rule (issue #2982): persist and confirm
            # the one valid NOGO record and the exclusive state:plan-no-go
            # label, create no decomposition artifact, skip only this run, and
            # keep the label so a later run discovers the issue again.
            _complete_review_cycle(item, ctx)
            logger.info(
                "plan_review:%d: generated child defers decomposition to a later run",
                item.issue,
            )
            return StageOutcome(Disposition.SKIP, GENERATED_CHILD_DEFERRAL)
        trigger = decomposition_required(
            item,
            ctx.github.issue_comments(item.issue),
            epoch=epoch,
        )
        if trigger is not None:
            # The record is durable first, so a later run or a restart keeps
            # the confirmed convergence count. The stage now owns the
            # decomposition transaction (#2982).
            item.payload[_DECOMP_TRIGGER_KEY] = trigger
            logger.warning(
                "plan_review:%d: %s decomposition trigger after round %d",
                item.issue,
                trigger,
                review.charged_round,
            )
            _complete_review_cycle(item, ctx)
            return self._start_or_resume_decomposition(item, ctx, trigger)

        # NOGO: amend within the cycle-relative budget.
        budget_iter = ctx.budget("plan_review_iter")
        if round_done < budget_iter:
            logger.info(
                "plan_review:%d: %s verdict (round %d/%d); amending plan",
                item.issue,
                verdict.verdict,
                round_done,
                budget_iter,
            )
            return Continue(next_state="AMEND_WAIT")

        # Iteration cap: fail back with state:plan-no-go already durable —
        # "nogo" while plan_cycles remain, "plan_cycles_exhausted" once the
        # cycle budget is consumed (routes to finished(fail) via ROUTES).
        logger.warning(
            "plan_review:%d: %s exhausted (round %d/%d); applying no-go label",
            item.issue,
            verdict.verdict,
            round_done,
            budget_iter,
        )
        cycles = item.attempts.get("plan_cycles", 0) + 1
        item.attempts["plan_cycles"] = cycles
        if cycles >= ctx.budget("plan_cycles"):
            _complete_review_cycle(item, ctx)
            logger.error(
                "plan_review:%d: plan_cycles exhausted (%d/%d)",
                item.issue,
                cycles,
                ctx.budget("plan_cycles"),
            )
            return StageOutcome(Disposition.FAIL_BACK, "plan_cycles_exhausted")
        return StageOutcome(Disposition.FAIL_BACK, "nogo")

    # --- Decomposition transaction (issue #2982) ---------------------------

    def _decomposition_step(self, item: WorkItem, ctx: StageContext) -> StepResult | None:
        """Dispatch one decomposition state, or None for an unknown state."""
        handlers: dict[str, Callable[[WorkItem, StageContext], StepResult]] = {
            DECOMP_GEN_WAIT: self._submit_decomposition_generation,
            DECOMP_GEN_EVAL: self._eval_decomposition_generation,
            DECOMP_REVIEW_WAIT: self._submit_decomposition_review,
            DECOMP_REVIEW_EVAL: self._eval_decomposition_review,
            DECOMP_CHILDREN_WAIT: self._submit_decomposition_children,
            DECOMP_CHILDREN_EVAL: self._eval_decomposition_children,
            DECOMP_PARENT_WAIT: self._submit_parent_transition,
            DECOMP_PARENT_EVAL: self._eval_parent_transition,
            DECOMP_LABELS_WAIT: self._submit_child_labels,
            DECOMP_LABELS_EVAL: self._eval_child_labels,
        }
        handler = handlers.get(item.state)
        if handler is None:
            return None
        return handler(item, ctx)

    def _start_or_resume_decomposition(
        self, item: WorkItem, ctx: StageContext, trigger: str
    ) -> StepResult:
        """Publish the intent, then resume at the exact durable record point.

        The intent is immutable per parent and requirements epoch. A later
        plan fingerprint in the same epoch recovers the intent and can never
        create a second tree. Partial records from a restart reconcile in the
        published order without redoing a confirmed step.
        """
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        if ctx.dry_run:
            preview = self._decomposition_dry_run_preview(item, ctx, trigger)
            logger.warning(
                "plan_review:%d: decomposition dry-run preview: %s",
                item.issue,
                preview,
            )
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_dry_run_preview")
        epoch = requirements_epoch(_issue_body(item))
        item.payload[_DECOMP_EPOCH_KEY] = epoch
        item.payload[_DECOMP_TRIGGER_KEY] = trigger
        comments = ctx.github.issue_comments(item.issue)
        intent = recover_decomposition_intent(comments, epoch)
        if intent is None:
            intent = publish_decomposition_intent(
                item.issue,
                build_decomposition_intent(item, comments, epoch, trigger),
                ctx.github,
            )
        item.payload[_DECOMP_INTENT_KEY] = intent
        comments = ctx.github.issue_comments(item.issue)
        # Fast-forward through durable records, newest first.
        transition = recover_decomposition_transition(comments, epoch)
        bindings = recover_decomposition_bindings(comments, epoch)
        if transition is not None and bindings is not None:
            seal = recover_decomposition_seal(comments, epoch)
            if seal is not None and bindings.seal_digest == seal.seal_digest:
                return self._resume_bound_children(item, ctx, seal, bindings)
        seal = recover_decomposition_seal(comments, epoch)
        if seal is not None:
            return Continue(next_state=DECOMP_CHILDREN_WAIT)
        draft_nodes = recover_decomposition_draft(comments, epoch)
        if draft_nodes is not None:
            units = normalize_work_tree(draft_nodes)
            if units is None:
                logger.error(
                    "plan_review:%d: durable draft record is unreadable; fail closed",
                    item.issue,
                )
                return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
            item.payload[_DECOMP_DRAFT_KEY] = [unit.as_dict() for unit in units]
            item.payload[_DECOMP_DRAFT_DIGEST_KEY] = tree_digest(units)
            return Continue(next_state=DECOMP_REVIEW_WAIT)
        return Continue(next_state=DECOMP_GEN_WAIT)

    def _decomposition_dry_run_preview(
        self, item: WorkItem, ctx: StageContext, trigger: str
    ) -> dict[str, object]:
        """Build the read-only preview of the complete decomposition transition."""
        from hephaestus.automation.plan_decomposition import build_requirement_manifest

        assert item.issue is not None  # noqa: S101 - step() validates the issue
        epoch = requirements_epoch(_issue_body(item))
        comments = ctx.github.issue_comments(item.issue)
        intent = recover_decomposition_intent(comments, epoch)
        requirement_ids = (
            [ref["identifier"] for ref in intent.requirement_manifest]
            if intent is not None
            else [
                ref.identifier
                for ref in build_requirement_manifest(parent_requirement_texts(_issue_body(item)))
            ]
        )
        finding_ids = (
            [ref["identifier"] for ref in intent.finding_manifest] if intent is not None else []
        )
        return {
            "trigger": trigger,
            "requirements_epoch": epoch,
            "source_plan_revision": int(item.payload.get("plan_revision") or 1),
            "requirement_identifiers": requirement_ids,
            "finding_identifiers": finding_ids,
            "generation_job": "one-shot decomposition generation session",
            "review_job": "one-shot independent review bound to the draft digest",
            "child_labels": [STATE_NEEDS_PLAN],
            "parent_transition": {
                "labels_added": ["epic", STATE_SKIP],
                "labels_removed": [STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO],
                "title_and_body": "preserved",
            },
        }

    def _submit_decomposition_generation(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the one-shot decomposition generation session."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        intent = item.payload.get(_DECOMP_INTENT_KEY)
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        if not isinstance(intent, DecompositionIntent) or not epoch:
            return StageOutcome(Disposition.FINISH_FAIL, "decomposition intent is unavailable")
        rounds = int(item.payload.get(_DECOMP_GEN_ROUNDS_KEY, 0))
        if rounds >= DECOMPOSITION_MODEL_ROUNDS:
            logger.error("plan_review:%d: generation rounds exhausted", item.issue)
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_generation_exhausted")
        item.payload[_DECOMP_GEN_ROUNDS_KEY] = rounds + 1
        comments = ctx.github.issue_comments(item.issue)
        plan_text = complete_current_plan_context(comments)
        logger.info(
            "plan_review:%d: requesting decomposition generation job epoch=%s round=%d",
            item.issue,
            epoch,
            rounds + 1,
        )
        workspace = planning_source_workspace_binding(item, ctx)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx, "planner"),
            model=stage_model(ctx, "planner", planner_model),
            prompt_builder=get_plan_decomposition_prompt,
            cwd=_review_job_cwd(item, ctx, workspace),
            timeout_s=stage_timeout(ctx, "planner", planner_claude_timeout),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLANNER,
            session_key=decomposition_generation_session_key(epoch),
            require_new_session=True,
            execution_request=ExecutionRequest(
                AgentRole.PLANNER,
                AgentOperation.PLAN,
                SessionLifecycle.START_NEW,
            ),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "issue_body": item.payload.get("issue_body", ""),
                "plan_text": plan_text,
                "requirement_manifest": intent.requirement_manifest,
                "finding_manifest": intent.finding_manifest,
                "requirements_epoch": epoch,
                "source_plan_revision": intent.source_plan_revision,
                "source_plan_fingerprint": intent.source_plan_fingerprint,
                "rejected_draft_digest": str(item.payload.get(_DECOMP_REJECTED_KEY) or ""),
                "validation_errors": item.payload.get(_DECOMP_ERRORS_KEY, ()),
                "review_findings": str(item.payload.get(_DECOMP_FINDINGS_KEY) or ""),
            },
            descr="decompose",
        )
        return JobRequest(job, on_done_state=DECOMP_GEN_EVAL)

    def _eval_decomposition_generation(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Validate one generation output, publish the draft, or re-request."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        output = item.payload.pop(_DECOMP_OUTPUT_KEY, None)
        item.payload.pop(_DECOMP_ERRORS_KEY, None)
        if not isinstance(output, str) or not output.strip():
            return _decomposition_retry(
                item,
                "decomposition_generation_retries",
                wait_state=DECOMP_GEN_WAIT,
                cap=DECOMPOSITION_GITHUB_RETRIES,
                reason="generation_output",
            )
        units = parse_decomposition_tree_output(output)
        if units is None:
            errors: list[str] = ["output did not contain one valid complete work-unit JSON array"]
        else:
            intent = item.payload.get(_DECOMP_INTENT_KEY)
            requirement_ids = (
                [ref["identifier"] for ref in intent.requirement_manifest]
                if isinstance(intent, DecompositionIntent)
                else []
            )
            finding_ids = (
                [ref["identifier"] for ref in intent.finding_manifest]
                if isinstance(intent, DecompositionIntent)
                else []
            )
            errors = work_tree_errors(
                units,
                requirement_ids=requirement_ids,
                finding_ids=finding_ids,
            )
        if errors:
            if int(item.payload.get(_DECOMP_GEN_ROUNDS_KEY, 0)) >= DECOMPOSITION_MODEL_ROUNDS:
                logger.error(
                    "plan_review:%d: generation validation failed %d times; fail closed",
                    item.issue,
                    int(item.payload.get(_DECOMP_GEN_ROUNDS_KEY, 0)),
                )
                return StageOutcome(Disposition.BLOCKED, "plan_decomposition_generation_exhausted")
            item.payload[_DECOMP_REJECTED_KEY] = hashlib.sha256(output.encode("utf-8")).hexdigest()
            item.payload[_DECOMP_ERRORS_KEY] = tuple(errors[:16])
            logger.warning(
                "plan_review:%d: draft rejected with %d validation errors",
                item.issue,
                len(errors),
            )
            return Continue(next_state=DECOMP_GEN_WAIT)
        if units is None:  # pragma: no cover - errors is empty only after a parse
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_generation_invalid")
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        draft_digest = tree_digest(units)
        publish_decomposition_draft(
            item.issue, epoch, draft_digest, [unit.as_dict() for unit in units], ctx.github
        )
        item.payload[_DECOMP_DRAFT_KEY] = [unit.as_dict() for unit in units]
        item.payload[_DECOMP_DRAFT_DIGEST_KEY] = draft_digest
        item.payload.pop(_DECOMP_REJECTED_KEY, None)
        logger.info(
            "plan_review:%d: validated draft %s with %d work units",
            item.issue,
            draft_digest[:12],
            len(units),
        )
        return Continue(next_state=DECOMP_REVIEW_WAIT)

    def _submit_decomposition_review(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the one-shot independent review bound to the draft digest."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        draft = item.payload.get(_DECOMP_DRAFT_KEY)
        digest = str(item.payload.get(_DECOMP_DRAFT_DIGEST_KEY) or "")
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        if not isinstance(draft, list) or not draft or not digest or not epoch:
            return StageOutcome(Disposition.FINISH_FAIL, "decomposition draft is unavailable")
        rounds = int(item.payload.get(_DECOMP_REVIEW_ROUNDS_KEY, 0))
        if rounds >= DECOMPOSITION_MODEL_ROUNDS:
            logger.error("plan_review:%d: review rounds exhausted", item.issue)
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_review_exhausted")
        item.payload[_DECOMP_REVIEW_ROUNDS_KEY] = rounds + 1
        units = normalize_work_tree(draft)
        if units is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        layers = topological_layers(units)
        if layers is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        intent = item.payload.get(_DECOMP_INTENT_KEY)
        comments = ctx.github.issue_comments(item.issue)
        logger.info(
            "plan_review:%d: requesting independent decomposition review job draft=%s",
            item.issue,
            digest[:12],
        )
        workspace = planning_source_workspace_binding(item, ctx)
        job = AgentJob(
            repo=item.repo,
            issue=item.issue,
            agent=agent_provider(ctx, "reviewer"),
            model=stage_model(ctx, "reviewer", reviewer_model),
            prompt_builder=get_plan_decomposition_review_prompt,
            cwd=_review_job_cwd(item, ctx, workspace),
            timeout_s=stage_timeout(ctx, "reviewer", plan_reviewer_claude_timeout),
            workspace=workspace,
            sandbox="read-only",
            allowed_tools="Read,Glob,Grep",
            session_agent=AGENT_PLAN_REVIEWER,
            session_key=decomposition_review_session_key(epoch, digest),
            require_new_session=True,
            execution_request=ExecutionRequest(
                AgentRole.PLAN_REVIEWER,
                AgentOperation.PLAN_REVIEW,
                SessionLifecycle.START_NEW,
            ),
            prompt_kwargs={
                "issue_number": item.issue,
                "issue_title": item.payload.get("issue_title", ""),
                "plan_text": complete_current_plan_context(comments),
                "draft_digest": digest,
                "tree_json": _tree_json(units),
                "requirement_manifest": (
                    intent.requirement_manifest if isinstance(intent, DecompositionIntent) else []
                ),
                "finding_manifest": (
                    intent.finding_manifest if isinstance(intent, DecompositionIntent) else []
                ),
                "layers": layers,
            },
            descr="decomposition review",
        )
        return JobRequest(job, on_done_state=DECOMP_REVIEW_EVAL)

    def _eval_decomposition_review(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Accept only a valid GO receipt for the exact draft digest."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        output = item.payload.pop(_DECOMP_REVIEW_OUTPUT_KEY, None)
        digest = str(item.payload.get(_DECOMP_DRAFT_DIGEST_KEY) or "")
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        if not isinstance(output, str) or not output.strip():
            return _decomposition_retry(
                item,
                "decomposition_review_retries",
                wait_state=DECOMP_REVIEW_WAIT,
                cap=DECOMPOSITION_GITHUB_RETRIES,
                reason="review_output",
            )
        verdict = parse_decomposition_review_receipt(output, digest)
        if verdict != "GO":
            if int(item.payload.get(_DECOMP_REVIEW_ROUNDS_KEY, 0)) >= DECOMPOSITION_MODEL_ROUNDS:
                return StageOutcome(Disposition.BLOCKED, "plan_decomposition_review_exhausted")
            item.payload[_DECOMP_FINDINGS_KEY] = output.strip()[:2000]
            logger.warning("plan_review:%d: independent review did not approve", item.issue)
            return Continue(next_state=DECOMP_GEN_WAIT)
        publish_decomposition_review_receipt(
            item.issue,
            DecompositionReviewReceipt(
                requirements_epoch=epoch,
                draft_digest=digest,
                verdict="GO",
                recorded_at=recorded_at(),
            ),
            ctx.github,
        )
        draft = item.payload.get(_DECOMP_DRAFT_KEY)
        units = normalize_work_tree(draft) if isinstance(draft, list) else None
        if units is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        publish_decomposition_seal(
            item.issue,
            DecompositionSeal(
                requirements_epoch=epoch,
                draft_digest=digest,
                nodes=tuple(unit.as_dict() for unit in units),
            ),
            ctx.github,
        )
        logger.info("plan_review:%d: sealed approved tree %s", item.issue, digest[:12])
        return Continue(next_state=DECOMP_CHILDREN_WAIT)

    def _submit_decomposition_children(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the host-owned child creation and exact-readback job."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        draft = item.payload.get(_DECOMP_DRAFT_KEY)
        units = normalize_work_tree(draft) if isinstance(draft, list) else None
        if units is None or not epoch:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        layers = topological_layers(units)
        if layers is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        request = DecompositionChildrenRequest(
            repository=f"{ctx.org}/{item.repo}".casefold(),
            issue_number=item.issue,
            deadline_s=stage_network_deadline(ctx),
            tree=FrozenJson.snapshot([unit.as_dict() for unit in units]),
            requirements_epoch=epoch,
            layers=FrozenJson.snapshot([list(layer) for layer in layers]),
        )
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)),
                request=request,
                descr="decomposition_children",
            ),
            on_done_state=DECOMP_CHILDREN_EVAL,
        )

    def _eval_decomposition_children(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Publish the bindings after the host verified every child readback."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        receipt = item.payload.get(_DECOMP_CHILDREN_JOB_KEY)
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        if not isinstance(receipt, DecompositionChildrenCreated):
            return _decomposition_retry(
                item,
                _DECOMP_CHILDREN_RETRIES_KEY,
                wait_state=DECOMP_CHILDREN_WAIT,
                cap=DECOMPOSITION_GITHUB_RETRIES,
                reason="children_job",
            )
        if receipt.failures:
            logger.error(
                "plan_review:%d: child creation failed closed: %s",
                item.issue,
                "; ".join(receipt.failures),
            )
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_children_failed")
        children = receipt.children.thaw()
        if not isinstance(children, list):  # pragma: no cover - receipt validation
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_children_failed")
        publish_decomposition_bindings(
            item.issue,
            DecompositionBindings(
                requirements_epoch=epoch,
                seal_digest=receipt.seal_digest,
                bindings=tuple((str(entry["node_id"]), int(entry["issue"])) for entry in children),
            ),
            ctx.github,
        )
        item.payload[_DECOMP_CHILDREN_KEY] = [
            {
                "node_id": str(entry["node_id"]),
                "issue": int(entry["issue"]),
                "layer": int(entry["layer"]),
            }
            for entry in children
        ]
        return Continue(next_state=DECOMP_PARENT_WAIT)

    def _submit_parent_transition(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Submit the ordered parent summary and exclusive epic transition."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        children = item.payload.get(_DECOMP_CHILDREN_KEY)
        if not epoch or not isinstance(children, list) or not children:
            return StageOutcome(Disposition.FINISH_FAIL, "decomposition bindings unavailable")
        ordered = [(str(entry["node_id"]), int(entry["issue"])) for entry in children]
        draft = item.payload.get(_DECOMP_DRAFT_KEY)
        units = normalize_work_tree(draft) if isinstance(draft, list) else None
        if units is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        layers = topological_layers(units)
        if layers is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        summary_marker = f"<!-- hephaestus-plan-decomposition-summary:v1:{epoch} -->"
        summary_body = render_decomposition_summary_body(
            parent_issue=item.issue,
            epoch=epoch,
            ordered_issues=ordered,
            layers=layers,
            bindings=dict(ordered),
        )
        request = DecompositionParentTransitionRequest(
            repository=f"{ctx.org}/{item.repo}".casefold(),
            issue_number=item.issue,
            deadline_s=stage_network_deadline(ctx),
            summary_marker=summary_marker,
            summary_body=summary_body,
            labels_to_add=("epic", STATE_SKIP),
            labels_to_remove=(STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO),
        )
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)),
                request=request,
                descr="decomposition_parent_transition",
            ),
            on_done_state=DECOMP_PARENT_EVAL,
        )

    def _eval_parent_transition(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Confirm the exclusive parent state before any child label write."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        receipt = item.payload.get(_DECOMP_PARENT_JOB_KEY)
        if not isinstance(receipt, DecompositionParentTransitioned):
            return _decomposition_retry(
                item,
                _DECOMP_PARENT_RETRIES_KEY,
                wait_state=DECOMP_PARENT_WAIT,
                cap=DECOMPOSITION_GITHUB_RETRIES,
                reason="parent_transition",
            )
        if receipt.failures:
            logger.error(
                "plan_review:%d: parent transition failed closed: %s",
                item.issue,
                "; ".join(receipt.failures),
            )
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_parent_failed")
        return Continue(next_state=DECOMP_LABELS_WAIT)

    def _submit_child_labels(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Add and confirm state:needs-plan on every child after the parent."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        children = item.payload.get(_DECOMP_CHILDREN_KEY)
        if not epoch or not isinstance(children, list) or not children:
            return StageOutcome(Disposition.FINISH_FAIL, "decomposition bindings unavailable")
        labels = ctx.github.gh_issue_json(item.issue).get("labels", [])
        names = [
            entry["name"] if isinstance(entry, dict) else str(entry)
            for entry in (labels if isinstance(labels, list) else [])
        ]
        severity = next(
            (name for name in names if isinstance(name, str) and name.startswith("severity:")),
            None,
        )
        request = DecompositionChildLabelsRequest(
            repository=f"{ctx.org}/{item.repo}".casefold(),
            issue_number=item.issue,
            deadline_s=stage_network_deadline(ctx),
            children=FrozenJson.snapshot(children),
            labels_to_add=((STATE_NEEDS_PLAN, severity) if severity else (STATE_NEEDS_PLAN,)),
        )
        return JobRequest(
            GitHubJob(
                repo=item.repo,
                repo_root=Path(str(ctx.paths.repo_root)),
                request=request,
                descr="decomposition_child_labels",
            ),
            on_done_state=DECOMP_LABELS_EVAL,
        )

    def _eval_child_labels(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Publish the complete transition receipt and release the children."""
        assert item.issue is not None  # noqa: S101 - step() validates the issue
        receipt = item.payload.get(_DECOMP_LABELS_JOB_KEY)
        epoch = str(item.payload.get(_DECOMP_EPOCH_KEY) or "")
        if not isinstance(receipt, DecompositionChildLabelsApplied):
            return _decomposition_retry(
                item,
                _DECOMP_LABELS_RETRIES_KEY,
                wait_state=DECOMP_LABELS_WAIT,
                cap=DECOMPOSITION_GITHUB_RETRIES,
                reason="child_labels",
            )
        if receipt.failures:
            logger.error(
                "plan_review:%d: child labels failed closed: %s",
                item.issue,
                "; ".join(receipt.failures),
            )
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_labels_failed")
        confirmed = receipt.confirmed.thaw()
        confirmed_nodes = (
            [str(entry.get("node_id") or "") for entry in confirmed]
            if isinstance(confirmed, list)
            else []
        )
        publish_decomposition_transition(
            item.issue,
            DecompositionTransitionReceipt(
                requirements_epoch=epoch,
                labels_added=("epic", STATE_SKIP),
                labels_removed=(STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO),
                needs_plan_confirmed=tuple(confirmed_nodes),
                summary_confirmed=True,
            ),
            ctx.github,
        )
        children = item.payload.get(_DECOMP_CHILDREN_KEY)
        count = len(children) if isinstance(children, list) else 0
        logger.info(
            "plan_review:%d: decomposition complete; %d single-pull-request children",
            item.issue,
            count,
        )
        return StageOutcome(
            Disposition.FINISH_PASS,
            f"plan decomposed into {count} single-pull-request issues",
        )

    def _resume_bound_children(
        self,
        item: WorkItem,
        ctx: StageContext,
        seal: DecompositionSeal,
        bindings: DecompositionBindings,
    ) -> StepResult:
        """Complete a restart whose records already bind children to issues."""
        units = normalize_work_tree(seal.nodes)
        if units is None:
            return StageOutcome(Disposition.BLOCKED, "plan_decomposition_draft_unreadable")
        item.payload[_DECOMP_DRAFT_KEY] = [unit.as_dict() for unit in units]
        item.payload[_DECOMP_DRAFT_DIGEST_KEY] = seal.draft_digest
        item.payload[_DECOMP_CHILDREN_KEY] = [
            {"node_id": node_id, "issue": issue, "layer": 0} for node_id, issue in bindings.bindings
        ]
        return Continue(next_state=DECOMP_PARENT_WAIT)

    @staticmethod
    def _review_identity_outcome(
        item: WorkItem,
        ctx: StageContext,
        review: _AcceptedPlanReview,
        *,
        snapshot: JournalSnapshot | None = None,
    ) -> StageOutcome | None:
        """Reject a changed plan and remove any label this result proposed."""
        assert item.issue is not None  # noqa: S101 - EVAL validates the issue
        if snapshot is None:
            snapshot = journal_snapshot(ctx.github.issue_comments(item.issue))
        if (
            snapshot.current_plan
            and snapshot.revision == review.revision
            and snapshot.current_plan_body == review.plan_comment_body
        ):
            return None
        if review.label_proposed:
            add, remove = enter_planning_transition()
            ctx.github.edit_labels(item.issue, add=add, remove=remove)
            labels = _require_issue_labels(item, ctx)
            if not is_exclusive_plan_state(labels, STATE_NEEDS_PLAN):
                return StageOutcome(Disposition.RETRY, "changed plan state was not confirmed")
        return StageOutcome(
            Disposition.FAIL_BACK, "plan_changed" if snapshot.current_plan else "plan_missing"
        )

    def _complete_go(
        self, item: WorkItem, ctx: StageContext, review: _AcceptedPlanReview
    ) -> StepResult:
        """Apply GO and release the main stage."""
        assert item.issue is not None  # noqa: S101 - _eval narrows the issue
        logger.info("plan_review:%d: GO verdict; applying label and advancing", item.issue)
        review = replace(review, label_proposed=True)
        item.payload["accepted_plan_review"] = review
        self._write_verdict_labels(item.issue, ctx, is_go=True)
        if not is_exclusive_plan_state(
            _require_issue_labels(item, ctx),
            STATE_PLAN_GO,
        ):
            return StageOutcome(Disposition.RETRY, "plan-go label was not confirmed")
        if identity_outcome := self._review_identity_outcome(item, ctx, review):
            return identity_outcome
        _complete_review_cycle(item, ctx)
        if recheck := item.payload.pop("stale_plan_recheck", None):
            self._record_stale_plan_recheck(item, ctx, recheck)
        return StageOutcome(Disposition.ADVANCE, "plan approved")

    @staticmethod
    def _record_stale_plan_recheck(
        item: WorkItem,
        ctx: StageContext,
        recheck: object,
    ) -> None:
        """Publish the durable one-recheck-per-plan receipt (fail-open).

        The receipt keys on the plan fingerprint, so a later plan edit re-arms
        the staleness gate while an untouched plan rechecks only once.
        """
        if item.issue is None:  # the caller only arms a recheck on issue items
            return
        plan_sha256 = ""
        if isinstance(recheck, dict):
            value = recheck.get("plan_sha256")
            plan_sha256 = str(value) if isinstance(value, str) else ""
        if not plan_sha256:
            try:
                identity = newest_plan_comment_identity(ctx.github.issue_comments(item.issue))
            except (CommentJournalReadError, RuntimeError) as exc:
                logger.warning(
                    "plan_review:%d: stale-plan recheck receipt identity read failed: %s",
                    item.issue,
                    exc,
                )
                identity = None
            plan_sha256 = identity[2] if identity is not None and identity[2] else ""
            if not plan_sha256:
                logger.warning(
                    "plan_review:%d: stale-plan recheck GO could not record its "
                    "receipt; the next run rechecks again",
                    item.issue,
                )
                return
        try:
            ctx.github.upsert_issue_comment(
                item.issue,
                STALE_PLAN_RECHECK_MARKER,
                render_stale_plan_recheck_record(
                    plan_sha256,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        except RuntimeError as exc:
            logger.warning(
                "plan_review:%d: stale-plan recheck receipt write failed: %s; "
                "the next run rechecks again",
                item.issue,
                exc,
            )

    @staticmethod
    def _write_verdict_labels(issue_number: int, ctx: StageContext, *, is_go: bool) -> None:
        """Write a verdict label.

        The caller must re-read and confirm an exclusive state before using
        this write to route the item.

        Args:
            issue_number: GitHub issue number.
            ctx: Stage context carrying the GitHub accessor.
            is_go: True for a GO verdict, False for NOGO-exhausted.

        """
        label_to_add, labels_to_remove = apply_plan_verdict(is_go=is_go)
        ctx.github.edit_labels(
            issue_number,
            add=[label_to_add],
            remove=labels_to_remove,
        )

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Store job results on the item payload (state is still the WAIT state).

        Args:
            item: The work item to update.
            result: The job result from the worker pool.
            ctx: Stage context.

        """
        if _checkpoint_review_identity(item, result, ctx):
            return
        if not result.ok:
            reason = _safe_agent_failure_reason(result.error)
            cause = bounded_pipeline_diagnostic(result.error or "", limit=500)
            if reason == "agent job failed" and cause:
                detail = cause
            elif cause and cause != reason:
                detail = f"{reason} ({cause})"
            else:
                detail = reason
            diagnostic = _safe_agent_failure_diagnostic(result.stderr_tail)
            if diagnostic:
                logger.error(
                    "plan_review:%s: agent failed due to %s; diagnostic: %s; retrying review loop",
                    item.issue,
                    detail,
                    diagnostic,
                )
            else:
                logger.error(
                    "plan_review:%s: agent failed due to %s; retrying review loop",
                    item.issue,
                    detail,
                )
            return

        if result.value is not None:
            self._record_job_value(item, result.value, ctx)

    def _record_job_value(self, item: WorkItem, value: object, ctx: StageContext) -> None:
        """Route one successful job value to its WAIT-state recorder."""
        if item.state == "REVIEW_WAIT":
            _record_review_value(item, value, ctx)
        elif item.state == "AMEND_WAIT" and isinstance(value, str):
            _record_amendment_value(item, value, ctx)
        elif item.state == DECOMP_GEN_WAIT and isinstance(value, str):
            item.payload[_DECOMP_OUTPUT_KEY] = value
        elif item.state == DECOMP_REVIEW_WAIT and isinstance(value, str):
            item.payload[_DECOMP_REVIEW_OUTPUT_KEY] = value
        elif item.state == DECOMP_CHILDREN_WAIT and isinstance(value, DecompositionChildrenCreated):
            item.payload[_DECOMP_CHILDREN_JOB_KEY] = value
        elif item.state == DECOMP_PARENT_WAIT and isinstance(
            value, DecompositionParentTransitioned
        ):
            item.payload[_DECOMP_PARENT_JOB_KEY] = value
        elif item.state == DECOMP_LABELS_WAIT and isinstance(
            value, DecompositionChildLabelsApplied
        ):
            item.payload[_DECOMP_LABELS_JOB_KEY] = value
        elif item.state == FEEDBACK_WAIT and isinstance(value, str):
            _record_feedback_value(item, value, ctx)
