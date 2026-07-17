"""Behavior tests for loop-owned approval after CI observation."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.ci import (
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


def test_pending_ci_does_not_authorize(make_ctx: Any, make_work_item: Any) -> None:
    """A pending observed check parks the loop without applying the label."""
    github = FakeStageGitHub(
        checks=[{"status": "in_progress", "conclusion": None, "required": True}],
        pr_state=_OPEN,
    )

    result = CiStage().step(_item(make_work_item), make_ctx(github=github))

    assert result == StageOutcome(Disposition.RETRY, "ci_pending")
    assert not any(action == "mark_pr_implementation_go" for action, _ in github.mutation_log)


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
