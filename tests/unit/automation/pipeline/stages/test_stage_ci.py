"""Behavior tests for loop-owned approval after CI observation."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.ci import (
    CI_POLL_STARTED_AT,
    DISCOVER,
    POLL,
    PUSH_WAIT,
    REBASE_PUSH_WAIT,
    REBASE_WAIT,
    CiStage,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_OPEN = {"state": "OPEN", "headRefOid": "abc123"}


def _item(make_work_item: Any, *, state: str = POLL) -> Any:
    item = make_work_item(stage=StageName.CI, pr=501, state=state)
    item.branch = "feature"
    item.worktree = "/tmp/worktree"
    item.payload["pr_review_skill_head"] = "abc123"
    return item


def test_no_checks_authorizes_after_in_loop_pr_review(make_ctx: Any, make_work_item: Any) -> None:
    """No GitHub Actions configuration is a successful CI observation."""
    github = FakeStageGitHub(checks=[], pr_state=_OPEN)

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.ADVANCE, "no_checks")
    assert ("mark_pr_implementation_go", (501,)) in github.mutation_log


def test_green_authorizes_after_in_loop_pr_review(make_ctx: Any, make_work_item: Any) -> None:
    """Observed green CI is the other path that writes implementation-go."""
    github = FakeStageGitHub(
        checks=[{"status": "completed", "conclusion": "success", "required": True}],
        pr_state=_OPEN,
    )

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.ADVANCE, "green")
    assert ("mark_pr_implementation_go", (501,)) in github.mutation_log


def test_non_required_failure_does_not_block_required_green(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The CI adapter filters optional workflows before this stage receives them."""
    github = FakeStageGitHub(
        checks=[
            {"status": "completed", "conclusion": "success", "required": True},
            {"status": "completed", "conclusion": "failure", "required": False},
        ],
        pr_state=_OPEN,
    )

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.ADVANCE, "green")


def test_label_write_failure_finishes_closed(make_ctx: Any, make_work_item: Any) -> None:
    """A failed loop-owned label mutation cannot be treated as approval."""

    class FailingLabelGitHub(FakeStageGitHub):
        def mark_pr_implementation_go(self, pr_number: int) -> None:
            del pr_number
            raise RuntimeError("label write failed")

    github = FailingLabelGitHub(checks=[], pr_state=_OPEN)

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "implementation_go_label_failed")


def test_head_drift_after_label_revokes_eligibility(make_ctx: Any, make_work_item: Any) -> None:
    """A push racing the label write is contained before merge-wait sees it."""

    class RacingLabelGitHub(FakeStageGitHub):
        def mark_pr_implementation_go(self, pr_number: int) -> None:
            super().mark_pr_implementation_go(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "new-head"}

    github = RacingLabelGitHub(checks=[], pr_state=_OPEN)

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "review_stale")
    assert ("gh_issue_remove_labels", (501, ("state:implementation-go",))) in github.mutation_log
    assert ("defer_auto_merge", (501,)) in github.mutation_log


def test_pending_ci_does_not_authorize(make_ctx: Any, make_work_item: Any) -> None:
    """A pending observed check parks the loop without applying the label."""
    github = FakeStageGitHub(
        checks=[{"status": "in_progress", "conclusion": None, "required": True}],
        pr_state=_OPEN,
    )

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.RETRY, "ci_pending")
    assert not any(action == "mark_pr_implementation_go" for action, _ in github.mutation_log)


def test_pending_ci_uses_bounded_backoff(make_ctx: Any, make_work_item: Any) -> None:
    """Pending required checks timer-park without granting loop approval."""
    github = FakeStageGitHub(
        checks=[{"status": "in_progress", "conclusion": None, "required": True}],
        pr_state=_OPEN,
    )
    item = _item(make_work_item)
    stage = CiStage()

    for delay in (1, 2, 4):
        assert stage.step(item, make_ctx(github=github)) == StageOutcome(
            Disposition.RETRY, "ci_pending"
        )
        assert item.payload["retry_delay_s"] == delay


def test_pending_ci_times_out_by_elapsed_wall_clock(make_ctx: Any, make_work_item: Any) -> None:
    """A permanently pending check cannot park the loop forever."""
    github = FakeStageGitHub(
        checks=[{"status": "in_progress", "conclusion": None, "required": True}],
        pr_state=_OPEN,
    )
    item = _item(make_work_item)
    item.payload[CI_POLL_STARTED_AT] = 0.0
    ctx = make_ctx(github=github)
    ctx.config.poll_max_wait = 10

    assert CiStage().step(item, ctx) == StageOutcome(Disposition.FINISH_FAIL, "timeout")


def test_failing_ci_routes_to_one_bounded_fix_attempt(make_ctx: Any, make_work_item: Any) -> None:
    """A required failure enters the existing repair leg, never approval."""
    github = FakeStageGitHub(
        checks=[{"status": "completed", "conclusion": "failure", "required": True}],
        pr_state=_OPEN,
    )
    item = _item(make_work_item)

    assert CiStage().step(item, make_ctx(github=github)) == Continue(next_state="FIX_WAIT")
    item.attempts["ci_fix"] = 1
    assert CiStage().step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FAIL_BACK, "fix_exhausted"
    )


def test_no_commit_fix_escalates_only_once(make_ctx: Any, make_work_item: Any) -> None:
    """An agent claiming a fix without a commit gets exactly one retry."""
    github = FakeStageGitHub(
        checks=[{"status": "completed", "conclusion": "failure", "required": True}],
        pr_state=_OPEN,
    )
    item = _item(make_work_item)
    item.payload["push_no_commit"] = True
    stage = CiStage()

    assert stage.step(item, make_ctx(github=github)) == Continue(next_state="FIX_WAIT")
    assert item.payload["force_engagement"] is True
    item.payload["push_no_commit"] = True
    assert stage.step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FAIL_BACK, "fix_exhausted"
    )


def test_stale_reviewed_head_reenters_review_without_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The loop never labels a head that its PR-review skill did not inspect."""
    github = FakeStageGitHub(checks=[], pr_state={"state": "OPEN", "headRefOid": "new"})

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "review_stale")
    assert not any(action == "mark_pr_implementation_go" for action, _ in github.mutation_log)


def test_pushed_ci_fix_reenters_review_before_label(make_ctx: Any, make_work_item: Any) -> None:
    """A repair changes the reviewed code and therefore requires a fresh review pass."""
    github = FakeStageGitHub(checks=[], pr_state=_OPEN)
    stage = CiStage()
    item = _item(make_work_item, state=PUSH_WAIT)
    stage.on_job_done(item, JobResult(ok=True, value=True), make_ctx(github=github))
    item.state = POLL

    result = stage.step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "review_stale")


def test_discover_requires_an_in_memory_pr_review_handoff(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restart cannot infer approval from a durable label or check state."""
    github = FakeStageGitHub(pr_state=_OPEN)
    item = _item(make_work_item, state=DISCOVER)
    item.payload.pop("pr_review_skill_head")

    result = CiStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert ("defer_auto_merge", (501,)) in github.mutation_log


def test_discover_preserves_the_current_reviewed_branch_and_base(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The check observer works on the real PR branch after the review handoff."""
    github = FakeStageGitHub(
        pr_head_branch="feature/current",
        pr_state={**_OPEN, "baseRefName": "release"},
    )
    item = _item(make_work_item, state=DISCOVER)
    item.branch = ""

    result = CiStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=REBASE_WAIT)
    assert item.branch == "feature/current"
    assert item.payload["base_branch"] == "release"


def test_mechanical_rebase_requires_a_fresh_review_before_labeling(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Any pushed code change invalidates the in-memory PR-review handoff."""
    github = FakeStageGitHub(checks=[], pr_state=_OPEN)
    stage = CiStage()
    item = _item(make_work_item, state=REBASE_PUSH_WAIT)

    stage.on_job_done(item, JobResult(ok=True, value=True), make_ctx(github=github))
    item.state = POLL
    result = stage.step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "review_stale")
    assert not any(action == "mark_pr_implementation_go" for action, _ in github.mutation_log)
