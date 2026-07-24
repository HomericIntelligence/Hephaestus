"""Tests for the intentionally simple label-only merge-wait stage."""

from __future__ import annotations

from typing import Any

from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import ARM, POLL, MergeWaitStage
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_OWNED_ARM_HEAD_KEY = "merge_wait_expected_head"


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
    assert item.payload[_OWNED_ARM_HEAD_KEY] == "a" * 40


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
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40
    github._pr_state = {"state": "OPEN", "headRefOid": "a" * 40}

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_no_longer_armed")
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)
    assert "operator" in caplog.text


def test_lost_own_arm_with_head_drift_stops_without_containment(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A vanished arm is never treated as authority to revoke a later approval."""
    github = _ArmingGitHub()
    github._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_no_longer_armed")
    assert github.mutation_log == []
    assert item.armed is True
    assert item.payload[_OWNED_ARM_HEAD_KEY] == "a" * 40


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


def test_poll_external_auto_merge_with_head_drift_remains_unmodified(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A changed head never grants merge-wait ownership of an external arm."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "elsewhere"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    assert github.mutation_log == []
    assert item.armed is False
    assert item.payload[_OWNED_ARM_HEAD_KEY] == "a" * 40


def test_owned_arm_head_drift_is_contained_before_fresh_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A changed owned head disables the arm, revokes GO, then re-reviews."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "head_drift")
    assert github.mutation_log == [
        ("defer_auto_merge", (12,)),
        ("mark_pr_implementation_no_go", (12,)),
    ]
    assert item.armed is False
    assert _OWNED_ARM_HEAD_KEY not in item.payload
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_owned_arm_missing_recorded_head_is_contained_before_fresh_review(
    make_ctx: Any, make_work_item: Any
) -> None:
    """An owned arm without its reviewed head fails closed instead of polling."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "head_drift")
    assert github.mutation_log == [
        ("defer_auto_merge", (12,)),
        ("mark_pr_implementation_no_go", (12,)),
    ]
    assert item.armed is False


def test_owned_arm_head_drift_stops_when_deferral_cannot_be_verified(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Fresh review cannot begin until a drifted owned arm is verified disabled."""

    class _FailingDeferGitHub(_ArmingGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            self._log("defer_auto_merge", pr_number)
            raise RuntimeError("transport failure")

    github = _FailingDeferGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "head_drift_defer_failed")
    assert github.mutation_log == [("defer_auto_merge", (12,))]
    assert item.armed is True
    assert item.payload[_OWNED_ARM_HEAD_KEY] == "a" * 40


def test_owned_arm_head_drift_stops_when_approval_revocation_fails(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A disabled arm is still unsafe to re-review when stale GO remains."""

    class _FailingRevokeGitHub(_ArmingGitHub):
        def mark_pr_implementation_no_go(self, pr_number: int) -> None:
            self._log("mark_pr_implementation_no_go", pr_number)
            raise RuntimeError("label mutation failure")

    github = _FailingRevokeGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "head_drift_revoke_failed")
    assert github.mutation_log == [
        ("defer_auto_merge", (12,)),
        ("mark_pr_implementation_no_go", (12,)),
    ]
    assert item.armed is True
    assert item.payload[_OWNED_ARM_HEAD_KEY] == "a" * 40


def test_head_drift_requires_a_fresh_review_before_a_new_arm(
    make_ctx: Any, make_work_item: Any
) -> None:
    """The old GO cannot re-arm the new head after containment."""
    github = _ArmingGitHub()
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "b" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40
    stage = MergeWaitStage()

    assert stage.step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FAIL_BACK, "head_drift"
    )

    github._pr_impl_state = (False, True)
    github._pr_state = {"state": "OPEN", "headRefOid": "b" * 40}
    item.state = ARM

    assert stage.step(item, make_ctx(github=github)) == StageOutcome(
        Disposition.FAIL_BACK, "not_implementation_go"
    )
    assert not any(action == "arm_auto_merge" for action, _ in github.mutation_log)


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
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40
    ctx = make_ctx(github=github, budget_fn=lambda name: 2 if name == "merge" else 1)

    first = MergeWaitStage().step(item, ctx)
    retry_delay_s = item.payload.pop("retry_delay_s")

    assert first == StageOutcome(Disposition.RETRY, "merge_pending")
    assert item.attempts["merge"] == 1
    assert retry_delay_s == 30

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
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40
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


def test_own_arm_without_approval_returns_to_review(make_ctx: Any, make_work_item: Any) -> None:
    """A lost approval on this run's arm is remediated by a fresh PR review."""
    github = _ArmingGitHub(labels=(False, False))
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FAIL_BACK, "not_implementation_go")
    assert github.mutation_log == [("defer_auto_merge", (12,))]
    assert item.armed is False
    assert item.attempts["merge"] == 0
    assert "retry_delay_s" not in item.payload


def test_own_arm_without_approval_stops_when_defer_fails(
    make_ctx: Any, make_work_item: Any
) -> None:
    """Review must not re-enter while this run's auto-merge arm remains live."""

    class _FailingDeferGitHub(_ArmingGitHub):
        def defer_auto_merge(self, pr_number: int) -> None:
            self._log("defer_auto_merge", pr_number)
            raise RuntimeError("transport failure")

    github = _FailingDeferGitHub(labels=(False, False))
    github._pr_state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "autoMergeRequest": {"enabledAt": "this-run"},
    }
    item = make_work_item(stage=StageName.MERGE_WAIT, pr=12, state=POLL)
    item.armed = True
    item.payload[_OWNED_ARM_HEAD_KEY] = "a" * 40

    result = MergeWaitStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, "auto_merge_defer_failed")
    assert github.mutation_log == [("defer_auto_merge", (12,))]
    assert item.armed is True
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
