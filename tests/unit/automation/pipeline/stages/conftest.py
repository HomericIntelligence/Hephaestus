"""Fixtures and fakes for pipeline stage tests.

``FakeStageGitHub`` extends the canonical pipeline ``FakeGitHub`` (see
``tests/unit/automation/pipeline/conftest.py``) with the read surface the
planning/plan_review stages use (``gh_issue_json``, PR-coverage lookups,
tri-state plan discovery), so mutator call sites and the ``mutation_log``
format stay identical to what coordinator tests (#1817) will assert.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.dependency_parser import DependencyFact
from hephaestus.automation.github_api import issue_body_digest
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.events import StageEvent
from hephaestus.automation.pipeline.github_jobs import (
    ReadRepositoryValidationCIRequest,
    RepositoryValidationCIRead,
)
from hephaestus.automation.pipeline.routing import ROUTES, StageName
from hephaestus.automation.pipeline.stages import (
    AutoMergeEnableResult,
    StageContext,
    StageGitHub,
)
from hephaestus.automation.pipeline.stages.base import (
    BranchWorktreeOwnerStatus,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
)
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    render_current_plan,
    render_current_review,
    render_pending_review,
)
from hephaestus.automation.source_worktree import _PreparationDeadline
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_PLAN_NO_GO,
)
from hephaestus.utils.worktree_identity import source_worktree_name
from tests.unit.automation.pipeline.conftest import FakeGitHub


class _DefaultPrState:
    """Sentinel that distinguishes a default open PR from an explicit failure."""


_DEFAULT_PR_STATE = _DefaultPrState()


class FakeStageGitHub(FakeGitHub):
    """Canonical FakeGitHub plus the stage read queries.

    Implements the :class:`StageGitHub` protocol (mypy-checked below).
    Reads mirror the real helper names the stages call through
    ``ctx.github``: ``gh_issue_json`` (github_api.issues),
    ``find_merged_closing_pr`` / ``find_pr_for_issue`` /
    ``close_issue_as_covered`` (_review_utils), and ``discover_plan``
    (PlannerStateManager).
    """

    def __init__(
        self,
        *,
        labels: list[str] | None = None,
        issue_title: str = "A task",
        issue_body: str = "",
        issue_state: str = "OPEN",
        merged_pr: int | None = None,
        open_pr: int | None = None,
        pr_issue: int | None = None,
        has_plan: bool = False,
        pr_head_branch: str | None = None,
        pr_head_writable: bool = True,
        pr_impl_state: tuple[bool, bool] = (False, False),
        pr_impl_readbacks: list[tuple[bool, bool] | Exception] | None = None,
        unresolved: list[tuple[int, int]] | None = None,
        by_severity: list[tuple[int, int, int]] | None = None,
        pr_state: dict[str, Any] | _DefaultPrState | None = _DEFAULT_PR_STATE,
        conversation_resolution: bool = True,
        pr_review_context: dict[str, str] | None = None,
        plan_read_error: str | None = None,
        journal_read_error: str | None = None,
        dependency_fact_batches: list[tuple[DependencyFact, ...] | Exception] | None = None,
    ) -> None:
        """Initialize the fake with canned read answers.

        Args:
            labels: Seed labels applied to any issue on first read/mutation.
            issue_title: Canned issue title returned by gh_issue_json.
            issue_body: Canned issue body returned by gh_issue_json.
            issue_state: Canned current GitHub issue state.
            merged_pr: Canned answer for find_merged_closing_pr.
            open_pr: Canned answer for find_pr_for_issue.
            pr_issue: Canned answer for find_issue_for_pr.
            has_plan: Canned answer for plan discovery.
            pr_head_branch: Canned answer for get_pr_head_branch.
            pr_head_writable: Whether the PR head belongs to the base origin
                and may receive coordinator-owned address commits.
            pr_impl_state: Canned (has_go, has_no_go) answer for
                pr_has_implementation_state_label.
            pr_impl_readbacks: Optional FIFO of independent label readbacks;
                entries may be contradictory, absent, or exceptions. When
                empty, the current post-mutation state is returned.
            unresolved: FIFO of ``(automation, external)`` review-thread
                shapes.  Each shape is consumed per fresh snapshot, with the
                final shape repeating; this concise form treats automation
                threads as blocking.
            by_severity: FIFO of ``(blocking, advisory, external)``
                review-thread shapes.  Supply this form when a test needs to
                distinguish blocking and advisory automation threads.
            pr_state: Canned answer for gh_pr_state (merge_wait's single
                PR-state read); ``None`` mirrors a transient read failure.
            conversation_resolution: Whether the admitted PR base has the
                server-enforced required-conversation-resolution protection.
            plan_read_error: Optional failure returned by plan-comment
                discovery.
            journal_read_error: Optional failure returned by review-journal
                discovery.
            dependency_fact_batches: Optional live dependency results. The
                final result repeats after the scripted transitions finish.

        """
        super().__init__()
        self._seed_labels = list(labels or [])
        self._issue_title = issue_title
        self._issue_body = issue_body
        self._issue_state = issue_state
        self._merged_pr = merged_pr
        self._open_pr = open_pr
        self._pr_issue = pr_issue
        self._has_plan = has_plan
        self._plan_read_error = plan_read_error
        self._journal_read_error = journal_read_error
        self._pr_head_branch = pr_head_branch
        self._pr_head_writable = pr_head_writable
        self._pr_impl_state = pr_impl_state
        self._pr_impl_readbacks = deque(pr_impl_readbacks or [])
        self._unresolved: list[tuple[int, int]] = list(unresolved or [(0, 0)])
        self._by_severity = (
            list(by_severity)
            if by_severity is not None
            else [(a, 0, h) for (a, h) in self._unresolved]
        )
        self._pr_state = (
            {
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "autoMergeRequest": None,
                "baseRefName": "main",
            }
            if isinstance(pr_state, _DefaultPrState)
            else pr_state
        )
        self.merge_attempts: list[tuple[int, str]] = []
        self.auto_merge_attempts: list[int] = []
        self._conversation_resolution = conversation_resolution
        self._pr_review_context = (
            pr_review_context
            if pr_review_context is not None
            else {
                "pr_title": "A current PR title",
                "pr_description": "Closes #1",
                "pr_head_sha": "a" * 40,
                "pr_base_branch": "main",
            }
        )
        self._posted_thread_ids: dict[int, list[str]] = {}
        self._thread_replies: dict[str, list[dict[str, str]]] = {}
        self.body_only_findings: dict[int, list[dict[str, Any]]] = {}
        self.review_summaries: dict[int, str] = {}
        self.operation_deadlines: list[tuple[float, threading.Event | None]] = []
        self._dependency_fact_batches = deque(dependency_fact_batches or [])
        self.dependency_fact_requests: list[tuple[int, ...]] = []

    @contextmanager
    def operation_deadline(
        self, deadline_s: float, *, shutdown: threading.Event | None = None
    ) -> Iterator[None]:
        """Record the worker operation scope without external I/O."""
        self.operation_deadlines.append((deadline_s, shutdown))
        if shutdown is not None and shutdown.is_set():
            raise InterruptedError("test GitHub operation cancelled")
        yield

    def read_repository_validation_ci(
        self, request: ReadRepositoryValidationCIRequest
    ) -> RepositoryValidationCIRead:
        """Require each test to supply its own CI evidence."""
        raise NotImplementedError("The test must supply CI evidence.")

    def _issue_labels(self, issue_number: int) -> set[str]:
        """Return the issue's label set, seeding it on first access."""
        if issue_number not in self.labels:
            self.labels[issue_number] = set(self._seed_labels)
        return self.labels[issue_number]

    # -- read surface used by the stages -----------------------------------
    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Mirror github_api.issues.gh_issue_json (issue context plus labels)."""
        stored = self.issues.get(issue_number)
        if isinstance(stored, dict):
            body = str(stored.get("body") or self._issue_body)
            return {
                "number": issue_number,
                "title": str(stored.get("title") or self._issue_title),
                "body": body,
                "bodyDigest": issue_body_digest(body),
                "state": str(stored.get("state") or self._issue_state),
                "labels": [{"name": name} for name in sorted(self._issue_labels(issue_number))],
            }
        return {
            "number": issue_number,
            "title": self._issue_title,
            "body": self._issue_body,
            "bodyDigest": issue_body_digest(self._issue_body),
            "state": self._issue_state,
            "labels": [{"name": name} for name in sorted(self._issue_labels(issue_number))],
        }

    def batch_dependency_facts(
        self,
        issue_numbers: Sequence[int],
        *,
        deadline_s: float,
        shutdown: threading.Event | None = None,
    ) -> tuple[Any, ...]:
        """Return the next scripted complete dependency batch."""
        del deadline_s, shutdown
        requested = tuple(issue_numbers)
        self.dependency_fact_requests.append(requested)
        if not self._dependency_fact_batches:
            raise RuntimeError("dependency fact batch was not scripted")
        result = self._dependency_fact_batches[0]
        if len(self._dependency_fact_batches) > 1:
            result = self._dependency_fact_batches.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_merged_closing_pr."""
        return self._merged_pr

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_merged_pr_for_issue."""
        return self._merged_pr

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_pr_for_issue (open PR lookup)."""
        return self._open_pr

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        """Mirror PipelineGitHub.find_issue_for_pr (PR body Closes lookup)."""
        return self._pr_issue

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Return a canned tri-state plan-discovery result."""
        if self._plan_read_error is not None:
            return PlanDiscoveryResult.read_error(self._plan_read_error)
        if self._has_plan:
            return PlanDiscoveryResult.found(render_current_plan("Fake plan"))
        return PlanDiscoveryResult.absent()

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return fake issue comments in their append order with ownership metadata."""
        if self._journal_read_error is not None:
            raise CommentJournalReadError(self._journal_read_error)
        comments = self.comments.get(issue_number, [])
        if not comments and self._has_plan:
            comments = [render_current_plan("Fake plan")]
            if STATE_PLAN_NO_GO in self._issue_labels(issue_number):
                comments.append(
                    render_current_review(
                        "Rejected fake plan\n\nstate:plan-no-go",
                        revision=1,
                    )
                )
            else:
                comments.append(render_pending_review(revision=1))
        return [
            comment
            if isinstance(comment, IssueComment)
            else IssueComment(
                body=comment,
                author_login="hephaestus[bot]",
                viewer_did_author=True,
            )
            for comment in comments
        ]

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Mirror _review_utils.get_pr_head_branch (canned answer)."""
        return self._pr_head_branch

    def pr_head_is_writable(self, pr_number: int) -> bool:
        """Mirror PipelineGitHub.pr_head_is_writable (canned answer)."""
        return self._pr_head_writable

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return a scripted independent label readback, when available."""
        del pr_number
        if self._pr_impl_readbacks:
            readback = self._pr_impl_readbacks.popleft()
            if isinstance(readback, Exception):
                raise readback
            return readback
        return self._pr_impl_state

    def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
        """Mirror PipelineGitHub's atomic PR-review input read."""
        del pr_number
        return dict(self._pr_review_context) if self._pr_review_context is not None else None

    def _next_unresolved_thread_counts(self) -> tuple[int, int, int]:
        """Return the next scripted open-thread shape for this test double."""
        if len(self._by_severity) > 1:
            return self._by_severity.pop(0)
        return self._by_severity[0]

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Return scripted fresh review-thread facts for label-gate tests."""
        blocking, advisory, external = self._next_unresolved_thread_counts()
        posted = list(self._posted_thread_ids.get(pr_number, []))
        posted_comments = (
            list(self.reviews.get(pr_number, [{}])[-1].get("comments", []))
            if self.reviews.get(pr_number)
            else []
        )
        threads: list[dict[str, Any]] = []
        cursor = 0
        for count, author, severity in (
            (blocking, False, "major"),
            (advisory, False, "nitpick"),
            (external, True, "major"),
        ):
            for _ in range(count):
                posted_comment = posted_comments[cursor] if cursor < len(posted_comments) else {}
                thread_id = (
                    posted[cursor] if cursor < len(posted) else f"live-thread-{pr_number}-{cursor}"
                )
                cursor += 1
                threads.append(
                    {
                        "id": thread_id,
                        "path": posted_comment.get("path") or "a.py",
                        "line": posted_comment.get("line") or cursor,
                        "side": "RIGHT",
                        "severity": severity,
                        "body": (
                            f"<!-- hephaestus-severity: {severity} -->\n"
                            f"{posted_comment.get('body') or 'finding'}"
                        ),
                        "author": "reviewer" if author else "hephaestus[bot]",
                        "authors": ["reviewer" if author else "hephaestus[bot]"],
                        "review_id": f"review-{pr_number}-{cursor}",
                        "comments": [
                            {
                                "id": f"comment-{thread_id}",
                                "author": "reviewer" if author else "hephaestus[bot]",
                                "body": posted_comment.get("body") or "finding",
                            },
                            *self._thread_replies.get(thread_id, []),
                        ],
                    }
                )
        return threads

    # -- mutator surface used by the stages ----------------------------------
    # Coordinator-neutral names (the pipeline architecture guard forbids
    # github_api mutator names inside pipeline modules); each delegates to
    # the canonical gh_* recorder so mutation_log keeps the canonical format.
    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Mirror _review_utils.close_issue_as_covered (records mutation)."""
        self._log("close_issue_as_covered", issue_number, pr_number)

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Coordinator-neutral label add (delegates to gh_issue_add_labels)."""
        self._issue_labels(issue_number)
        self.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Coordinator-neutral label remove (delegates to gh_issue_remove_labels)."""
        self._issue_labels(issue_number)
        has_go, has_no_go = self._pr_impl_state
        if STATE_IMPLEMENTATION_GO in labels:
            has_go = False
        if STATE_IMPLEMENTATION_NO_GO in labels:
            has_no_go = False
        self._pr_impl_state = (has_go, has_no_go)
        self.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomic add+remove recorded as ONE mutation (mirrors gh issue edit)."""
        labels = self._issue_labels(issue_number)
        labels.update(add)
        labels.difference_update(remove)
        self._log("edit_labels", issue_number, tuple(add), tuple(remove))

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Mirror the generic marker-keyed comment upsert (#2256)."""
        comments = self.comments.setdefault(issue_number, [])
        matches = [
            index
            for index, comment in enumerate(comments)
            if (comment.body if isinstance(comment, IssueComment) else comment)
            .lstrip()
            .startswith(marker)
        ]
        if not matches and legacy_marker is not None:
            matches = [
                index
                for index, comment in enumerate(comments)
                if (comment.body if isinstance(comment, IssueComment) else comment)
                .lstrip()
                .startswith(legacy_marker)
            ]
        if matches:
            comments[matches[-1]] = body
        else:
            comments.append(body)
        self._log("gh_issue_upsert_comment", issue_number, marker)

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Mirror the immutable replay-safe journal append."""
        comments = self.comments.setdefault(issue_number, [])
        matching = [
            comment.body if isinstance(comment, IssueComment) else comment
            for comment in comments
            if (comment.body if isinstance(comment, IssueComment) else comment)
            .lstrip()
            .startswith(marker)
        ]
        if matching and any(existing != body for existing in matching):
            raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
        if not matching:
            comments.append(body)
            self._log("append_issue_comment", issue_number, marker)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Mirror the coordinator plan-comment upsert (PLAN_COMMENT_MARKER-keyed).

        Delegates to the canonical ``gh_issue_upsert_comment`` recorder so
        the mutation_log keeps the canonical format, and flips the
        plan-discovery answer to FOUND — the posted comment IS the durable
        plan artifact the verify step reads back.
        """
        self._has_plan = True
        self.upsert_issue_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            body,
        )

    def open_prs_for_branch(self, branch_name: str) -> list[tuple[int, str]]:
        """Return all stored open PRs for the exact branch."""
        rows = {
            number: str(pr.get("base", "main"))
            for number, pr in self.prs.items()
            if pr.get("branch") == branch_name and pr.get("state", "OPEN") == "OPEN"
        }
        if self._open_pr is not None and self._pr_head_branch == branch_name:
            rows[self._open_pr] = "main"
        return sorted(rows.items())

    def create_pr(
        self,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        *,
        strict_absence: bool = False,
    ) -> int:
        """Mirror normal and strict PR creation through the fake store."""
        if type(strict_absence) is not bool:
            raise ValueError("strict_absence must be a boolean")
        stored_issue_pr = any(
            pr.get("issue_number") == issue_number and pr.get("state", "OPEN") == "OPEN"
            for pr in self.prs.values()
        )
        if strict_absence and (
            self.open_prs_for_branch(branch)
            or self.find_pr_for_issue(issue_number) is not None
            or stored_issue_pr
        ):
            raise RuntimeError("strict PR creation found an existing PR")
        number = self.gh_pr_create(branch, title, body)
        self.prs[number]["issue_number"] = issue_number
        return number

    def post_review(
        self,
        pr_number: int,
        inline: list[dict[str, Any]],
        body_only: list[dict[str, Any]],
        summary: str = "",
    ) -> None:
        """Record one posted review and the findings kept in its body."""
        self.review_summaries[pr_number] = summary
        ids = self.gh_pr_review_post(pr_number, inline, "")
        self._posted_thread_ids[pr_number] = list(ids)
        self.body_only_findings[pr_number] = list(body_only)
        self._log("post_review", pr_number, len(inline), len(body_only))

    def apply_review_thread_decisions(
        self,
        pr_number: int,
        *,
        resolved_thread_ids: tuple[str, ...],
        feedback: dict[str, str],
    ) -> None:
        """Record the reviewer's decisions on open threads."""
        self._log(
            "apply_review_thread_decisions",
            pr_number,
            tuple(sorted(resolved_thread_ids)),
            tuple(sorted(feedback)),
        )

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_go (records mutation)."""
        self._pr_impl_state = (True, False)
        self._log("mark_pr_implementation_go", pr_number)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_no_go (records mutation)."""
        self._pr_impl_state = (False, True)
        self._log("mark_pr_implementation_no_go", pr_number)

    def reviewed_pr_state(self, pull_request_id: str) -> dict[str, Any] | None:
        """Return no terminal proof unless the test supplies it."""
        return None

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror ci_driver.CIDriver._gh_pr_state (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return self._pr_state

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror the post-405 operational readiness lookup."""
        del pr_number
        return dict(self._pr_state) if isinstance(self._pr_state, dict) else self._pr_state

    @property
    def _repo_slug(self) -> str:
        """Return the stable repository identity used by GitHub queries."""
        return "org/repo-a"

    def _viewer_login(self) -> str:
        """Return the fake authenticated automation actor."""
        return "hephaestus[bot]"

    def enable_auto_merge(
        self,
        pr_number: int,
        **_kwargs: Any,
    ) -> AutoMergeEnableResult:
        """Record one native auto-merge enablement."""
        self.auto_merge_attempts.append(pr_number)
        self.merge_attempts.append((pr_number, ""))
        return AutoMergeEnableResult(enabled=True)

    def ensure_state_labels(self) -> None:
        """Mirror the repo-stage label-vocabulary ensure (records mutation)."""
        self._log("ensure_state_labels")


if TYPE_CHECKING:
    # mypy-enforced declaration that FakeStageGitHub satisfies the
    # StageGitHub protocol (m5): a drifted signature fails type checking.
    _stage_github_protocol_check: StageGitHub = FakeStageGitHub()


def _budget_fn(name: str) -> int:
    """Look up a budget across all ROUTES rows (conservative default 1)."""
    for route in ROUTES.values():
        if name in route.budgets:
            return route.budgets[name]
    return 1


class FakeSourceWorkspaceManager:
    """Return explicit source bindings for tests that do not run a provider."""

    def __init__(self, repo_root: Path = Path("/tmp/repo"), repository: str = "test-repo") -> None:
        """Set the repository identity used by each test binding."""
        self.repo_root = repo_root
        self.repository = repository

    def prepare_bounded(
        self,
        item_number: int,
        lane: SourceLane,
        revision: str,
        *,
        branch: str | None = None,
        deadline: _PreparationDeadline | None = None,
    ) -> WorkspaceBinding:
        """Return a complete binding without Git or provider execution."""
        return WorkspaceBinding.source(
            cwd=self.repo_root
            / "build"
            / ".worktrees"
            / source_worktree_name(item_number, lane.value),
            reusable_root=self.repo_root,
            repository=self.repository,
            ownership_key=f"{self.repository}:test:{item_number}:{lane.value}",
            item_number=item_number,
            lane=lane,
            revision=revision,
            generation=1,
            detached=branch is None,
        )


class _Paths:
    """Supply an explicit source manager for stage tests."""

    repo_root = "/tmp/repo"
    worktree = "/tmp/repo/worktree"

    def __init__(self) -> None:
        self.source_workspaces = FakeSourceWorkspaceManager()


@pytest.fixture
def make_ctx() -> Callable[..., StageContext]:
    """Build StageContext instances with a fake clock and ROUTES budgets."""

    def _make_ctx(
        *,
        config: PipelineConfig | None = None,
        config_overrides: dict[str, Any] | None = None,
        org: str = "test-org",
        dry_run: bool = False,
        github: FakeStageGitHub | None = None,
        paths: Any = None,
        now_fn: Callable[[], float] | None = None,
        budget_fn: Callable[[str], int] | None = None,
        event_fn: Callable[[StageEvent], None] | None = None,
        learning_journal: Any = None,
        plan_review_sessions: Any = None,
        branch_worktree_owner_status: (
            Callable[[WorkItem, str, str], BranchWorktreeOwnerStatus] | None
        ) = None,
    ) -> StageContext:
        if config is not None and config_overrides:
            raise ValueError("pass config or config_overrides, not both")

        ticks = [0]

        def default_now_fn() -> float:
            ticks[0] += 1
            return 1000.0 + ticks[0]

        resolved_config = config or PipelineConfig(
            org=org,
            repos=["test-repo"],
            dry_run=dry_run,
        )
        if config_overrides:
            resolved_config = replace(resolved_config, **config_overrides)

        return StageContext(
            config=resolved_config,
            org=org,
            dry_run=dry_run,
            github=github if github is not None else FakeStageGitHub(),
            paths=paths if paths is not None else _Paths(),
            now_fn=now_fn if now_fn is not None else default_now_fn,
            budget_fn=budget_fn if budget_fn is not None else _budget_fn,
            event_fn=event_fn,
            learning_journal=learning_journal,
            plan_review_sessions=plan_review_sessions,
            branch_worktree_owner_status=branch_worktree_owner_status,
        )

    return _make_ctx


@pytest.fixture
def make_work_item() -> Callable[..., WorkItem]:
    """Build WorkItem instances parked in a plan-side stage."""

    def _make_item(
        *,
        repo: str = "test-repo",
        kind: ItemKind = ItemKind.ISSUE,
        issue: int | None = 1,
        pr: int | None = None,
        stage: StageName = StageName.PLANNING,
        state: str = "ENTER",
        labels: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkItem:
        item = WorkItem(repo=repo, kind=kind, issue=issue, pr=pr, stage=stage, state=state)
        if state == "COMMIT_PUSH_WAIT":
            item.payload.update({"issue_title": "A task", "issue_body": ""})
        if labels:
            item.labels_cache = dict.fromkeys(labels, True)
        if payload:
            item.payload = payload
        if not any(
            key in item.payload for key in ("_synced_default_branch_sha", "_direct_scope_base_sha")
        ):
            item.payload["_synced_default_branch_sha"] = "a" * 40
        # Direct EVAL unit tests model a review job that has already crossed
        # the checkout barrier. Integration walks still install this proof only
        # after the barrier completes.
        if pr is not None and state == "EVAL":
            item.payload.setdefault("reviewed_pr_head_sha", "a" * 40)
        return item

    return _make_item
