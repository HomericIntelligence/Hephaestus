"""Tests for label-only merge-wait authorization."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import ARM, POLL, MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ArmingGitHub(FakeStageGitHub):
    def __init__(self, *, labels: tuple[bool, bool]) -> None:
        super().__init__(
            pr_impl_state=labels,
            pr_state={"state": "OPEN", "headRefOid": "a" * 40},
        )

    def arm_auto_merge(self, pr_number: int, expected_head_sha: str) -> None:
        super().arm_auto_merge(pr_number, expected_head_sha)
        self._pr_state = {
            "state": "OPEN",
            "headRefOid": expected_head_sha,
            "autoMergeRequest": {"enabledAt": "now"},
        }


def test_implementation_go_label_arms_without_an_external_gate(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Merge-wait consumes the loop-owned label and no external artifact."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert github.arming_records[item.issue] == (12, "a" * 40)
    assert item.armed is True
    assert item.payload["merge_wait_head"] == "a" * 40


def test_missing_implementation_go_is_contained(make_ctx: Any, make_work_item: Any) -> None:
    """No label means no arm; the item returns to the in-loop review path."""
    github = _ArmingGitHub(labels=(False, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)


def test_stale_in_memory_review_handoff_is_contained_before_arm(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A push between strict-review's label write and ARM cannot be armed."""

    class StaleHandoffGitHub(_ArmingGitHub):
        def __init__(self) -> None:
            super().__init__(labels=(True, False))
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}

        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None}

    github = StaleHandoffGitHub()
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
        payload={"pr_review_skill_head": "a" * 40},
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "review_stale")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert any(action == "gh_issue_remove_labels" for action, _ in github.mutation_log)


def test_armed_labeled_pr_polls_without_rechecking_external_state(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Once armed, the loop waits on GitHub while the loop-owned label remains."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)
    stage = MergeWaitStage()

    assert isinstance(stage.step(item, make_ctx(github=github)), Continue)
    item.state = POLL
    result = stage.step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.RETRY, "merge_pending")


def test_post_arm_head_drift_revokes_auto_merge(make_ctx: Any, make_work_item: Any) -> None:
    """A push after arming cannot merge code the skill did not review."""

    class DriftingGitHub(_ArmingGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            super().defer_auto_merge(pr_number)
            self._pr_state = {
                "state": "OPEN",
                "headRefOid": "b" * 40,
                "autoMergeRequest": None,
            }

    github = DriftingGitHub(labels=(True, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)
    stage = MergeWaitStage()

    assert isinstance(stage.step(item, make_ctx(github=github)), Continue)
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "now"},
    }
    item.state = POLL

    result = stage.step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert item.armed is False


def test_arm_failure_is_contained_before_finishing(make_ctx: Any, make_work_item: Any) -> None:
    """An ambiguous GitHub arm error cannot leave auto-merge enabled."""

    class FailingArmGitHub(_ArmingGitHub):
        def arm_auto_merge(self, pr_number: int, expected_head_sha: str) -> None:
            del pr_number, expected_head_sha
            raise RuntimeError("transport failure")

    github = FailingArmGitHub(labels=(True, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "arm_failed")
    assert ("defer_auto_merge", (12,)) in github.mutation_log


def test_closed_pr_finishes_failed_after_arm(make_ctx: Any, make_work_item: Any) -> None:
    """A closed-but-unmerged PR cannot be reported as a loop success."""
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.payload["merge_wait_started_at"] = 1.0
    github = FakeStageGitHub(pr_state={"state": "CLOSED"})

    assert MergeWaitStage().step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FINISH_FAIL, "closed"
    )


def test_merged_pr_completes_without_learn_when_disabled(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The post-merge learn feature does not obscure a completed merge."""
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.payload["merge_wait_started_at"] = 1.0
    github = FakeStageGitHub(pr_state={"state": "MERGED"})
    ctx = make_ctx(github=github)
    ctx.config.enable_learn = False

    assert MergeWaitStage().step(item, ctx) == StageOutcome(Disposition.FINISH_PASS, "merged")
