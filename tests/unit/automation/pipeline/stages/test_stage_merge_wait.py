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


def test_missing_implementation_go_is_contained(make_ctx: Any, make_work_item: Any) -> None:
    """No label means no arm; the item returns to the in-loop review path."""
    github = _ArmingGitHub(labels=(False, False))
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)


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
