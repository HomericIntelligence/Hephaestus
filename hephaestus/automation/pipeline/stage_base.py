"""Base protocol and step-result types for pipeline stages.

This module defines the :class:`Stage` protocol that all pipeline stages
implement, the step-result types (:class:`Continue` / :class:`JobRequest` /
re-exported :class:`StageOutcome`), and :class:`StageContext`, the bundle of
coordinator-owned accessors injected into every stage call. The
:class:`StageGitHub` protocol for the GitHub accessor lives in
:mod:`.stage_github`.

Core types come from their source modules (epic #1809):
``StageOutcome``/``Disposition``/``StageName`` from :mod:`.routing`,
``WorkItem``/``ItemKind`` from :mod:`.work_item`, and
``AgentJob``/``JobResult``/``JobHandle`` from :mod:`.jobs`. They are
re-exported here so stage modules and their tests have a single import
surface for the stage contract.

Coordinator convention (binding for #1817, the coordinator slice):

- ``on_enter`` runs once when an item enters the stage. It must be
  idempotent, and its label checks are ordered at-or-past checks (never
  equality), so re-entry after a restart fast-forwards instead of redoing
  work. It returns ``None`` to proceed or a ``StageOutcome`` to route away.
- ``step`` is invoked for the item's *current* ``state``. Returning
  ``Continue`` advances ``item.state`` and steps again; returning
  ``JobRequest`` submits the job while ``item.state`` stays at the
  submitting WAIT state; returning ``StageOutcome`` routes via ROUTES.
- When a requested job completes and was NOT interrupted, the coordinator
  calls ``on_job_done`` (``item.state`` still the WAIT state that submitted
  the job), then sets ``item.state = on_done_state`` and steps again.
  ``on_job_done`` is never called for interrupted results — interrupts
  leave items resumable, never failed.
- Timer-park (RETRY delay) contract: ``StageOutcome`` has NO delay field,
  so a stage that returns ``StageOutcome(Disposition.RETRY, ...)`` for a
  non-blocking poll records the backoff delay in
  ``item.payload["retry_delay_s"]`` immediately before returning. The
  coordinator (#1817) consumes ``payload["retry_delay_s"]`` to park the
  item on its timer heap and re-steps it after that many seconds (a
  missing key means "retry on the next drain tick"). Stages NEVER sleep —
  the heap owns every wait.
- All durable GitHub mutations go through ``ctx.github`` and happen
  immediately BEFORE the outcome that causes a queue push ("durable write
  precedes the queue push").
- ``ctx.github`` implements the :class:`StageGitHub` protocol. Its mutator
  surface (``add_labels`` / ``remove_labels`` / ``close_issue_as_covered`` /
  ``upsert_plan_comment``) uses coordinator-neutral names the coordinator
  (#1817) maps onto the ``github_api`` mutators; ``upsert_plan_comment`` is
  the durable plan-comment channel (doc section 2: "plan comment = durable
  artifact") — the planning stage calls it in VERIFY so the plan the agent
  produced is journaled BEFORE the verify/ADVANCE decision. Stage code must
  not call the native auto-merge mutations (``enable_auto_merge`` /
  ``disable_auto_merge``); the stage-module scan
  ``tests/unit/automation/pipeline/test_pipeline_architecture.py::test_pipeline_stages_have_no_auto_merge_mutation_capability``
  enforces that ban, and the closed merge-wait job plus the adapter's own
  no-go write own every auto-merge call site.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from hephaestus.agents.model_selection import normalize_model_reference
from hephaestus.agents.runtime import (
    DEFAULT_AGENT,
    agent_uses_configured_model_default,
)
from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.operation_deadlines import operation_deadline_after
from hephaestus.automation.source_worktree import _PreparationDeadline
from hephaestus.automation.state_labels import STATE_SKIP

from .athena_skill_jobs import AthenaSkillJob, AthenaSkillRequest, AthenaSkillResult
from .events import StageEvent
from .github_jobs import GitHubJob
from .jobs import AgentJob, BuildTestJob, CompactJob, GitJob, JobHandle, JobResult
from .routing import ROUTES, Disposition, StageName, StageOutcome
from .stage_results import Continue, JobRequest
from .work_item import ItemKind, WorkItem

if TYPE_CHECKING:
    from .coordinator_types import PipelineConfig
    from .stage_github import StageGitHub

__all__ = [
    "GIT_JOB_TIMEOUT_S",
    "SOURCE_WORKSPACE_PREPARATION_TIMEOUT_S",
    "AgentJob",
    "AthenaSkillJob",
    "AthenaSkillRequest",
    "AthenaSkillResult",
    "BuildTestJob",
    "CompactJob",
    "Continue",
    "Disposition",
    "GitHubJob",
    "GitJob",
    "ItemKind",
    "JobHandle",
    "JobRequest",
    "JobResult",
    "Stage",
    "StageContext",
    "StageEvent",
    "StageName",
    "StageOutcome",
    "StepResult",
    "WorkItem",
    "agent_provider",
    "athena_advise_failure_reason",
    "planning_source_revision",
    "planning_source_workspace_binding",
    "records_retry_exceeded_cap",
    "source_workspace_binding",
    "stage_model",
    "stage_network_deadline",
    "stage_timeout",
    "write_skip_label",
]

logger = logging.getLogger(__name__)

_ATHENA_ADVISE_FAILURE_KINDS = frozenset(
    {
        "mnemosyne_binding",
        "remote_git_authentication",
        "remote_git_identity",
        "remote_git_transport",
    }
)


def athena_advise_failure_reason(item: WorkItem) -> str:
    """Return a terminal reason with an allowed host failure class."""
    error = item.payload.get("athena_advise_error")
    if isinstance(error, str):
        failure_kind = error.partition(":")[0].strip()
        if failure_kind in _ATHENA_ADVISE_FAILURE_KINDS:
            return f"athena_advise_failed:{failure_kind}"
    return "athena_advise_failed"


#: Timeout for git worktree/commit/push jobs (mechanical, no agent). Shared
#: by every stage that submits :class:`GitJob`s (single home — stages must
#: not import it from each other).
GIT_JOB_TIMEOUT_S = 600

# Keep source preparation below the coordinator's 60-second stage watchdog.
SOURCE_WORKSPACE_PREPARATION_TIMEOUT_S = 45.0


type StepResult = "Continue | JobRequest | StageOutcome"
type BranchWorktreeOwnerStatus = Literal["verified", "pending", "unverified"]


def _repo_state_root(ctx: StageContext, repo: str) -> Path:
    """Return the durable state root for a repository context.

    The intake checkout can be removed and rebound when the remote default
    branch advances.  A coordinator context therefore keeps its journals in
    the receipt-owned state area instead of the replaceable worktree.  The
    fallback preserves lightweight stage-context compatibility.
    """
    state_roots = getattr(ctx.config, "repo_state_roots", {})
    return Path(str(state_roots.get(repo, ctx.paths.repo_root)))


@dataclass(frozen=True)
class StageContext:
    """Context passed to every stage call.

    All coordinator-owned accessors (github, paths, clock, budgets) are
    injected here so stages never construct their own I/O helpers. The
    ``github`` accessor is the coordinator's single mutation channel and
    implements the :class:`StageGitHub` protocol: its mutator surface uses
    coordinator-neutral names (``add_labels``, ``remove_labels``,
    ``close_issue_as_covered``, ``upsert_plan_comment``) that the
    coordinator (#1817) maps onto the ``github_api`` mutators, while its
    read surface mirrors the existing helper names (``gh_issue_json``,
    ``find_merged_closing_pr``, ``find_pr_for_issue``,
    ``discover_plan``). Stages never import ``github_api`` directly —
    enforced by ``tests/unit/automation/pipeline/test_pipeline_architecture``.
    """

    config: PipelineConfig
    org: str
    dry_run: bool
    github: StageGitHub  # coordinator-owned GitHub accessor (label/comment/PR writes+reads)
    paths: Any  # coordinator-owned path accessor (repo_root, worktree)
    now_fn: Callable[[], float] | None = None  # injectable clock (tests pass a fake)
    budget_fn: Callable[[str], int] | None = None  # injected overrides; falls back to ROUTES
    event_fn: Callable[[StageEvent], None] | None = None
    learning_journal: Any = None
    plan_review_sessions: Any = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    # Per-Coordinator one-shot consumption state for plan-review session
    # resets. The coordinator copies this from the immutable
    # ``PipelineConfig.reset_plan_review_sessions`` frozenset so stages can
    # consume entries without mutating caller-owned configuration (POLA).
    plan_review_session_resets: set[int] = field(default_factory=set)
    # Coordinator-owned set of issue identities generated in the current run
    # (issue #2982). The coordinator shares one set object through every
    # StageContext of one process run and resets it when the process starts.
    # It is only a same-run recursion guard; durable GitHub records remain
    # the restart authority.
    generated_issue_ids: set[int] = field(default_factory=set)
    # A worktree-holder result is only a diagnostic fact from Git.  The
    # coordinator proves that it belongs to a live pipeline sibling before
    # implementation can treat a collision as redundant work.  Leaving this
    # unset intentionally fails closed in isolated stage tests and alternate
    # hosts.
    branch_worktree_owner_status: (
        Callable[[WorkItem, str, str], BranchWorktreeOwnerStatus] | None
    ) = None

    def now(self) -> float:
        """Return the injected stage clock value (monotonic in the coordinator)."""
        if self.now_fn is not None:
            return self.now_fn()
        return time.time()

    def budget(self, name: str) -> int:
        """Look up the budget for a given counter name from the routing tables."""
        if self.budget_fn is not None:
            return self.budget_fn(name)
        for route in ROUTES.values():
            if name in route.budgets:
                return route.budgets[name]
        return 1  # conservative default for unknown keys

    def emit_event(self, event: StageEvent) -> None:
        """Emit a runtime-validated stage event when coordinator wiring exists."""
        if self.event_fn is not None:
            self.event_fn(event)


def agent_provider(ctx: StageContext, role: str = "") -> str:
    """Return the role tool, global tool, or default tool."""
    return getattr(ctx.config, f"{role}_agent", "") or ctx.config.agent or DEFAULT_AGENT


def stage_model(
    ctx: StageContext,
    phase: str,
    fallback: Callable[[], str],
    *,
    provider: str | None = None,
) -> str:
    """Return the normalized compact selection for one pipeline phase."""
    selected_provider = provider or agent_provider(ctx, phase)
    phase_value = getattr(ctx.config, f"{phase}_model", "")
    catch_all = getattr(ctx.config, "model", "")
    configured_value = phase_value or catch_all
    configured_model = str(
        configured_value
        or ("" if agent_uses_configured_model_default(selected_provider) else fallback())
    )
    return normalize_model_reference(configured_model)


def stage_timeout(
    ctx: StageContext,
    phase: str,
    fallback: Callable[[], int] | int,
) -> int:
    """Return a positive typed phase timeout or the legacy fixed default."""
    value = getattr(ctx.config, f"{phase}_timeout", None)
    if value is None:
        value = fallback() if callable(fallback) else fallback
    timeout = int(value)
    if timeout <= 0:
        raise ValueError(f"{phase}_timeout must be positive")
    return timeout


def stage_network_deadline(ctx: StageContext) -> float:
    """Return the absolute deadline for one bounded stage network operation."""
    return operation_deadline_after(stage_timeout(ctx, "network", GIT_JOB_TIMEOUT_S))


def records_retry_exceeded_cap(
    payload: MutableMapping[str, object],
    key: str,
    cap: int,
) -> bool:
    """Increment a payload retry counter and return whether it passed the cap.

    Args:
        payload: The stage payload that holds the counter.
        key: Counter key. A missing key starts at zero.
        cap: Maximum retryable count.

    Returns:
        ``True`` when the new count is greater than ``cap``.

    Raises:
        TypeError: If the stored counter is not an integer. The helper does not
            coerce a malformed value.

    """
    stored = payload.get(key, 0)
    if type(stored) is not int:
        raise TypeError(f"{key} retry counter must be an integer")
    retries = stored + 1
    payload[key] = retries
    return retries > cap


def source_workspace_binding(
    item: WorkItem,
    ctx: StageContext,
    lane: SourceLane,
    *,
    revision: str | None = None,
    branch: str | None = None,
    preparation_timeout_s: float | None = None,
) -> WorkspaceBinding:
    """Prepare the required source lane within a bounded operation."""
    manager = ctx.paths.source_workspaces
    if callable(manager):
        manager = manager()
        ctx.paths.source_workspaces = manager
    if manager is None or not callable(getattr(manager, "prepare_bounded", None)):
        raise RuntimeError("source workspace manager is required")
    item_number = item.issue or item.pr
    if item_number is None:
        raise RuntimeError("source workspace requires an issue or pull request number")
    target = (
        revision
        if revision is not None
        else str(
            item.payload.get("_worktree_cleanup_head_sha")
            or item.payload.get("_impl_source_revision")
            or item.payload.get("reviewed_pr_head_sha")
            or item.payload.get("pr_head_sha")
            or item.payload.get("_synced_default_branch_sha")
            or item.payload.get("_direct_scope_base_sha")
            or ""
        )
    )
    if len(target) != 40:
        raise RuntimeError("source workspace requires a captured full revision")
    timeout = (
        SOURCE_WORKSPACE_PREPARATION_TIMEOUT_S
        if preparation_timeout_s is None
        else preparation_timeout_s
    )
    if timeout <= 0:
        raise ValueError("preparation_timeout_s must be positive")
    clock = ctx.now_fn or time.monotonic
    deadline = _PreparationDeadline(
        expires_at=clock() + timeout,
        monotonic=clock,
        shutdown=ctx.cancellation,
    )
    binding = manager.prepare_bounded(
        item_number,
        lane,
        target,
        branch=branch,
        deadline=deadline,
    )
    if lane is SourceLane.IMPLEMENTATION:
        item.payload["_impl_source_revision"] = binding.revision
        item.payload["_impl_source_workspace"] = binding.to_dict()
    return cast(WorkspaceBinding, binding)


def planning_source_revision(item: WorkItem) -> str:
    """Return the captured default-branch revision for planning evidence.

    Planning uses the captured default-branch revision. It does not use
    implementation, cleanup, or pull-request revisions because those values
    can refer to a preserved writer workspace or a stale source.
    """
    synced_revision = item.payload.get("_synced_default_branch_sha")
    if synced_revision is None:
        synced_revision = item.payload.get("_direct_scope_base_sha")
    if (
        not isinstance(synced_revision, str)
        or len(synced_revision) != 40
        or any(char not in "0123456789abcdef" for char in synced_revision)
    ):
        raise RuntimeError("planning source requires a captured full revision")
    return synced_revision


def planning_source_workspace_binding(
    item: WorkItem,
    ctx: StageContext,
    *,
    preparation_timeout_s: float | None = None,
) -> WorkspaceBinding | None:
    """Prepare the detached review lane for the captured planning source.

    A dry run returns ``None`` and prepares nothing. The coordinator logs the
    previewed job and does not submit it, so a dry run must not synchronize a
    checkout, create a source worktree, or require revision evidence that only
    a suppressed synchronization job can produce. A normal run still requires
    the captured full revision.
    """
    if ctx.dry_run:
        return None
    return source_workspace_binding(
        item,
        ctx,
        SourceLane.REVIEW,
        revision=planning_source_revision(item),
        branch=None,
        preparation_timeout_s=preparation_timeout_s,
    )


def _require_issue_labels(item: WorkItem, ctx: StageContext) -> list[str]:
    """Return a fresh label read or propagate failure for authoritative gates.

    Cached labels are useful diagnostic context but cannot authorize a stage
    transition. Planning, plan-review, and implementation gates use this strict
    variant so a stale cache can never advance work after a GitHub read failure.
    """
    if item.issue is None:
        return []
    data = ctx.github.gh_issue_json(item.issue)
    raw = data.get("labels", []) if isinstance(data, dict) else []
    labels = [entry["name"] if isinstance(entry, dict) else str(entry) for entry in raw]
    item.labels_cache = dict.fromkeys(labels, True)
    return labels


def _worktree_path(item: WorkItem, ctx: StageContext) -> Path:
    """Return the item's worktree as a Path, falling back to the shared one.

    The shared-checkout fallback is only safe for READ-mostly agent jobs
    (advise, review) that run before a worktree exists; stages that edit or
    push code MUST guard against dispatching into the shared checkout on the
    wrong branch (see ``PrReviewStage._address``).
    """
    if item.worktree:
        return Path(item.worktree)
    return Path(str(ctx.paths.worktree))


def _terminal_pr_outcome(pr_state: dict[str, Any] | None, pr_number: int) -> StageOutcome | None:
    """Return a terminal outcome for PRs already merged/closed, if known."""
    if not pr_state:
        return None
    state = str(pr_state.get("state") or "").upper()
    if state == "MERGED" or pr_state.get("mergedAt"):
        logger.info("PR #%d is already merged; terminalizing", pr_number)
        return StageOutcome(Disposition.FINISH_PASS, "merged")
    if state == "CLOSED":
        logger.info("PR #%d is already closed; terminalizing", pr_number)
        return StageOutcome(Disposition.FINISH_FAIL, "closed")
    return None


def _is_confirmed_open_unarmed(pr_state: dict[str, Any] | None) -> bool:
    """Return whether a complete live PR record proves it is open and unarmed.

    A missing ``autoMergeRequest`` field is not equivalent to ``null``. The
    pipeline must not mutate labels after a partial GitHub response because a
    concurrent actor could own an omitted auto-merge request. GitHub's
    lifecycle value is likewise intentionally exact: the CLI contract
    supplies ``OPEN`` for a writable open pull request.
    """
    return bool(
        isinstance(pr_state, dict)
        and pr_state.get("state") == "OPEN"
        and "autoMergeRequest" in pr_state
        and pr_state["autoMergeRequest"] is None
    )


def write_skip_label(issue_number: int, ctx: StageContext, reason: str) -> None:
    """Durably apply ``state:skip`` and log its reason, non-fatally.

    Single home for the exhaustion skip write (previously duplicated across
    the implementation and pr_review stages). The label is the durable state
    authority; the reason belongs in structured run logs so automation does not
    add a third canonical comment to the linked issue. No-commit implementation
    runs use their own human-intervention comment and latch.

    Args:
        issue_number: GitHub issue number.
        ctx: Stage context carrying the GitHub accessor.
        reason: Human-readable explanation included in the run log.

    """
    try:
        ctx.github.add_labels(issue_number, [STATE_SKIP])
    except Exception as e:
        logger.warning(
            "pipeline:%d: failed to add label %r (non-fatal): %s",
            issue_number,
            STATE_SKIP,
            e,
        )
    logger.warning("pipeline:%d: applied %s: %s", issue_number, STATE_SKIP, reason)


@runtime_checkable
class Stage(Protocol):
    """Protocol for pipeline stage implementations.

    A stage processes work items through a small in-memory state machine
    (states are stage-local strings, never GitHub labels):

    1. ``on_enter``: refresh item state, perform idempotent fast-forward
       checks (ordered at-or-past label checks, never equality), and ensure
       required entry labels durably. Return ``None`` to proceed or a
       ``StageOutcome`` to skip/finish.
    2. ``step``: take the next action for the current state (``Continue`` to
       advance state, ``JobRequest`` to submit work, or ``StageOutcome`` to
       route). Every durable mutation happens immediately before the return.
    3. ``on_job_done``: handle the result of a completed job (never called
       for interrupted results), storing parsed values on ``item.payload``.
    """

    def on_cancelled_before_start(self, item: WorkItem, ctx: StageContext) -> None:
        """Release stage-owned claims when execution did not start."""
        return None

    def on_enter(self, item: WorkItem, ctx: StageContext) -> StageOutcome | None:
        """Refresh labels and perform idempotent fast-forward checks on entry.

        Must be safe to call repeatedly (restart = re-run): label checks are
        ordered at-or-past checks, and any entry-label write is guarded by a
        presence check so re-entry produces no duplicate mutations.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            None to proceed with step(), or a StageOutcome to skip/finish.

        Note:
            Implementations MAY mutate ``item.state`` as a side effect during
            ``on_enter`` to fast-forward past already-completed work (e.g. an
            existing plan comment jumps ``item.state`` to VERIFY so a restart
            never redoes finished sub-steps). This side effect is *in addition*
            to the return value; a ``None`` return does not imply ``item.state``
            is unchanged. Callers MUST re-read ``item.state`` after ``on_enter``
            to observe any fast-forward.

        """
        ...

    def step(self, item: WorkItem, ctx: StageContext) -> StepResult:
        """Execute the next action for the item's current state.

        Args:
            item: The work item being processed.
            ctx: The stage context.

        Returns:
            A Continue (advance state), JobRequest (submit work while this
            state waits), or StageOutcome (route via ROUTES). All durable
            mutations happen immediately before the return.

        """
        ...

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: StageContext) -> None:
        """Handle completion of a job (never called for interrupted results).

        Called with ``item.state`` still at the WAIT state that submitted the
        job. Store parsed results on ``item.payload``; the coordinator then
        advances ``item.state`` to the JobRequest's ``on_done_state``.

        Args:
            item: The work item being processed.
            result: The job result from the worker pool.
            ctx: The stage context.

        """
        ...
