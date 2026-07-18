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


def test_loop_owned_implementation_go_label_arms_without_an_external_gate(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Merge-wait consumes the loop-owned label without an external gate."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert github.arming_records[item.issue] == (12, "a" * 40)
    assert item.armed is True
    assert item.payload["merge_wait_head"] == "a" * 40


def test_missing_implementation_go_is_contained(make_ctx: Any, make_work_item: Any) -> None:
    """No label means no arm; the item returns to the in-loop review path."""
    github = _ArmingGitHub(labels=(False, False))
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)


def test_orphan_pr_is_contained_before_merge_wait_can_consume_go_label(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An unlinked direct PR may never turn a stale GO label into an arm."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(
        issue=None,
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

    result = MergeWaitStage().on_enter(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_orphan")
    assert ("defer_auto_merge", (12,)) in github.mutation_log
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)


def test_label_without_ephemeral_review_handoff_arms(make_ctx: Any, make_work_item: Any) -> None:
    """A persisted loop-owned label remains the merge authorization after restart."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert any(action == "arm_auto_merge" for action, _ in github.mutation_log)


def test_label_arms_even_when_previous_review_state_is_not_rehydrated(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restart does not turn ephemeral review state into another gate."""
    head = "a" * 40
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
        payload={
            "strict_review_attempt": 1,
            "strict_review_head": head,
            "strict_review_worktree": "/review/stale-strict-12",
            "strict_review_worktree_head": head,
        },
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert any(action == "arm_auto_merge" for action, _ in github.mutation_log)


def test_live_label_authorization_uses_the_current_pr_head(
    make_ctx: Any, make_work_item: Any
) -> None:
    """ARM uses the live head after the loop has applied its approval label."""
    github = _ArmingGitHub(labels=(True, False))
    github._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert isinstance(result, Continue)
    assert github.arming_records[item.issue] == (12, "b" * 40)
    assert not any(action == "gh_issue_remove_labels" for action, _ in github.mutation_log)


def test_recovered_arm_is_disarmed_before_rechecking_label_authorization(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A restart cannot leave a former arm live until ARM gets a queue turn."""
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state="ENTER",
        payload={"merge_wait_recovery": True},
    )
    github = FakeStageGitHub(pr_state={"state": "OPEN", "headRefOid": "a" * 40})

    assert MergeWaitStage().on_enter(item, make_ctx(github=github)) is None

    assert item.state == ARM
    assert github.mutation_log[0] == ("defer_auto_merge", (12,))


def test_armed_labeled_pr_polls_without_rechecking_external_state(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Once armed, the loop waits on GitHub while the loop-owned label remains."""
    github = _ArmingGitHub(labels=(True, False))
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )
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
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )
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
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
    )

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
