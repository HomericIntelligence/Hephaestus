"""Fixtures and fakes for pipeline stage tests.

``FakeStageGitHub`` extends the canonical pipeline ``FakeGitHub`` (see
``tests/unit/automation/pipeline/conftest.py``) with the read surface the
planning/plan_review stages use (``gh_issue_json``, PR-coverage lookups,
plan-comment presence), so mutator call sites and the ``mutation_log``
format stay identical to what coordinator tests (#1817) will assert.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from hephaestus.automation.pipeline.routing import ROUTES, StageName
from hephaestus.automation.pipeline.stages import StageContext, StageGitHub
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER
from tests.unit.automation.pipeline.conftest import FakeGitHub


class FakeStageGitHub(FakeGitHub):
    """Canonical FakeGitHub plus the stage read queries.

    Implements the :class:`StageGitHub` protocol (mypy-checked below).
    Reads mirror the real helper names the stages call through
    ``ctx.github``: ``gh_issue_json`` (github_api.issues),
    ``find_merged_closing_pr`` / ``find_pr_for_issue`` /
    ``close_issue_as_covered`` (_review_utils), and ``has_existing_plan``
    (PlannerStateManager).
    """

    def __init__(
        self,
        *,
        labels: list[str] | None = None,
        merged_pr: int | None = None,
        open_pr: int | None = None,
        has_plan: bool = False,
        pr_head_branch: str | None = None,
        pr_impl_state: tuple[bool, bool] = (False, False),
        unresolved: list[tuple[int, int]] | None = None,
        pr_state: dict[str, Any] | None = None,
        failing_checks: list[str] | None = None,
        pending_checks: list[str] | None = None,
        checks: list[dict[str, Any]] | None = None,
        pr_stuck: bool = False,
        learn_terminal: bool = False,
    ) -> None:
        """Initialize the fake with canned read answers.

        Args:
            labels: Seed labels applied to any issue on first read/mutation.
            merged_pr: Canned answer for find_merged_closing_pr.
            open_pr: Canned answer for find_pr_for_issue.
            has_plan: Canned answer for has_existing_plan.
            pr_head_branch: Canned answer for get_pr_head_branch.
            pr_impl_state: Canned (has_go, has_no_go) answer for
                pr_has_implementation_state_label.
            unresolved: FIFO of (automation, human) answers for
                count_unresolved_threads — consumed one per call, last
                entry repeating (lets tests script a decreasing /
                plateauing thread count for the #1554 progress rule).
            pr_state: Canned answer for gh_pr_state (merge_wait's single
                PR-state read); ``None`` mirrors a transient read failure.
            failing_checks: Canned answer for failing_required_check_names.
            pending_checks: Canned answer for pending_required_check_names.
            checks: Canned answer for pr_checks (all checks for CI polling).
            pr_stuck: Canned answer for pr_is_genuinely_stuck.
            learn_terminal: Seed answer for drive_green_learn_terminal —
                True mirrors an issue whose post-merge /learn already ran
                terminally (the #848 dedupe record).

        """
        super().__init__()
        self._seed_labels = list(labels or [])
        self._merged_pr = merged_pr
        self._open_pr = open_pr
        self._has_plan = has_plan
        self._pr_head_branch = pr_head_branch
        self._pr_impl_state = pr_impl_state
        self._unresolved: list[tuple[int, int]] = list(unresolved or [(0, 0)])
        self._pr_state = pr_state
        self._failing_checks = list(failing_checks or [])
        self._pending_checks = list(pending_checks or [])
        self._checks = list(checks or [])
        self._pr_stuck = pr_stuck
        self._learn_terminal = learn_terminal
        self.arming_records: dict[int, tuple[int, str]] = {}
        self.learn_results: dict[int, bool] = {}

    def _issue_labels(self, issue_number: int) -> set[str]:
        """Return the issue's label set, seeding it on first access."""
        if issue_number not in self.labels:
            self.labels[issue_number] = set(self._seed_labels)
        return self.labels[issue_number]

    # -- read surface used by the stages -----------------------------------
    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Mirror github_api.issues.gh_issue_json (labels subset only)."""
        return {"labels": [{"name": name} for name in sorted(self._issue_labels(issue_number))]}

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_merged_closing_pr."""
        return self._merged_pr

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Mirror _review_utils.find_pr_for_issue (open PR lookup)."""
        return self._open_pr

    def has_existing_plan(self, issue_number: int) -> bool:
        """Mirror PlannerStateManager.has_existing_plan (plan comment check)."""
        return self._has_plan

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Mirror _review_utils.get_pr_head_branch (canned answer)."""
        return self._pr_head_branch

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Mirror pr_manager.pr_has_implementation_state_label (canned answer)."""
        return self._pr_impl_state

    def count_unresolved_threads(self, pr_number: int) -> tuple[int, int]:
        """Mirror _review_phase._count_unresolved_threads_blocking_go (FIFO).

        Consumes one scripted (automation, human) answer per call; the last
        entry repeats once the FIFO drains.
        """
        if len(self._unresolved) > 1:
            return self._unresolved.pop(0)
        return self._unresolved[0]

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
        self.gh_issue_remove_labels(issue_number, labels)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Mirror the coordinator plan-comment upsert (PLAN_COMMENT_MARKER-keyed).

        Delegates to the canonical ``gh_issue_upsert_comment`` recorder so
        the mutation_log keeps the canonical format, and flips the
        ``has_existing_plan`` answer to True — the posted comment IS the
        durable plan artifact the verify step reads back.
        """
        self._has_plan = True
        self.gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, body)

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Mirror the coordinator PR-ensure (delegates to gh_pr_create)."""
        return self.gh_pr_create(branch, title, body)

    def defer_auto_merge(self, pr_number: int) -> None:
        """Mirror pr_manager.ensure_pr_auto_merge_deferred (records mutation)."""
        self._log("defer_auto_merge", pr_number)

    def post_review_threads(
        self, pr_number: int, threads: list[dict[str, Any]], summary: str
    ) -> list[str]:
        """Mirror the coordinator thread post (delegates to gh_pr_review_post)."""
        return self.gh_pr_review_post(pr_number, threads, summary)

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_go (records mutation)."""
        self._log("mark_pr_implementation_go", pr_number)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Mirror pr_manager.mark_pr_implementation_no_go (records mutation)."""
        self._log("mark_pr_implementation_no_go", pr_number)

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Mirror the coordinator PR-comment post (delegates to gh_issue_comment).

        PRs share the issue comment channel, so the canonical
        ``gh_issue_comment`` recorder keeps the mutation_log format and
        stores the body for content assertions.
        """
        self.gh_issue_comment(pr_number, body)

    def arm_auto_merge(self, pr_number: int) -> None:
        """Mirror pr_manager.enable_auto_merge_after_implementation_go."""
        self._log("arm_auto_merge", pr_number)

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror ci_driver.CIDriver._gh_pr_state (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return self._pr_state

    def failing_required_check_names(self, pr_number: int) -> list[str]:
        """Mirror CICheckInspector.failing_required_check_names (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return list(self._failing_checks)

    def pending_required_check_names(self, pr_number: int) -> list[str]:
        """Mirror CICheckInspector.pending_required_check_names (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return list(self._pending_checks)

    def pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        """Mirror gh_pr_checks (returns all checks for CI classification)."""
        del pr_number  # single canned answer; not per-PR keyed
        return list(self._checks)

    def arm_drive_green(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        """Mirror ci_driver.CIDriver._arm_drive_green (records the arming record)."""
        self.arming_records[issue_number] = (pr_number, head_sha)
        self._log("arm_drive_green", issue_number, pr_number, head_sha)

    def pr_is_genuinely_stuck(self, pr_number: int) -> bool:
        """Mirror pr_manager.pr_is_genuinely_stuck (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return self._pr_stuck

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Mirror ci_driver._learn_record_terminal over the arming record.

        Terminal when seeded so (``learn_terminal=True``) or once
        :meth:`mark_drive_green_learn_result` recorded an outcome — the
        exactly-once /learn read-back (#848).
        """
        return self._learn_terminal or issue_number in self.learn_results

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Mirror post_merge_processor.mark_drive_green_learn_result [durable]."""
        self.learn_results[issue_number] = succeeded
        self._log("mark_drive_green_learn_result", issue_number, succeeded)

    def ensure_state_labels(self) -> None:
        """Mirror the repo-stage label-vocabulary ensure (records mutation).

        The canonical ``skip_epics`` recorder is inherited from
        :class:`FakeGitHub`; this is the only repo-stage (#1817) protocol
        method without a canonical recorder there.
        """
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


class _Config:
    """PlannerOptions-like config stub for stage tests."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.enable_advise = True
        self.enable_learn = True
        self.enable_follow_up = True
        self.run_pre_pr_tests = False
        self.force = False
        self.agent = "claude"
        self.dry_run = dry_run


class _Paths:
    """Path accessor stub for stage tests."""

    repo_root = "/tmp/repo"
    worktree = "/tmp/repo/worktree"


@pytest.fixture
def make_ctx() -> Callable[..., StageContext]:
    """Build StageContext instances with a fake clock and ROUTES budgets."""

    def _make_ctx(
        *,
        config: Any = None,
        org: str = "test-org",
        dry_run: bool = False,
        github: FakeStageGitHub | None = None,
        paths: Any = None,
    ) -> StageContext:
        ticks = [0]

        def now_fn() -> float:
            ticks[0] += 1
            return 1000.0 + ticks[0]

        return StageContext(
            config=config if config is not None else _Config(dry_run=dry_run),
            org=org,
            dry_run=dry_run,
            github=github if github is not None else FakeStageGitHub(),
            paths=paths if paths is not None else _Paths(),
            now_fn=now_fn,
            budget_fn=_budget_fn,
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
        if labels:
            item.labels_cache = dict.fromkeys(labels, True)
        if payload:
            item.payload = payload
        return item

    return _make_item
