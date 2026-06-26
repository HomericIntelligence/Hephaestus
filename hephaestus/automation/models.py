"""Pydantic models for automation workflows.

Defines data structures for:
- Issue information and dependencies
- Planning and implementation state
- Worker results and options
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

# The canonical heading the planner writes at the top of the single plan
# comment is now defined in :mod:`hephaestus.automation.protocol` together
# with :data:`PLAN_REVIEW_PREFIX`. Re-exported here for backward compatibility
# with the historical ``from .models import PLAN_COMMENT_MARKER`` import path.
from .protocol import PLAN_COMMENT_MARKER as PLAN_COMMENT_MARKER

__all__ = ["PLAN_COMMENT_MARKER"]


class IssueState(str, Enum):
    """GitHub issue/PR state.

    ``MERGED`` only ever applies to pull requests, but PRs share the issues
    number-space, so a dependency reference (e.g. ``Depends on #123``) can point
    at a merged PR. The GitHub API then reports ``"MERGED"`` for that node, which
    must be a valid member or ``IssueState("MERGED")`` raises ``ValueError`` and
    the dependency fails to load.
    """

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"

    @property
    def is_done(self) -> bool:
        """True if the issue/PR is in a terminal state (closed or merged).

        Skip-logic that previously gated on ``== IssueState.CLOSED`` should use
        this so a merged-PR dependency is treated as complete rather than
        re-queued for work.
        """
        return self in (IssueState.CLOSED, IssueState.MERGED)


class IssueInfo(BaseModel):
    """Information about a GitHub issue."""

    number: int
    title: str
    body: str = ""
    state: IssueState = IssueState.OPEN
    labels: list[str] = Field(default_factory=list)
    dependencies: list[int] = Field(default_factory=list)
    priority: int = 0

    def __hash__(self) -> int:
        """Make IssueInfo hashable for use in sets."""
        return hash(self.number)

    def __eq__(self, other: object) -> bool:
        """Compare issues by number."""
        if not isinstance(other, IssueInfo):
            return NotImplemented
        return self.number == other.number


class ImplementationPhase(str, Enum):
    """Phase of issue implementation."""

    PLANNING = "planning"
    WAITING_FOR_PLAN_REVIEW = "waiting_for_plan_review"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"  # 3x review loop between implement and test
    TESTING = "testing"
    COMMITTING = "committing"
    PUSHING = "pushing"
    CREATING_PR = "creating_pr"
    LEARN = "learn"
    FOLLOW_UP_ISSUES = "follow_up_issues"
    COMPLETED = "completed"
    FAILED = "failed"


class ImplementationState(BaseModel):
    """State tracking for issue implementation."""

    issue_number: int
    phase: ImplementationPhase = ImplementationPhase.PLANNING
    worktree_path: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    session_id: str | None = None
    session_agent: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    learn_completed: bool = False
    review_iterations: int = 0  # number of review loop iterations executed
    last_review_verdict: str | None = None  # "GO", "NOGO", "AMBIGUOUS"
    last_review_grade: str | None = None  # letter grade from final review


class WorkerResult(BaseModel):
    """Result from a worker thread."""

    issue_number: int
    success: bool
    error: str | None = None
    pr_number: int | None = None
    branch_name: str | None = None
    worktree_path: str | None = None
    # Set True when the implementer skipped because the latest plan-review
    # verdict is not GO (NOGO / missing / unparseable). The issue is not
    # failed — it should be retried on the next automation loop after the
    # planner amends and the reviewer re-evaluates. See #551.
    plan_review_not_go: bool = False
    # Set True when an open PR already existed for this issue, so the
    # plan/implement steps were skipped. The PR is still driven through the
    # in-loop PR-review + address-review cycle here (to earn the
    # implementation-GO label) unless it was already settled by a prior loop.
    # Not a failure.
    already_has_pr: bool = False
    # Set True when the reviewer short-circuited (plan already cleared GO, or no
    # plan comment yet); the issue was not reviewed this pass and does not
    # count as work for loop convergence (#613).
    already_reviewed: bool = False


class PlanResult(BaseModel):
    """Result from planning an issue."""

    issue_number: int
    success: bool
    error: str | None = None
    plan_already_exists: bool = False


_DEFAULT_WORKERS = 3


class WorkerOptionsBase(BaseModel):
    """Shared fields for automation worker option models."""

    dry_run: bool = False


class _MaxWorkerOptionsBase(WorkerOptionsBase):
    """Shared fields for commands that expose --max-workers."""

    max_workers: int = _DEFAULT_WORKERS


class _ParallelWorkerOptionsBase(WorkerOptionsBase):
    """Shared fields for planner commands that expose --parallel."""

    parallel: int = _DEFAULT_WORKERS


class _VerboseWorkerOptionsBase(_MaxWorkerOptionsBase):
    """Shared fields for worker commands that expose --verbose."""

    verbose: bool = False


class PlannerOptions(_ParallelWorkerOptionsBase):
    """Options for the Planner."""

    issues: list[int]
    # True when ``issues`` came from an explicit ``--issues`` flag rather than
    # auto-discovery. Drives the "all issues filtered out" warning so a
    # mis-scoped explicit run is visible, while a converged auto-discovery pass
    # (legitimately empty) stays quiet.
    issues_explicit: bool = False
    agent: str = "claude"
    force: bool = False
    system_prompt_file: Path | None = None
    skip_closed: bool = True
    enable_advise: bool = True


class ImplementerOptions(_MaxWorkerOptionsBase):
    """Options for the Implementer."""

    epic_number: int = 0
    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    analyze_only: bool = False
    health_check: bool = False
    resume: bool = False
    skip_closed: bool = True
    auto_merge: bool = True
    enable_advise: bool = True
    enable_learn: bool = True
    enable_follow_up: bool = True
    enable_ui: bool = True
    # A2-004: opt-in pre-PR test gate; defaults to False for rollout safety.
    run_pre_pr_tests: bool = False
    # #1083: when False (default) the reviewer omits nitpick-severity comments;
    # --nitpick re-enables them.
    include_nitpicks: bool = False


class ReviewPhase(str, Enum):
    """Phase of PR review and fix workflow."""

    ANALYZING = "analyzing"
    FIXING = "fixing"
    PUSHING = "pushing"
    LEARN = "learn"
    COMPLETED = "completed"
    FAILED = "failed"
    POSTING = "posting"  # posting inline review comments to GitHub
    WAITING_CI = "waiting_ci"  # waiting for CI checks
    CI_FIXING = "ci_fixing"  # fixing CI failures
    MERGED = "merged"  # PR merged


class ReviewState(BaseModel):
    """State tracking for PR review and fix workflow."""

    issue_number: int
    pr_number: int
    phase: ReviewPhase = ReviewPhase.ANALYZING
    worktree_path: str | None = None
    branch_name: str | None = None
    plan_path: str | None = None
    session_id: str | None = None
    session_agent: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error: str | None = None
    posted_thread_ids: list[str] = Field(default_factory=list)  # GitHub review thread IDs posted
    addressed_thread_ids: list[str] = Field(default_factory=list)  # thread IDs Claude addressed


class ReviewerOptions(_MaxWorkerOptionsBase):
    """Options for the PRReviewer."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    enable_learn: bool = True
    enable_ui: bool = True


class PlanReviewerOptions(_VerboseWorkerOptionsBase):
    """Options for the PlanReviewer."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    enable_ui: bool = True


class AddressReviewOptions(_VerboseWorkerOptionsBase):
    """Options for the AddressReview workflow."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    enable_ui: bool = True
    resume_impl_session: bool = True  # attempt to resume implementer's saved agent session


class CIDriverOptions(_VerboseWorkerOptionsBase):
    """Options for the CIDriver workflow."""

    issues: list[int] = Field(default_factory=list)
    # Direct PR numbers to drive. Bypasses issue-to-PR discovery entirely
    # (#918). Used when the operator already knows the PR numbers and the
    # PRs do not use a strict ``Closes #N`` link (e.g. ``Refs #N``).
    prs: list[int] = Field(default_factory=list)
    agent: str = "claude"
    enable_advise: bool = True
    enable_learn: bool = True
    enable_ui: bool = True
    max_fix_iterations: int = 1  # number of fix attempts before giving up
    force_merge_on_stall: bool = False  # attempt squash-merge fallback if auto-merge fails
    # When True, _discover_prs unions the issue-driven set with every open
    # bot-authored PR on the repo (#848). Bot PRs (Dependabot, github-actions,
    # etc.) carry no ``Closes #N`` link so they are architecturally invisible
    # to issue-driven discovery; without this they leak forever.
    include_bot_prs: bool = True
    # When False (default, #821), discovery is restricted to PRs whose
    # author.login matches the authenticated `gh api user` viewer login.
    # Set True via the CLI's --all flag to drive every open PR (teammates'
    # and bots' included). Issue-scoped lookups (`--issues N`) bypass this
    # filter — they always resolve to the matching PR regardless of author.
    include_all_authors: bool = False
    # When True (default), _drive_issue first attempts a mechanical ``git
    # rebase`` onto the base branch for PRs that are behind/conflicting, pushing
    # the result with --force-with-lease — no agent spend. Only PRs whose rebase
    # hits real conflicts fall through to the Claude/Codex agent (#871).
    enable_mechanical_rebase: bool = True


class DependencyGraph(BaseModel):
    """Dependency graph for issues."""

    issues: dict[int, IssueInfo] = Field(default_factory=dict)
    edges: dict[int, list[int]] = Field(default_factory=dict)  # issue_number -> dependencies

    def add_issue(self, issue: IssueInfo) -> None:
        """Add an issue to the graph."""
        self.issues[issue.number] = issue
        if issue.number not in self.edges:
            self.edges[issue.number] = []

    def add_dependency(self, issue_number: int, depends_on: int) -> None:
        """Add a dependency edge.

        Args:
            issue_number: Issue that depends on another
            depends_on: Issue that must be completed first

        Raises:
            ValueError: If source issue doesn't exist in the graph

        Note:
            Dependency issue doesn't need to exist yet - it may be added later.
            This allows building the graph incrementally.

        """
        if issue_number not in self.issues:
            raise ValueError(f"Issue #{issue_number} not in graph")

        if issue_number not in self.edges:
            self.edges[issue_number] = []
        if depends_on not in self.edges[issue_number]:
            self.edges[issue_number].append(depends_on)

    def get_dependencies(self, issue_number: int) -> list[int]:
        """Get direct dependencies for an issue."""
        return self.edges.get(issue_number, [])

    def get_all_dependencies(self, issue_number: int) -> set[int]:
        """Get all transitive dependencies for an issue."""
        deps: set[int] = set()
        to_visit = [issue_number]
        visited: set[int] = set()

        while to_visit:
            current = to_visit.pop()
            if current in visited:
                continue
            visited.add(current)

            for dep in self.get_dependencies(current):
                deps.add(dep)
                to_visit.append(dep)

        return deps
