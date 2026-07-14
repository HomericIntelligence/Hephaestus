"""Tests for the merge-wait stage (doc section "7. merge_wait", issue #1816)."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.pipeline.jobs import AgentJob, GitJob, JobResult
from hephaestus.automation.pipeline.routing import ROUTES, Disposition, StageName
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.base import StrictReviewArtifact
from hephaestus.automation.pipeline.stages.merge_wait import (
    ARM,
    BLOCKED_ADDRESS_WAIT,
    BLOCKED_PUSH_WAIT,
    DIRTY_PUSH_WAIT,
    DIRTY_REBASE_WAIT,
    ENTER,
    LEARN_WAIT,
    MERGE_MAX_WAIT_ENV,
    MW_FINISH,
    POLL,
    MergeWaitStage,
    build_drive_green_learn_prompt,
)
from hephaestus.automation.state_labels import STATE_SKIP
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

# Sanity anchors: the reasons this suite exercises are ROUTES rows.
assert ROUTES[StageName.MERGE_WAIT].fail_routes["ci_red"] == StageName.CI
assert ROUTES[StageName.MERGE_WAIT].fail_routes["blocked_exhausted"] == StageName.PR_REVIEW
assert ROUTES[StageName.MERGE_WAIT].budgets["blocked_address"] == 2
assert ROUTES[StageName.MERGE_WAIT].budgets["rebase"] == 2
assert ROUTES[StageName.MERGE_WAIT].budgets["merge"] == 1

MERGED_STATE = {"state": "MERGED", "headRefOid": "abc123"}
CLOSED_STATE = {"state": "CLOSED"}
OPEN_STATE = {"state": "OPEN", "mergeStateStatus": "BEHIND", "headRefOid": "abc123"}
DIRTY_STATE = {
    "state": "OPEN",
    "mergeStateStatus": "DIRTY",
    "baseRefName": "develop",
    "headRefOid": "abc123",
}
BLOCKED_STATE = {"state": "OPEN", "mergeStateStatus": "BLOCKED", "headRefOid": "abc123"}


class _FifoStateGitHub(FakeStageGitHub):
    """FakeStageGitHub whose gh_pr_state pops a scripted FIFO (last repeats)."""

    def __init__(self, states: list[dict[str, Any] | None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._states = list(states)

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        del pr_number
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


class _ArmFailGitHub(FakeStageGitHub):
    """arm_auto_merge raises (auto-merge arming rejected)."""

    def arm_auto_merge(self, pr_number: int) -> None:
        raise RuntimeError("auto-merge rejected")


class _RecordFailGitHub(FakeStageGitHub):
    """arm_drive_green raises (arming-record write failed)."""

    def arm_drive_green(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        raise OSError("disk full")


class _MarkFailGitHub(FakeStageGitHub):
    """mark_drive_green_learn_result raises (learn-record write failed)."""

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        raise OSError("disk full")


def _drive(stage: Any, item: Any, ctx: Any, pool: FakeWorkerPool, max_steps: int = 80) -> Any:
    """Drive a stage through the canonical FakeWorkerPool until an outcome."""
    entry = stage.on_enter(item, ctx)
    if entry is not None:
        return entry
    for _ in range(max_steps):
        result = stage.step(item, ctx)
        if isinstance(result, Continue):
            item.state = result.next_state
            continue
        if isinstance(result, JobRequest):
            pool.submit(result.job, result.on_done_state)  # type: ignore[arg-type]
            _handle, job_result = pool.completion_q.get_nowait()
            assert not job_result.interrupted  # on_job_done contract precondition
            stage.on_job_done(item, job_result, ctx)
            item.state = result.on_done_state
            continue
        return result
    raise AssertionError("stage driver did not terminate")


def _item(make_work_item: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("stage", StageName.MERGE_WAIT)
    kwargs.setdefault("pr", 601)
    item = make_work_item(**kwargs)
    item.branch = "1-auto-impl"
    item.worktree = "/tmp/wt/1"
    return item


def _armed_item(make_work_item: Any, *, with_anchor: bool = True, **kwargs: Any) -> Any:
    kwargs.setdefault("state", POLL)
    item = _item(make_work_item, **kwargs)
    item.armed = True
    if with_anchor:
        item.payload["merge_wait_started_at"] = 1000.0
    return item


class TestMergeWaitArm:
    """ARM: durable arming record BEFORE POLL, idempotent, failure terminals."""

    def test_arm_writes_record_before_poll_and_learn_marks_after(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Durable-order oracle: arm_auto_merge -> arm_drive_green -> learn mark.

        Mutation probe (b): swapping the arming-record write past POLL would
        break this exact mutation_log order (the record must exist before
        the first POLL classification can dispatch anything). PR starts OPEN
        (so PREPARE/ARM/CONFIRM run) and turns MERGED by POLL time.
        """

        class _MergesAfterArmGitHub(FakeStageGitHub):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._calls = 0

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                self._calls += 1
                if self._calls == 1:  # PREPARE
                    return OPEN_STATE
                if self._calls == 2:  # CONFIRM
                    return {**OPEN_STATE, "autoMergeRequest": {"enabledBy": {}}}
                return MERGED_STATE  # POLL

        stage = MergeWaitStage()
        github = _MergesAfterArmGitHub(
            strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO"),
        )
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        names = [name for name, _ in github.mutation_log]
        assert names == ["arm_auto_merge", "arm_drive_green", "mark_drive_green_learn_result"]
        assert github.arming_records[1] == (601, "abc123")
        # The learn session ran (once) and its result was durably marked.
        assert github.learn_results[1] is True
        learn_jobs = [h.job for h in pool.submitted if isinstance(h.job, AgentJob)]
        assert len(learn_jobs) == 1
        assert learn_jobs[0].descr == "drive_green_learn"

    def test_arming_record_is_durable_before_the_first_poll_park(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The arming record exists by the time the first POLL merely parks.

        Mutation probe (b), crash-safety form: a mutant that defers the
        ``arm_drive_green`` write past POLL (e.g. into the MERGED leg) leaves
        a parked-PENDING item armed with NO durable record — a crash there
        would lose the /learn dedupe key. The walk parks on PENDING and the
        record must already be on disk (in the fake: recorded+logged).
        """
        stage = MergeWaitStage()
        github = FakeStageGitHub(
            pr_state=OPEN_STATE,
            strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO"),
        )
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.RETRY, "merge_pending")
        names = [name for name, _ in github.mutation_log]
        assert names == ["arm_auto_merge", "arm_drive_green"]
        assert github.arming_records[1] == (601, "abc123")
        assert item.armed is True

    def test_arm_is_idempotent_when_already_armed(self, make_ctx: Any, make_work_item: Any) -> None:
        """An armed item skips straight to POLL with zero mutations."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)
        item.armed = True

        result = stage.step(item, ctx)

        assert result == Continue(next_state=POLL)
        assert github.mutation_log == []

    def test_dry_run_never_arms(self, make_ctx: Any, make_work_item: Any) -> None:
        """Dry-run proceeds to POLL without any durable write."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github, dry_run=True)
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == Continue(next_state=POLL)
        assert github.mutation_log == []
        assert item.armed is False

    def test_no_strict_artifact_fails_back_strict_gate_unavailable(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PREPARE with no valid head-bound strict-GO artifact refuses to arm (#2055)."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE, strict_artifact=None)
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "strict_gate_unavailable")
        assert item.armed is False
        assert ("defer_auto_merge", (601,)) in github.mutation_log
        assert ("arm_auto_merge", (601,)) not in github.mutation_log

    def test_nogo_artifact_never_authorizes_arming(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A validly-authenticated NOGO artifact must never authorize a merge."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(
            pr_state=OPEN_STATE,
            strict_artifact=StrictReviewArtifact(is_go=False, head_sha="abc123", verdict="NOGO"),
        )
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "strict_gate_unavailable")
        assert item.armed is False

    def test_stale_artifact_head_mismatch_refuses_to_arm(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """FakeStageGitHub's canned artifact is queried with the LIVE head."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(
            pr_state=OPEN_STATE,  # headRefOid="abc123"
            strict_artifact=None,  # simulates the real adapter rejecting a stale-head artifact
        )
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)

        stage.step(item, ctx)

        assert github.strict_artifact_calls == [(601, "abc123")]

    def test_arm_failure_finishes_failed(self, make_ctx: Any, make_work_item: Any) -> None:
        """A rejected auto-merge arm is terminal (legacy auto-merge-failed)."""
        stage = MergeWaitStage()
        ctx = make_ctx(
            github=_ArmFailGitHub(
                pr_state=OPEN_STATE,
                strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO"),
            )
        )
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "arm_failed")
        assert item.armed is False

    def test_arm_failure_reconciles_already_merged_race(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """AC6: arm_auto_merge fails because the PR merged mid-call — reconciled."""

        class _RaceGitHub(FakeStageGitHub):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._calls = 0

            def arm_auto_merge(self, pr_number: int) -> None:
                raise RuntimeError("already merged by someone else")

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                self._calls += 1
                if self._calls == 1:
                    return OPEN_STATE
                return MERGED_STATE

        stage = MergeWaitStage()
        github = _RaceGitHub(
            strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO")
        )
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_confirm_time_race_reconciles_already_merged(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """AC6: the PR merges between a successful arm and the CONFIRM readback."""

        class _ConfirmRaceGitHub(FakeStageGitHub):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._calls = 0

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                self._calls += 1
                if self._calls == 1:
                    return OPEN_STATE  # PREPARE
                return MERGED_STATE  # CONFIRM sees the race

        stage = MergeWaitStage()
        github = _ConfirmRaceGitHub(
            strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO")
        )
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _item(make_work_item, state="")

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert ("arm_auto_merge", (601,)) in github.mutation_log

    def test_confirm_no_auto_merge_request_is_verified_disable(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """CONFIRM finds no autoMergeRequest -> verified disable, arm_confirm_failed."""

        class _NoConfirmGitHub(FakeStageGitHub):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._calls = 0

            def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
                self._calls += 1
                if self._calls == 1:
                    return OPEN_STATE  # PREPARE
                return {**OPEN_STATE, "autoMergeRequest": None}  # CONFIRM: not armed

        stage = MergeWaitStage()
        github = _NoConfirmGitHub(
            strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO")
        )
        ctx = make_ctx(github=github)
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "arm_confirm_failed")
        assert item.armed is False
        assert ("defer_auto_merge", (601,)) in github.mutation_log

    def test_record_failure_never_flips_armed(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed arming-record write must not leave the item armed.

        An armed PR with no durable dedupe record could double-fire /learn
        after a crash — the stage stops instead.
        """
        stage = MergeWaitStage()
        ctx = make_ctx(
            github=_RecordFailGitHub(
                pr_state=OPEN_STATE,
                strict_artifact=StrictReviewArtifact(is_go=True, head_sha="abc123", verdict="GO"),
            )
        )
        item = _item(make_work_item, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "arm_record_failed")
        assert item.armed is False

    def test_arm_without_pr_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """No PR on the item: nothing to arm."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _item(make_work_item, pr=None, state=ARM)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "no_pr")

    def test_wall_clock_anchor_stamped_once(self, make_ctx: Any, make_work_item: Any) -> None:
        """merge_wait_started_at is stamped on first ARM and never re-stamped."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=OPEN_STATE))
        item = _item(make_work_item, state=ARM)

        stage.step(item, ctx)
        first = item.payload["merge_wait_started_at"]
        item.state = ARM  # simulate a re-entry
        stage.step(item, ctx)

        assert item.payload["merge_wait_started_at"] == first

    def test_on_enter_and_dispatch(self, make_ctx: Any, make_work_item: Any) -> None:
        """on_enter initializes ENTER; ENTER routes to ARM; unknown state stops."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _item(make_work_item, state="")

        assert stage.on_enter(item, ctx) is None
        assert item.state == ENTER
        assert stage.step(item, ctx) == Continue(next_state=ARM)

        item.state = "BOGUS"
        result = stage.step(item, ctx)
        assert isinstance(result, StageOutcome)
        assert result.disposition == Disposition.FINISH_FAIL


class TestMergeWaitPoll:
    """POLL: classify_pr_merge_state wiring and terminal routing."""

    def test_closed_finishes_failed(self, make_ctx: Any, make_work_item: Any) -> None:
        """CLOSED-not-merged is the doc's finished(fail) terminal."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=CLOSED_STATE))
        item = _armed_item(make_work_item)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_FAIL, "closed")

    def test_failing_fails_back_ci_red(self, make_ctx: Any, make_work_item: Any) -> None:
        """A fixable red required check regresses to the ci stage."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=OPEN_STATE, failing_checks=["lint"]))
        item = _armed_item(make_work_item)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FAIL_BACK, "ci_red")

    def test_policy_only_failure_keeps_waiting(self, make_ctx: Any, make_work_item: Any) -> None:
        """auto-merge-policy alone is not fixable red: PENDING park (legacy)."""
        stage = MergeWaitStage()
        ctx = make_ctx(
            github=FakeStageGitHub(pr_state=BLOCKED_STATE, failing_checks=["auto-merge-policy"])
        )
        item = _armed_item(make_work_item)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "merge_pending")

    def test_blocked_with_pending_checks_is_pending(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """BLOCKED while checks are in flight parks instead of addressing."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=BLOCKED_STATE, pending_checks=["pytest"]))
        item = _armed_item(make_work_item)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.RETRY, "merge_pending")

    def test_pending_backoff_is_exponential_in_payload(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """PENDING parks with the legacy min(2**n, 60) delay in the payload."""
        stage = MergeWaitStage()
        ctx = make_ctx(
            github=FakeStageGitHub(pr_state=OPEN_STATE),
            budget_fn=lambda name: 10 if name == "merge" else 1,
        )
        item = _armed_item(make_work_item)
        item.payload["merge_wait_started_at"] = 1000.0

        for expected_delay in (1, 2, 4):
            result = stage.step(item, ctx)
            assert result == StageOutcome(Disposition.RETRY, "merge_pending")
            assert item.payload["retry_delay_s"] == expected_delay
        assert item.payload["merge_poll_count"] == 3
        # The wall clock lives in the payload, NEVER in the attempts dict.
        assert "merge_elapsed" not in item.attempts
        assert "merge_poll" not in item.attempts

    def test_pending_exhausts_merge_attempt_budget_before_wall_clock_timeout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """The CLI merge budget bounds pending polls and durably skips the issue."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=OPEN_STATE)
        ctx = make_ctx(github=github, budget_fn=lambda _name: 1)
        item = _armed_item(make_work_item)

        first = stage.step(item, ctx)
        assert first == StageOutcome(Disposition.RETRY, "merge_pending")
        item.payload.pop("retry_delay_s")

        second = stage.step(item, ctx)

        assert second == StageOutcome(Disposition.SKIP, "merge_attempts_exhausted")
        assert STATE_SKIP in github.labels[item.issue]

    def test_wall_clock_timeout_uses_ctx_now(
        self, make_ctx: Any, make_work_item: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 1800s legacy budget is enforced via ctx.now(), not sleep sums."""
        monkeypatch.setenv(MERGE_MAX_WAIT_ENV, "10")
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=OPEN_STATE))
        item = _armed_item(make_work_item)
        item.payload["merge_wait_started_at"] = 0.0  # ctx.now() is ~1000

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "timeout")

    @pytest.mark.parametrize("payload", ({}, {"merge_wait_started_at": None}))
    def test_poll_missing_wall_clock_anchor_finishes_failed(
        self, make_ctx: Any, make_work_item: Any, payload: dict[str, object]
    ) -> None:
        """A restart that reaches POLL without the ARM anchor fails the invariant."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=OPEN_STATE))
        item = _armed_item(make_work_item, with_anchor=False)
        item.payload.update(payload)

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "missing_merge_wait_started_at")
        assert item.payload == payload
        assert "retry_delay_s" not in item.payload
        assert "merge_poll_count" not in item.payload

    def test_poll_without_pr_finishes_no_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """Restart safety: POLL with no PR finishes failed."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _armed_item(make_work_item, pr=None)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_FAIL, "no_pr")


class TestMergeWaitDirty:
    """DIRTY: mechanical rebase+push, budget-bounded."""

    def test_dirty_routes_to_rebase_with_base_branch(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """DIRTY captures the PR's real base ref for the rebase target."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=DIRTY_STATE))
        item = _armed_item(make_work_item)

        result = stage.step(item, ctx)

        assert result == Continue(next_state=DIRTY_REBASE_WAIT)
        assert item.payload["base_branch"] == "develop"

    def test_dirty_exhaustion_is_rebase_exhausted_not_timeout(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Rebase-budget exhaustion uses its own vocabulary, never 'timeout'."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=DIRTY_STATE))
        item = _armed_item(make_work_item)
        item.attempts["rebase"] = 2

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FINISH_FAIL, "rebase_exhausted")

    def test_dirty_clean_rebase_pushes_then_merges(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """DIRTY -> rebase (clean) -> push -> re-poll MERGED -> learn -> PASS."""
        stage = MergeWaitStage()
        github = _FifoStateGitHub([DIRTY_STATE, MERGED_STATE])
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=True, value=True),  # rebase: clean
            JobResult(ok=True),  # push
            JobResult(ok=True, value="learned"),  # learn
        )
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        git_jobs = [h.job for h in pool.submitted if isinstance(h.job, GitJob)]
        assert [j.op for j in git_jobs] == ["rebase", "push"]
        assert git_jobs[0].kwargs["base_branch"] == "develop"
        assert item.attempts["rebase"] == 1

    def test_dirty_conflicting_rebase_never_pushes_and_exhausts(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A still-conflicting rebase re-polls without pushing until exhausted."""
        stage = MergeWaitStage()
        github = _FifoStateGitHub([DIRTY_STATE])
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=False, value=False, error="conflict"),  # rebase 1
            JobResult(ok=False, value=False, error="conflict"),  # rebase 2
        )
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_FAIL, "rebase_exhausted")
        git_jobs = [h.job for h in pool.submitted if isinstance(h.job, GitJob)]
        assert [j.op for j in git_jobs] == ["rebase", "rebase"]  # no push ever
        assert item.attempts["rebase"] == 2


class TestMergeWaitBlocked:
    """BLOCKED: address threads, budget-bounded, stuck-gated skip."""

    def test_blocked_routes_to_address(self, make_ctx: Any, make_work_item: Any) -> None:
        """BLOCKED with budget enters the address leg."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=BLOCKED_STATE))
        item = _armed_item(make_work_item)

        assert stage.step(item, ctx) == Continue(next_state=BLOCKED_ADDRESS_WAIT)

    def test_blocked_walk_addresses_pushes_then_merges(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """BLOCKED -> address agent -> push -> re-poll MERGED -> learn -> PASS."""
        stage = MergeWaitStage()
        github = _FifoStateGitHub([BLOCKED_STATE, MERGED_STATE])
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _armed_item(make_work_item)
        item.payload["threads_json"] = '[{"id": "T1"}]'
        item.payload["difficulty_tiers"] = "@ file.py Line 3 - EASY - fix"

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        address_jobs = [
            h.job
            for h in pool.submitted
            if isinstance(h.job, AgentJob) and h.job.descr == "blocked_address"
        ]
        assert len(address_jobs) == 1
        # kwargs-verified: the stored kwargs must compose a real prompt.
        prompt = address_jobs[0].prompt_builder(**address_jobs[0].prompt_kwargs)
        assert "T1" in prompt
        push_jobs = [h.job for h in pool.submitted if isinstance(h.job, GitJob)]
        assert [j.op for j in push_jobs] == ["commit_push"]
        assert item.attempts["blocked_address"] == 1

    def test_failed_address_turn_repolls_without_push(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A hard-failed address session re-polls; the budget still counts."""
        stage = MergeWaitStage()
        github = _FifoStateGitHub([BLOCKED_STATE])
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        pool.script(
            JobResult(ok=False, error="agent crashed"),  # address 1
            JobResult(ok=False, error="agent crashed"),  # address 2
        )
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FAIL_BACK, "blocked_exhausted")
        assert item.attempts["blocked_address"] == 2
        assert not [h.job for h in pool.submitted if isinstance(h.job, GitJob)]

    def test_blocked_exhaustion_not_stuck_fails_back(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """An awaiting-review BLOCKED PR is NOT stuck: regress, never skip (#1576)."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=BLOCKED_STATE, pr_stuck=False)
        ctx = make_ctx(github=github)
        item = _armed_item(make_work_item)
        item.attempts["blocked_address"] = 2

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.FAIL_BACK, "blocked_exhausted")
        assert STATE_SKIP not in github.labels.get(1, set())

    def test_blocked_exhaustion_genuinely_stuck_skips_durably(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """A genuinely stuck PR is durably skip-tagged BEFORE the SKIP outcome."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=BLOCKED_STATE, pr_stuck=True)
        ctx = make_ctx(github=github)
        item = _armed_item(make_work_item)
        item.attempts["blocked_address"] = 2

        result = stage.step(item, ctx)

        assert result == StageOutcome(Disposition.SKIP, "blocked_stuck")
        assert STATE_SKIP in github.labels[1]
        assert ("gh_issue_add_labels", (1, (STATE_SKIP,))) in github.mutation_log


class TestMergeWaitLearn:
    """MERGED -> deduped /learn -> durable mark -> FINISH_PASS."""

    def test_learn_dedupe_skips_terminal_records(self, make_ctx: Any, make_work_item: Any) -> None:
        """Mutation probe (c): a terminal learn record never re-fires /learn."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE, learn_terminal=True)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert pool.submitted == []  # no learn session dispatched
        assert github.mutation_log == []  # and nothing re-marked

    def test_learn_disabled_by_config_skips_session(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """enable_learn=False finishes PASS without dispatching /learn."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub(pr_state=MERGED_STATE))
        ctx.config.enable_learn = False
        pool = FakeWorkerPool()
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert pool.submitted == []

    def test_failed_learn_never_fails_a_merged_pr(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed /learn is marked (succeeded=False) and the drive PASSes."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        pool.script(JobResult(ok=False, error="learn agent crashed"))
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert github.learn_results[1] is False  # terminal: failed, not retried

    def test_learn_marked_terminal_prevents_replay(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """After one learn run, a restarted walk dedupes on the read-back."""
        stage = MergeWaitStage()
        github = FakeStageGitHub(pr_state=MERGED_STATE)
        ctx = make_ctx(github=github)
        pool = FakeWorkerPool()
        first = _armed_item(make_work_item)
        assert _drive(stage, first, ctx, pool) == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert len(pool.submitted) == 1

        replay = _armed_item(make_work_item)  # same issue, fresh in-memory item
        assert _drive(stage, replay, ctx, pool) == StageOutcome(Disposition.FINISH_PASS, "merged")
        assert len(pool.submitted) == 1  # no second learn session

    def test_failed_learn_mark_is_non_fatal(self, make_ctx: Any, make_work_item: Any) -> None:
        """A failed durable mark logs and still finishes PASS (merged is merged)."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=_MarkFailGitHub(pr_state=MERGED_STATE))
        pool = FakeWorkerPool()
        item = _armed_item(make_work_item)

        outcome = _drive(stage, item, ctx, pool)

        assert outcome == StageOutcome(Disposition.FINISH_PASS, "merged")

    def test_learn_prompt_renders(self) -> None:
        """The composed /learn prompt names the PR and issue."""
        prompt = build_drive_green_learn_prompt(issue_number=9, pr_number=99)
        assert "/learn" in prompt
        assert "PR #99" in prompt
        assert "issue #9" in prompt

    def test_finish_state_is_terminal_pass(self, make_ctx: Any, make_work_item: Any) -> None:
        """MW_FINISH always passes — /learn outcome can never flip a merged PR."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _armed_item(make_work_item, state=MW_FINISH)

        assert stage.step(item, ctx) == StageOutcome(Disposition.FINISH_PASS, "merged")


class TestMergeWaitOnJobDone:
    """on_job_done budget/flag routing (state still the WAIT state)."""

    def test_rebase_done_counts_and_records_cleanliness(
        self, make_ctx: Any, make_work_item: Any
    ) -> None:
        """Rebase completion consumes the budget and records rebase_clean."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _armed_item(make_work_item, state=DIRTY_REBASE_WAIT)

        stage.on_job_done(item, JobResult(ok=True, value=True), ctx)
        assert item.attempts["rebase"] == 1
        assert item.payload["rebase_clean"] is True

        stage.on_job_done(item, JobResult(ok=False, value=False, error="conflict"), ctx)
        assert item.attempts["rebase"] == 2
        assert item.payload["rebase_clean"] is False

    def test_push_failures_are_best_effort(self, make_ctx: Any, make_work_item: Any) -> None:
        """Push failures on either leg record nothing — POLL re-classifies."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        for state in (DIRTY_PUSH_WAIT, BLOCKED_PUSH_WAIT):
            item = _armed_item(make_work_item, state=state)
            stage.on_job_done(item, JobResult(ok=False, error="push failed"), ctx)
            assert "address_failed" not in item.payload
            assert "rebase_clean" not in item.payload

    def test_learn_wait_step_dispatches_learn_job(self, make_ctx: Any, make_work_item: Any) -> None:
        """LEARN_WAIT submits the composed learn job targeting MW_FINISH."""
        stage = MergeWaitStage()
        ctx = make_ctx(github=FakeStageGitHub())
        item = _armed_item(make_work_item, state=LEARN_WAIT)

        result = stage.step(item, ctx)

        assert isinstance(result, JobRequest)
        assert isinstance(result.job, AgentJob)
        assert result.job.prompt_builder is build_drive_green_learn_prompt
        assert result.on_done_state == MW_FINISH
        prompt = result.job.prompt_builder(**result.job.prompt_kwargs)
        assert "PR #601" in prompt
