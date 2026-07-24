"""Tests for the intentionally simple label-only merge-wait stage."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import ARM, POLL, MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _ArmingGitHub(FakeStageGitHub):
    def __init__(self, *, labels: tuple[bool, bool] = (True, False)) -> None:
        super().__init__(
            pr_impl_state=labels,
            pr_state={"state": "OPEN", "headRefOid": "a" * 40},
        )


def test_loop_owned_implementation_go_arms_once_without_durable_recovery(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A current-run GO issues one arm request and does not create an arm record."""
    github = _ArmingGitHub()
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state=POLL)
    assert ("arm_auto_merge", (12, "a" * 40)) in github.mutation_log
    assert not any(action == "arm_drive_green" for action, _ in github.mutation_log)
    assert item.armed is True


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


def test_arm_error_is_warned_and_stops_without_cross_run_reconciliation(
    make_ctx: Any, make_work_item: Any, caplog: Any
) -> None:
    """An ambiguous arm result is handed to an operator instead of being retried."""

    class _FailingArmGitHub(_ArmingGitHub):
        def arm_auto_merge(self, pr_number: int, expected_head_sha: str) -> None:
            del pr_number, expected_head_sha
            raise RuntimeError("transport failure")

    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=_FailingArmGitHub()))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_arm_failed")
    assert "operator" in caplog.text


def test_lost_own_arm_is_warned_and_stops_without_retrying(
    make_ctx: Any, make_work_item: Any, caplog: Any
) -> None:
    """A later external disarm is not retried or routed through review again."""
    github = _ArmingGitHub()
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    github._pr_state = {"state": "OPEN", "headRefOid": "a" * 40}

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_no_longer_armed")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert "operator" in caplog.text


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
    assert "armed outside this run" in caplog.text


def test_own_arm_poll_stops_at_the_configured_merge_budget(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Only pending polls of this run's arm consume the merge budget."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    ctx = make_ctx(github=github, budget_fn=lambda name: 2 if name == "merge" else 1)

    first = MergeWaitStage().step(item, ctx)

    assert first == StageOutcome(Disposition.RETRY, "merge_pending")
    assert item.attempts["merge"] == 1
    assert item.payload.pop("retry_delay_s") == 30

    second = MergeWaitStage().step(item, ctx)

    assert second == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_exhausted")
    assert item.attempts["merge"] == 2
    assert "retry_delay_s" not in item.payload


def test_own_arm_poll_honors_a_single_poll_merge_budget(make_ctx: Any, make_work_item: Any) -> None:
    """A one-poll override finishes after the first unresolved own-arm poll."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    ctx = make_ctx(github=github, budget_fn=lambda _name: 1)

    result = MergeWaitStage().step(item, ctx)

    assert result == StageOutcome(Disposition.FINISH_FAIL, "merge_wait_exhausted")
    assert item.attempts["merge"] == 1
    assert "retry_delay_s" not in item.payload


def test_missing_implementation_go_returns_to_review(make_ctx: Any, make_work_item: Any) -> None:
    """A normal absent approval still returns to the loop-owned review stage."""
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=ARM)

    result = MergeWaitStage().step(item, make_ctx(github=_ArmingGitHub(labels=(False, False))))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")


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
