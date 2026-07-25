"""Tests for the intentionally simple label-only merge-wait stage."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import ARM, POLL, MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ArmingGitHub(FakeStageGitHub):
    def __init__(self, *, labels: tuple[bool, bool] = (True, False)) -> None:
        super().__init__(
            pr_impl_state=labels,
            pr_state={"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
        )


def test_matching_reviewed_head_stands_down_without_creating_auto_merge(
    make_ctx: Any, make_work_item: Any
) -> None:
    """#2423 holds a reviewed PR safely pending the later normal merge path."""
    github = _ArmingGitHub()
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
        payload={"reviewed_pr_head_sha": "a" * 40},
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")
    assert github.mutation_log == []
    assert item.armed is False


def test_existing_auto_merge_is_blocked_and_left_to_the_operator(
    make_ctx: Any, make_work_item: Any, caplog: Any
) -> None:
    """An arm observed before this run never triggers recovery or re-arming."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "elsewhere"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert item.attempts["merge"] == 0
    assert "already armed" in caplog.text


def test_missing_reviewed_head_revokes_go_and_returns_to_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Restarted merge-wait has no durable review proof and must fail back."""
    github = _ArmingGitHub()
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "reviewed_head_missing")
    assert github.mutation_log == [("mark_pr_implementation_no_go", (12,))]


def test_poll_of_an_unarmed_pr_stands_by_without_retrying(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Merge wait no longer tracks or retries an auto-merge arm."""
    github = _ArmingGitHub()
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    github._pr_state = {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None}

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")
    assert github.mutation_log == []


def test_closed_pr_stops_without_mutating_or_consuming_merge_budget(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A closed-unmerged PR is terminal before merge-arm handling."""
    github = _ArmingGitHub()
    github._pr_state = {"state": "CLOSED"}
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "closed")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0


def test_unavailable_pr_state_stops_without_mutating_or_consuming_merge_budget(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An API/read failure is terminal before merge-arm handling."""
    github = _ArmingGitHub()
    github._pr_state = None
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0


def test_merge_wait_rejects_a_partial_unarmed_state_without_label_mutation(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A missing auto-merge field is ambiguous, not proof of an unarmed PR."""
    github = _ArmingGitHub()
    github._pr_state = {"state": "OPEN", "headRefOid": "a" * 40}
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
        payload={"reviewed_pr_head_sha": "a" * 40},
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
    assert github.mutation_log == []


def test_stale_proof_re_read_that_matches_stands_down_without_revoking_go(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A remote head moving back to the reviewed commit invalidates the stale decision."""

    class FlippingStateGitHub(_ArmingGitHub):
        def __init__(self) -> None:
            super().__init__()
            self._states = [
                {"state": "OPEN", "headRefOid": "b" * 40, "autoMergeRequest": None},
                {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
            ]

        def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
            del pr_number
            return self._states.pop(0)

    github = FlippingStateGitHub()
    item = make_work_item(
        stage=StageName.MERGE_WAIT,
        pr=12,
        state=ARM,
        payload={"reviewed_pr_head_sha": "a" * 40},
    )

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_standing_by")
    assert github.mutation_log == []


def test_poll_external_auto_merge_is_blocked_without_consuming_merge_budget(
    make_ctx: Any, make_work_item: Any, caplog: Any
) -> None:
    """A POLL restart never claims an arm it did not create."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "elsewhere"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload
    assert "already armed" in caplog.text


def test_poll_external_auto_merge_without_approval_remains_blocked(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An external arm remains operator-owned even after its approval changes."""
    github = _ArmingGitHub(labels=(False, False))
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "elsewhere"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_poll_treats_any_observed_arm_as_external(make_ctx: Any, make_work_item: Any) -> None:
    """No in-process arm ownership remains after #2423."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_poll_arm_does_not_consume_a_merge_budget(make_ctx: Any, make_work_item: Any) -> None:
    """An operator-owned arm is never retried by the pipeline."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    result = MergeWaitStage().step(item, make_ctx(github=github, budget_fn=lambda _name: 1))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_missing_implementation_go_returns_to_review(make_ctx: Any, make_work_item: Any) -> None:
    """A normal absent approval still returns to the loop-owned review stage."""
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=_ArmingGitHub(labels=(False, False))))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")


def test_external_arm_without_approval_remains_blocked(make_ctx: Any, make_work_item: Any) -> None:
    """External arms take priority over missing labels and receive no mutation."""
    github = _ArmingGitHub(labels=(False, False))
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_external_arm_never_invokes_the_removed_defer_path(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The safe operator stand-down has no disable/reconciliation operation."""
    github = _ArmingGitHub(labels=(False, False))
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_orphan_pr_finishes_without_mutating_an_operator_owned_arm(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Missing requirements context cannot arm or disarm a PR."""
    github = _ArmingGitHub()
    item = make_work_item(issue=None, stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().on_enter(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_orphan")
    assert github.mutation_log == []


def test_merged_pr_completes_without_learn_when_disabled(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A merged PR remains a normal terminal success."""
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    ctx = make_ctx(github=FakeStageGitHub(pr_state={"state": "MERGED"}))
    ctx.config.enable_learn = False

    assert MergeWaitStage().step(item, ctx) == StageOutcome(Disposition.FINISH_PASS, "merged")
