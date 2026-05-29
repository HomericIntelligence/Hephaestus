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

# The canonical heading the planner WRITES at the top of the single plan
# comment. The pipeline upserts exactly one comment starting with this marker
# (see github_api.gh_issue_upsert_comment). This is the only marker used to
# *locate the plan to review*.
PLAN_COMMENT_MARKER: str = "# Implementation Plan"


class IssueState(str, Enum):
    """GitHub issue state."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


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
    # verdict is not APPROVED (REVISE / BLOCK / missing). The issue is not
    # failed — it should be retried on the next automation loop after the
    # planner amends and the reviewer re-evaluates. See #551.
    plan_review_not_approved: bool = False
    # Set True when the implementer skipped because an open PR already exists
    # for this issue. Re-implementing would clobber in-flight work; the open
    # PR is handled by the implementer's in-loop PR-review + address-review
    # steps and the later drive-green stage. Not a failure.
    already_has_pr: bool = False
    # Set True when the reviewer short-circuited (plan already APPROVED, or no
    # plan comment yet); the issue was not reviewed this pass and does not
    # count as work for loop convergence (#613).
    already_reviewed: bool = False


class PlanResult(BaseModel):
    """Result from planning an issue."""

    issue_number: int
    success: bool
    error: str | None = None
    plan_already_exists: bool = False


class PlannerOptions(BaseModel):
    """Options for the Planner."""

    issues: list[int]
    agent: str = "claude"
    dry_run: bool = False
    force: bool = False
    parallel: int = 3
    system_prompt_file: Path | None = None
    skip_closed: bool = True
    enable_advise: bool = True


class ImplementerOptions(BaseModel):
    """Options for the Implementer."""

    epic_number: int = 0
    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    analyze_only: bool = False
    health_check: bool = False
    resume: bool = False
    max_workers: int = 3
    skip_closed: bool = True
    auto_merge: bool = True
    dry_run: bool = False
    enable_advise: bool = True
    enable_learn: bool = True
    enable_follow_up: bool = True
    enable_ui: bool = True
    # A2-004: opt-in pre-PR test gate; defaults to False for rollout safety.
    run_pre_pr_tests: bool = False


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
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error: str | None = None
    posted_thread_ids: list[str] = Field(default_factory=list)  # GitHub review thread IDs posted
    addressed_thread_ids: list[str] = Field(default_factory=list)  # thread IDs Claude addressed


class ReviewerOptions(BaseModel):
    """Options for the PRReviewer."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    max_workers: int = 3
    dry_run: bool = False
    enable_learn: bool = True
    enable_ui: bool = True


class PlanReviewerOptions(BaseModel):
    """Options for the PlanReviewer."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    max_workers: int = 3
    dry_run: bool = False
    enable_ui: bool = True
    verbose: bool = False


class AddressReviewOptions(BaseModel):
    """Options for the AddressReview workflow."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    max_workers: int = 3
    dry_run: bool = False
    enable_ui: bool = True
    verbose: bool = False
    resume_impl_session: bool = True  # attempt to resume implementer's Claude session


class CIDriverOptions(BaseModel):
    """Options for the CIDriver workflow."""

    issues: list[int] = Field(default_factory=list)
    agent: str = "claude"
    max_workers: int = 3
    dry_run: bool = False
    enable_advise: bool = True
    enable_learn: bool = True
    enable_ui: bool = True
    verbose: bool = False
    max_fix_iterations: int = 1  # number of fix attempts before giving up
    force_merge_on_stall: bool = False  # attempt squash-merge fallback if auto-merge fails


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
