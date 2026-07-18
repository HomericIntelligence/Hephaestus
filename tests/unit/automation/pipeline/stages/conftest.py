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

from hephaestus.automation.pipeline.events import StageEvent
from hephaestus.automation.pipeline.routing import ROUTES, StageName
from hephaestus.automation.pipeline.stages import (
    StageContext,
    StageGitHub,
    StrictReviewEvidence,
)
from hephaestus.automation.pipeline.stages.implementation import PRE_PR_TEST_ARGV
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
        issue_title: str = "A task",
        issue_body: str = "",
        merged_pr: int | None = None,
        open_pr: int | None = None,
        pr_issue: int | None = None,
        has_plan: bool = False,
        pr_head_branch: str | None = None,
        pr_impl_state: tuple[bool, bool] = (False, False),
        unresolved: list[tuple[int, int]] | None = None,
        by_severity: list[tuple[int, int, int]] | None = None,
        pr_state: dict[str, Any] | None = None,
        learn_terminal: bool = False,
        resolve_count: int = 0,
        strict_evidence: StrictReviewEvidence | None = None,
    ) -> None:
        """Initialize the fake with canned read answers.

        Args:
            labels: Seed labels applied to any issue on first read/mutation.
            issue_title: Canned issue title returned by gh_issue_json.
            issue_body: Canned issue body returned by gh_issue_json.
            merged_pr: Canned answer for find_merged_closing_pr.
            open_pr: Canned answer for find_pr_for_issue.
            pr_issue: Canned answer for find_issue_for_pr.
            has_plan: Canned answer for has_existing_plan.
            pr_head_branch: Canned answer for get_pr_head_branch.
            pr_impl_state: Canned (has_go, has_no_go) answer for
                pr_has_implementation_state_label.
            unresolved: FIFO of (automation, human) answers for
                count_unresolved_threads — consumed one per call, last
                entry repeating (lets tests script a decreasing /
                plateauing thread count for the #1554 progress rule).
            by_severity: FIFO of (blocking, minor, human) answers for
                count_unresolved_threads_by_severity (#1856); defaults to
                deriving from unresolved (legacy: all automation = blocking).
            pr_state: Canned answer for gh_pr_state (merge_wait's single
                PR-state read); ``None`` mirrors a transient read failure.
            learn_terminal: Seed answer for drive_green_learn_terminal —
                True mirrors an issue whose post-merge /learn already ran
                terminally (the #848 dedupe record).
            resolve_count: Canned return count for resolve_automation_threads.
            strict_evidence: Canned bounded evidence for a strict-review job.

        """
        super().__init__()
        self._seed_labels = list(labels or [])
        self._issue_title = issue_title
        self._issue_body = issue_body
        self._merged_pr = merged_pr
        self._open_pr = open_pr
        self._pr_issue = pr_issue
        self._has_plan = has_plan
        self._pr_head_branch = pr_head_branch
        self._pr_impl_state = pr_impl_state
        self._unresolved: list[tuple[int, int]] = list(unresolved or [(0, 0)])
        self._by_severity = (
            list(by_severity)
            if by_severity is not None
            else [(a, 0, h) for (a, h) in self._unresolved]  # legacy: all automation = blocking
        )
        self._pr_state = pr_state
        self._learn_terminal = learn_terminal
        self._resolve_count = resolve_count
        self._strict_evidence = strict_evidence
        self.strict_evidence_calls: list[tuple[int, str, int]] = []
        self.arming_records: dict[int, tuple[int, str]] = {}
        self.confirmed_arming_records: set[int] = set()
        self.learn_results: dict[int, bool] = {}
        self.learn_claims: set[int] = set()

    def _issue_labels(self, issue_number: int) -> set[str]:
        """Return the issue's label set, seeding it on first access."""
        if issue_number not in self.labels:
            self.labels[issue_number] = set(self._seed_labels)
        return self.labels[issue_number]

    # -- read surface used by the stages -----------------------------------
    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Mirror github_api.issues.gh_issue_json (issue context plus labels)."""
        return {
            "number": issue_number,
            "title": self._issue_title,
            "body": self._issue_body,
            "labels": [{"name": name} for name in sorted(self._issue_labels(issue_number))],
        }

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

    def count_unresolved_threads_by_severity(self, pr_number: int) -> tuple[int, int, int]:
        """FIFO of scripted (blocking, minor, human) answers (#1856)."""
        if len(self._by_severity) > 1:
            return self._by_severity.pop(0)
        return self._by_severity[0]

    def resolve_automation_threads(self, pr_number: int) -> int:
        self._log("resolve_automation_threads", pr_number)
        return self._resolve_count

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

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomic add+remove recorded as ONE mutation (mirrors gh issue edit)."""
        labels = self._issue_labels(issue_number)
        labels.update(add)
        labels.difference_update(remove)
        self._log("edit_labels", issue_number, tuple(add), tuple(remove))

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

    def upsert_pr_comment(self, pr_number: int, marker_prefix: str, body: str) -> bool:
        """Mirror the coordinator PR-comment upsert (delegates to issue comments)."""
        self.gh_issue_upsert_comment(pr_number, marker_prefix, body)
        return True

    def arm_auto_merge(self, pr_number: int, expected_head_sha: str) -> None:
        """Mirror pr_manager.enable_auto_merge_after_implementation_go."""
        self._log("arm_auto_merge", pr_number, expected_head_sha)

    def strict_review_evidence(
        self, pr_number: int, head_sha: str, issue_number: int
    ) -> StrictReviewEvidence | None:
        """Return canned evidence only when it remains for the requested head."""
        self.strict_evidence_calls.append((pr_number, head_sha, issue_number))
        if self._strict_evidence is None or self._strict_evidence.head_sha != head_sha:
            return None
        return self._strict_evidence

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Mirror ci_driver.CIDriver._gh_pr_state (canned answer)."""
        del pr_number  # single canned answer; not per-PR keyed
        return self._pr_state

    def arm_drive_green(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        """Mirror ci_driver.CIDriver._arm_drive_green (records the arming record)."""
        self.arming_records[issue_number] = (pr_number, head_sha)
        self.confirmed_arming_records.discard(issue_number)
        self._log("arm_drive_green", issue_number, pr_number, head_sha)

    def confirm_drive_green_arm(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        """Mirror the read-back-confirmed durable drive-green arm transition."""
        if self.arming_records.get(issue_number) != (pr_number, head_sha):
            raise RuntimeError("arm record is missing or mismatched")
        self.confirmed_arming_records.add(issue_number)
        self._log("confirm_drive_green_arm", issue_number, pr_number, head_sha)

    def drive_green_arm_confirmed(self, issue_number: int, pr_number: int) -> bool:
        """Return whether the canned arming record was confirmed remotely."""
        record = self.arming_records.get(issue_number)
        return (
            record is not None
            and record[0] == pr_number
            and issue_number in self.confirmed_arming_records
        )

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Mirror ci_driver._learn_record_terminal over the arming record.

        Terminal when seeded so (``learn_terminal=True``) or once
        :meth:`mark_drive_green_learn_result` recorded an outcome — the
        exactly-once /learn read-back (#848).
        """
        return self._learn_terminal or issue_number in self.learn_results

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Mirror a durable pre-dispatch /learn claim."""
        return issue_number in self.learn_claims

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Record the pre-dispatch claim unless another run already owns it."""
        if self.drive_green_learn_terminal(issue_number) or issue_number in self.learn_claims:
            return False
        self.learn_claims.add(issue_number)
        self._log("claim_drive_green_learn", issue_number, pr_number)
        return True

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Mirror post_merge_processor.mark_drive_green_learn_result [durable]."""
        self.learn_results[issue_number] = succeeded
        self.learn_claims.discard(issue_number)
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
        self.pre_pr_test_argv = PRE_PR_TEST_ARGV


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
        budget_fn: Callable[[str], int] | None = None,
        event_fn: Callable[[StageEvent], None] | None = None,
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
            budget_fn=budget_fn if budget_fn is not None else _budget_fn,
            event_fn=event_fn,
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
