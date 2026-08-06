"""Base protocol and step-result types for pipeline stages.

This module defines the :class:`Stage` protocol that all pipeline stages
implement, the step-result types (:class:`Continue` / :class:`JobRequest` /
re-exported :class:`StageOutcome`), and :class:`StageContext`, the bundle of
coordinator-owned accessors injected into every stage call.

Core types come from their source modules (epic #1809):
``StageOutcome``/``Disposition``/``StageName`` from :mod:`..routing`,
``WorkItem``/``ItemKind`` from :mod:`..work_item`, and
``AgentJob``/``JobResult``/``JobHandle`` from :mod:`..jobs`. They are
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
  produced is journaled BEFORE the verify/ADVANCE decision.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from hephaestus.agents.runtime import DEFAULT_AGENT, agent_supports_model_reasoning_effort
from hephaestus.automation.review_journal import IssueComment, PlanDiscoveryResult
from hephaestus.automation.state_labels import (
    SKIP_REASON_MARKER,
    STATE_SKIP,
    format_skip_reason_comment,
)

from ..events import StageEvent
from ..github_jobs import GitHubJob
from ..jobs import AgentJob, BuildTestJob, CompactJob, GitJob, JobHandle, JobResult
from ..routing import ROUTES, Disposition, StageName, StageOutcome
from ..work_item import ItemKind, WorkItem

__all__ = [
    "GIT_JOB_TIMEOUT_S",
    "AgentJob",
    "BuildTestJob",
    "CompactJob",
    "ConditionalMergeResult",
    "Continue",
    "Disposition",
    "GitHubJob",
    "GitJob",
    "ImplementationThreadReplyResult",
    "ItemKind",
    "JobHandle",
    "JobRequest",
    "JobResult",
    "ReviewerThreadReconciliationResult",
    "Stage",
    "StageContext",
    "StageEvent",
    "StageGitHub",
    "StageName",
    "StageOutcome",
    "StepResult",
    "WorkItem",
    "agent_provider",
    "stage_model",
    "write_skip_label",
]

logger = logging.getLogger(__name__)

#: Timeout for git worktree/commit/push jobs (mechanical, no agent). Shared
#: by every stage that submits :class:`GitJob`s (single home — stages must
#: not import it from each other).
GIT_JOB_TIMEOUT_S = 600

#: Poll backoff cap in seconds (legacy ``min(2**attempt, 60)`` — shared by
#: every stage that uses the legacy exponential poll delay.


@dataclass(frozen=True)
class ConditionalMergeResult:
    """Outcome of one SHA-conditional normal GitHub merge request.

    ``status`` and ``body`` preserve the server's response for merge-wait to
    classify. ``transport_error`` means the server outcome is unknown, while
    ``malformed`` means the response was received but could not be safely
    interpreted. The adapter never retries this mutation itself.
    """

    status: int | None
    body: dict[str, Any] | None
    transport_error: bool = False
    malformed: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ImplementationThreadReplyResult:
    """Outcome of posting head-gated implementation replies to review threads.

    ``receipts`` are complete, host-read thread snapshots after the implementation
    reply is visible.  They are the only input a later reviewer-validation
    mutation may consume; model output never directly identifies a mutable
    GitHub object. ``retryable_thread_ids`` identifies only replies whose host
    outcome is transport/read-ambiguous. An ordinary ``blocked_thread_ids``
    result means the saved snapshot is stale and must return through a fresh
    reviewer pass rather than replay. ``visibility_lag`` distinguishes a
    second GitHub thread read that has not yet observed the just-pushed head;
    the caller consumes its separate bounded backoff rather than transport
    retries. Implementation replies are submitted through one review-level
    envelope so every response from a pass remains batched.
    """

    replied_thread_ids: tuple[str, ...] = ()
    blocked_thread_ids: tuple[str, ...] = ()
    receipts: tuple[dict[str, Any], ...] = ()
    retryable_thread_ids: tuple[str, ...] = ()
    retryable: bool = False
    visibility_lag: bool = False


@dataclass(frozen=True)
class ReviewerThreadReconciliationResult:
    """Outcome of a reviewer performing fresh thread reconciliation."""

    resolved_thread_ids: tuple[str, ...] = ()
    feedback_thread_ids: tuple[str, ...] = ()
    blocked_thread_ids: tuple[str, ...] = ()


@runtime_checkable
class StageGitHub(Protocol):
    """Single-owner GitHub accessor.

    ``StageContext.github`` is coordinator-thread-only and must never cross a
    worker boundary. ``GitHubJob`` workers create a fresh structurally
    conforming accessor per job and serialize same-repository operations.

    The single seam through which stages read GitHub facts and request
    durable mutations. Dry-run is honored INSIDE the accessor implementation
    (#1817): when the coordinator runs with ``--dry-run``, the mutator
    methods below log-and-skip the underlying ``gh`` calls, so stages never
    branch on ``ctx.dry_run`` around a write.

    Read surface mirrors the existing helper names; the mutator surface uses
    coordinator-neutral names the coordinator maps onto ``github_api``
    mutators (the pipeline architecture guard forbids ``github_api`` mutator
    names inside pipeline modules).
    """

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (mirrors ``github_api.issues.gh_issue_json``)."""
        ...

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Return the merged PR closing this issue, if any (``_review_utils``)."""
        ...

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the merged PR for this issue, if any (tri-state seeding lookup)."""
        ...

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return an open PR covering this issue, if any (``_review_utils``)."""
        ...

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        """Return the linked issue for this PR, if its body has ``Closes #N``."""
        pass

    def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
        """Return title/body/head metadata; checkout later derives the review diff."""
        ...

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Return the tri-state actor-owned plan-discovery outcome."""
        ...

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return issue comments with ownership metadata in creation order."""
        pass

    def ensure_blocked_audit(self, issue_number: int) -> None:
        """Repair a missing canonical BLOCKED explanation without changing labels."""
        pass

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (coordinator maps to ``gh_issue_add_labels``)."""
        ...

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (coordinator maps to ``gh_issue_remove_labels``)."""
        ...

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Durably add and remove labels in ONE atomic ``gh issue edit`` call.

        The single-transition primitive the skill mandates (one HTTP call so
        the issue never has zero or two state labels mid-window). The
        coordinator maps this onto a single
        ``gh issue edit --add-label ... --remove-label ...``. Prefer this over
        paired :meth:`add_labels`/:meth:`remove_labels` for any state:* swap.
        """
        ...

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        ...

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Upsert an automation-owned canonical comment keyed on ``marker``."""
        pass

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append one immutable, replay-safe automation-owned journal comment."""
        pass

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the actor-owned plan comment using its opaque canonical marker.

        Durable plan-comment channel (doc section 2: "plan comment = durable
        artifact"). The coordinator maps this onto
        The human-readable heading remains for display and legacy migration,
        but only a comment proved to be authored by the authenticated actor is
        mutable. Callers pass a body normalized with the opaque marker.
        """
        ...

    # -- implementation / pr_review surface (#1815) ------------------------

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Return the PR's head branch name (``_review_utils.get_pr_head_branch``)."""
        ...

    def pr_head_is_writable(self, pr_number: int) -> bool:
        """Return whether this loop can safely publish to the PR head branch.

        A fork head may be fetched for read-only review, but it must never be
        addressed by pushing a same-named branch to the base repository.
        """
        pass

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return ``(has_go, has_no_go)`` for the PR's implementation state labels.

        Mirrors ``pr_manager.pr_has_implementation_state_label`` — the
        existing-PR fast-path read the implementation GATE uses.
        """
        ...

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Return complete fresh snapshots of every unresolved review thread."""
        pass

    def post_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str,
    ) -> ImplementationThreadReplyResult:
        """Post host-validated implementation replies after a successful push.

        The implementation agent supplies only prose keyed by IDs from the
        host-provided snapshot.  This accessor verifies the PR head and exact
        live thread state before and after every reply, but never resolves a
        thread.
        """
        ...

    def reviewer_validation_receipts(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        threads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return current, host-verifiable implementation reply receipts.

        Receipts are derived afresh from the complete live-thread snapshot and
        are bound to ``reviewed_head_sha``.  They are deliberately not backed
        by process-local work-item state, so a restarted loop can validate an
        implementation reply created by an earlier work item.
        """
        ...

    def reconcile_reviewer_validated_threads(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        receipts: list[dict[str, Any]],
        resolved_thread_ids: set[str],
        feedback: dict[str, str],
    ) -> ReviewerThreadReconciliationResult:
        """Resolve reviewer-validated threads or reply with remaining defects.

        A fresh read-only reviewer supplies the disposition.  The adapter
        revalidates the complete implementation-reply receipt immediately
        before mutation, resolves only accepted IDs, and leaves feedback IDs
        open after posting the reviewer's explanation.
        """
        ...

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        Backing (#1817): ``_review_utils.find_pr_for_issue`` first (reuse an
        existing open PR — the idempotence), then ``github_api.gh_pr_create``
        with the *given* ``title``/``body``. NOT ``pr_manager
        .ensure_pr_created``, which generates its own PR body and would
        discard the ``get_pr_description`` body the stage composed. PR
        creation is the implementation stage's journal entry (doc section 4:
        "Owned labels: PR creation is the journal entry").
        """
        ...

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Durably apply ``state:implementation-no-go`` to the PR.

        Mirrors ``pr_manager.mark_pr_implementation_no_go`` (adds the no-go
        label, removes any stale go label). Doc section 5 owned label:
        written on every non-authorizing review round before retry/regress.
        """
        ...

    def post_review_threads(
        self,
        pr_number: int,
        threads: list[dict[str, Any]],
        *,
        expected_head_sha: str,
        review_diff: str | None = None,
    ) -> list[dict[str, Any]]:
        """Post one source-anchored batch for the immutable reviewed snapshot.

        ``expected_head_sha`` identifies the exact detached checkout the
        reviewer inspected.  A later PR push does not invalidate that review:
        GitHub accepts a review bound to the older commit and marks affected
        comments outdated when appropriate.  Eligibility for the GO label is
        checked separately against the current head.
        """
        ...

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Durably apply ``state:implementation-go`` to the PR.

        The PR review stage requests this only after complete structural audit
        facts, live thread facts, and an exact reviewed open/unarmed head pass
        its mutation guard. A post-write readback must still prove the same
        head and exclusive label. Audit prose, grades, CI, and secondary
        artifacts are informational rather than label authority.
        """
        ...

    # -- merge_wait surface (#1816) ------------------------------------------

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Read shared PR state for seed, stage, and merge decisions.

        Returns a PR lifecycle record (including ``state``, ``headRefOid``,
        ``mergedAt``, ``autoMergeRequest``, and ``baseRefName``), or ``None``
        on a transient read failure. The repo seed
        path and the implementation stage boundary use this read for
        merged/closed terminal-state checks before branch adoption or further
        routing. The merge_wait path uses the same contract to capture the
        head OID and classify merged, closed, and open lifecycle states.
        """
        ...

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, Any] | None:
        """Read operational normal-merge readiness without granting authorization."""
        pass

    def base_branch_requires_conversation_resolution(
        self, pr_number: int, base_branch: str
    ) -> bool:
        """Return whether this PR base branch has server-enforced conversation resolution.

        The read is scoped to the accessor's explicit repository and the exact
        base branch admitted for ``pr_number``. Admission requires enforced
        conversation resolution and administrator enforcement, with no
        explicit PR-bypass allowances. ``False`` includes an absent,
        unreadable, or malformed branch-protection response and must prevent a
        normal merge request.
        """
        ...

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        """Perform one immediate normal merge conditional on ``reviewed_sha``."""
        pass

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Return True when the post-merge ``/learn`` is already terminal.

        An arming record whose ``learn_captured_at``/``learn_succeeded_at`` is
        set, or whose ``learn_status`` is ``succeeded``/``failed``, must never
        fire ``/learn`` again — the merge_wait MERGED path dedupes on this
        read (doc section 7: "Post-merge learn (deduped via arming_state)").
        """
        ...

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Return whether a durable post-merge ``/learn`` claim is in flight.

        An ``in_progress`` claim is deliberately distinct from a terminal
        outcome. It is written and read back before the agent starts. If a
        process dies after that boundary, a later process must not replay the
        externally visible ``/learn`` operation.
        """
        pass

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Durably claim one post-merge ``/learn`` dispatch.

        Returns ``True`` only after an ``in_progress`` record for this issue
        and PR has been persisted and read back. ``False`` means a terminal
        or previously in-flight claim already owns the dispatch. Raises when
        persistence cannot be acknowledged, so the caller fails closed before
        the agent can perform an external learning action.
        """
        pass

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Durably record the post-merge ``/learn`` outcome on the arming record.

        Mirrors ``post_merge_processor.mark_drive_green_learn_result``:
        written as soon as the learn job completes (success or failure alike)
        and BEFORE the FINISH_PASS outcome. The preceding durable in-flight
        claim prevents a restart from replaying ``/learn`` if this final
        outcome write fails.
        """
        ...

    # -- repo-stage surface (#1817) -----------------------------------------

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Durably tag epics ``state:skip`` (the ONE sanctioned seeding write).

        The coordinator (#1817) maps this onto the existing
        ``github_api.skip_epics`` chokepoint; the repo stage calls it BEFORE
        excluding epics (doc row "Epic tagging is the one seeding write; done
        BEFORE excluding" — see :mod:`..seeding`'s write-path boundary note).
        """
        ...

    def ensure_state_labels(self) -> None:
        """Durably ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1 ``ensure_state_labels``): the
        coordinator maps this onto ``github_api._ensure_labels_exist`` over
        the full ``state_labels`` vocabulary. Idempotent by construction.
        """
        ...


@dataclass(frozen=True)
class Continue:
    """Advance to the next state without requesting a job."""

    next_state: str


@dataclass(frozen=True)
class JobRequest:
    """Request a job be submitted to the worker pool.

    Attributes:
        job: The frozen job spec to submit (agent, build/test, git, GitHub, or
            session compaction — the same union :class:`~..jobs.JobHandle` carries).
        on_done_state: The state the coordinator moves the item to after the
            job completes and ``on_job_done`` has run.

    """

    job: AgentJob | BuildTestJob | GitJob | GitHubJob | CompactJob
    on_done_state: str


type StepResult = "Continue | JobRequest | StageOutcome"
type BranchWorktreeOwnerStatus = Literal["verified", "pending", "unverified"]


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

    config: Any  # PlannerOptions-like (enable_advise, enable_learn, force, agent, dry_run)
    org: str
    dry_run: bool
    github: StageGitHub  # coordinator-owned GitHub accessor (label/comment/PR writes+reads)
    paths: Any  # coordinator-owned path accessor (repo_root, worktree)
    now_fn: Callable[[], float] | None = None  # injectable clock (tests pass a fake)
    budget_fn: Callable[[str], int] | None = None  # injected overrides; falls back to ROUTES
    event_fn: Callable[[StageEvent], None] | None = None
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


def agent_provider(ctx: StageContext) -> str:
    """Return the selected agent backend provider for an agent job."""
    return str(getattr(ctx.config, "agent", "") or DEFAULT_AGENT)


def stage_model(
    ctx: StageContext,
    phase: str,
    fallback: Callable[[], str],
    *,
    provider: str | None = None,
) -> str:
    """Return a phase model override, the catch-all model, or the legacy fallback."""
    phase_value = getattr(ctx.config, f"{phase}_model", "")
    catch_all = getattr(ctx.config, "model", "")
    model = str(phase_value or catch_all or fallback())
    reasoning_effort = str(getattr(ctx.config, f"{phase}_reasoning_effort", "") or "")
    if reasoning_effort and agent_supports_model_reasoning_effort(provider or agent_provider(ctx)):
        base_model, separator, current_effort = model.rpartition(":")
        if separator and current_effort in {"default", "low", "medium", "high", "xhigh"}:
            model = base_model
        return f"{model}:{reasoning_effort}"
    return model


def _issue_labels(item: WorkItem, ctx: StageContext) -> list[str]:
    """Refresh the item's labels from GitHub and update ``labels_cache``.

    Reads through ``ctx.github.gh_issue_json`` (mirrors
    ``github_api.issues.gh_issue_json``); on any read failure the cached
    labels are used so a transient API blip cannot mis-route the item.
    Shared by every stage that gates on labels (single home — stages must
    not import it from each other).
    """
    if item.issue is None:
        return []
    try:
        data = ctx.github.gh_issue_json(item.issue)
    except Exception as e:  # transient gh failure: fall back to cache
        logger.warning("pipeline:%d: label refresh failed (using cache): %s", item.issue, e)
        return list(item.labels_cache)
    raw = data.get("labels", []) if isinstance(data, dict) else []
    labels = [entry["name"] if isinstance(entry, dict) else str(entry) for entry in raw]
    item.labels_cache = dict.fromkeys(labels, True)
    return labels


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


def _require_item_worktree(item: WorkItem, stage_name: str, action: str) -> StageOutcome | None:
    """Return a fail-back outcome when a mutating action lacks a worktree."""
    if item.worktree:
        return None
    logger.warning(
        "%s:%s: %s requires an item worktree; failing back to implementation",
        stage_name,
        item.issue if item.issue is not None else item.pr,
        action,
    )
    return StageOutcome(Disposition.FAIL_BACK, "missing_worktree")


def _build_rebase_job(item: WorkItem, ctx: StageContext, *, descr: str) -> GitJob:
    """Build the mechanical rebase-onto-base GitJob (shared base-ref capture).

    ``merge_wait`` uses this shared worker operation when a dirty-worktree
    resolution needs to rebase the item's worktree onto the captured
    ``item.payload["base_branch"]`` (defaulting to ``main``) via the same
    worker ``op="rebase"`` (``git_utils.rebase_worktree_onto``) — single home
    so all remaining consumers use one mechanic (#1861).
    """
    return GitJob(
        repo=item.repo,
        op="rebase",
        timeout_s=GIT_JOB_TIMEOUT_S,
        kwargs={
            "cwd": _worktree_path(item, ctx),
            "base_branch": str(item.payload.get("base_branch") or "main"),
        },
        descr=descr,
    )


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

    Single home for the exhaustion/no-commits skip write (previously
    duplicated across the implementation and pr_review stages). The label is
    the durable state authority; the reason belongs in structured run logs so
    automation does not add a third canonical comment to the linked issue.

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
