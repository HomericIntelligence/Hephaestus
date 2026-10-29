"""Coordinator-level phase-recording tests for the work-item timing seam (#3295)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.github_jobs import RateBudgetRead, ReadRateBudgetRequest
from hephaestus.automation.pipeline.jobs import AgentJob, BuildTestJob, GitJob, JobHandle, JobResult
from hephaestus.automation.pipeline.phase_timing import PhaseName, RetryCause
from hephaestus.automation.pipeline.routing import ROUTES, Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.stage_results import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from tests.unit.automation.pipeline.conftest import (
    FakeWorkerPool,
    claim_test_item,
    fake_worker_factories,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_EPOCH = 1_700_000_000.0


class _Clock:
    """Controllable wall clock for deterministic timing assertions."""

    def __init__(self, start: float = _EPOCH) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta_s: float) -> None:
        self.now += delta_s


def _coordinator(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
    event_log_path: Path | None = None,
) -> tuple[Coordinator, _Clock]:
    clock = clock or _Clock()
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        loops=1,
        projects_dir=tmp_path,
        rate_guard_enabled=False,
        event_log_path=event_log_path,
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(FakeWorkerPool(), None),
        install_signals=False,
        wall_time=clock,
    )
    return coordinator, clock


def _item(issue: int = 7, stage: StageName = StageName.PLANNING) -> WorkItem:
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, stage=stage, state="ENTER")


def _agent_job(repo: str = "repo-a", issue: int = 7) -> AgentJob:
    return AgentJob(
        repo=repo,
        issue=issue,
        agent="claude",
        model="m",
        prompt_builder=lambda **kwargs: "prompt",
        cwd=Path("/tmp"),
        timeout_s=10,
        descr="stub agent job",
    )


def _receipt_events(coordinator: Coordinator) -> list[dict[str, Any]]:
    return [fields[0] for event, *fields in coordinator.event_log if event == "work_item_timing"]


class TestHandoffPhaseRecording:
    """Stage handoffs open and close intervals on the item accumulator."""

    def test_handoff_enters_and_leaves_phase_intervals(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        claim_test_item(coordinator, item)

        coordinator._activate_handoff(item, StageName.PLAN_REVIEW, enter=True, result=None)
        clock.advance(10.0)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)

        planning = item.timing.phases[PhaseName.PLANNING]
        implementation = item.timing.phases[PhaseName.IMPLEMENTATION]
        assert planning.start_s == _EPOCH
        assert planning.elapsed_s == pytest.approx(10.0)
        assert implementation.start_s == _EPOCH + 10.0
        assert item.timing.current_phase is PhaseName.IMPLEMENTATION
        assert item.timing.wall_s() == pytest.approx(10.0)

    def test_same_phase_stages_share_one_interval(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)
        clock.advance(5.0)
        coordinator._activate_handoff(item, StageName.PR_REVIEW, enter=True, result=None)
        clock.advance(5.0)
        coordinator._activate_handoff(item, StageName.MERGE_WAIT, enter=True, result=None)

        implementation = item.timing.phases[PhaseName.IMPLEMENTATION]
        assert implementation.elapsed_s == pytest.approx(10.0)
        assert item.timing.current_phase is PhaseName.MERGE_ADMISSION

    def test_handoff_into_an_unmapped_target_closes_the_interval(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        coordinator._activate_handoff(item, StageName.PLAN_REVIEW, enter=True, result=None)
        clock.advance(3.0)
        coordinator._activate_handoff(item, StageName.FINISHED, enter=True, result=None)

        assert item.timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(3.0)
        assert item.timing.current_phase is None


class TestWaitRecording:
    """Timer parks record classified waits; wakes close them."""

    def test_merge_wait_park_records_ci_wait(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.MERGE_WAIT)
        coordinator._activate_handoff(item, StageName.MERGE_WAIT, enter=True, result=None)
        clock.advance(2.0)
        coordinator._timer_park(item, 30.0)
        clock.advance(30.0)
        _wake, seq, parked = coordinator.timers[0]
        coordinator.timers[0] = (0.0, seq, parked)
        coordinator._wake_timers()

        ci_wait = item.timing.phases[PhaseName.CI_WAIT]
        assert ci_wait.wait_s == pytest.approx(30.0)
        assert ci_wait.attempts == 1
        assert item.timing.current_waiting is False
        assert item.timing.current_phase is PhaseName.MERGE_ADMISSION

    def test_stage_park_records_a_waiting_classification_in_phase(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)
        clock.advance(4.0)
        coordinator._timer_park(item, 6.0)
        clock.advance(6.0)
        _wake, seq, parked = coordinator.timers[0]
        coordinator.timers[0] = (0.0, seq, parked)
        coordinator._wake_timers()

        record = item.timing.phases[PhaseName.IMPLEMENTATION]
        assert record.wait_s == pytest.approx(6.0)
        assert record.elapsed_s == pytest.approx(10.0)
        assert record.active_s == pytest.approx(4.0)

    def test_rate_guard_park_records_external_wait(self, tmp_path: Path) -> None:
        coordinator, _clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        coordinator._activate_handoff(item, StageName.PLANNING, enter=True, result=None)
        coordinator._timer_park(item, 60.0, wait_cause="rate_guard")

        assert item.timing.phases[PhaseName.EXTERNAL_WAIT].attempts == 1
        assert item.timing.current_phase is PhaseName.EXTERNAL_WAIT

    def test_complete_rate_budget_parks_with_external_wait(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        coordinator._activate_handoff(item, StageName.PLANNING, enter=True, result=None)
        request = ReadRateBudgetRequest(deadline_s=coordinator._monotonic() + 60.0)
        item.payload["_pending_agent_request"] = JobRequest(_agent_job(), on_done_state="PLAN_WAIT")
        result = JobResult(
            ok=True,
            value=RateBudgetRead(request=request, remaining=0, reset_epoch=int(clock.now) + 60),
        )

        coordinator._complete_rate_budget(item, result)

        assert item.timing.phases[PhaseName.EXTERNAL_WAIT].attempts == 1
        assert len(coordinator.timers) == 1


class TestJobAttribution:
    """Job completions attribute durations to the classified phase."""

    def test_build_test_completion_counts_test_execution(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)
        job = BuildTestJob(
            repo="repo-a",
            cwd=Path("/tmp"),
            argv=("uv", "run", "pytest", "-q"),
            timeout_s=10,
            descr="run affected tests",
        )
        handle = JobHandle(job=job, on_done_state="TEST_DONE")
        clock.advance(12.0)
        coordinator._record_job_timing(item, handle, JobResult(ok=True, duration_s=12.0))

        tests = item.timing.phases[PhaseName.TEST_EXECUTION]
        assert tests.active_s == pytest.approx(12.0)
        assert tests.attempts == 1
        assert item.timing.last_error_class is None

    def test_failed_build_test_records_the_error_class(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)
        job = BuildTestJob(
            repo="repo-a",
            cwd=Path("/tmp"),
            argv=("uv", "run", "pytest", "-q"),
            timeout_s=10,
            descr="run affected tests",
        )
        handle = JobHandle(job=job, on_done_state="TEST_DONE")
        clock.advance(4.0)
        coordinator._record_job_timing(
            item, handle, JobResult(ok=False, error="rc=2", duration_s=4.0)
        )

        assert item.timing.last_error_class == "process_exit"

    def test_publication_ops_count_publication(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(item, StageName.IMPLEMENTATION, enter=True, result=None)
        job = GitJob(repo="repo-a", op="commit_push", timeout_s=10, descr="publish work")
        handle = JobHandle(job=job, on_done_state="PUBLISHED")
        clock.advance(7.0)
        coordinator._record_job_timing(item, handle, JobResult(ok=True, duration_s=7.0))

        assert item.timing.phases[PhaseName.PUBLICATION].active_s == pytest.approx(7.0)
        assert item.timing.phases[PhaseName.PUBLICATION].attempts == 1

    def test_other_jobs_count_the_current_stage_phase(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        coordinator._activate_handoff(item, StageName.PLANNING, enter=True, result=None)
        handle = JobHandle(job=_agent_job(), on_done_state="PLANNED")
        clock.advance(9.0)
        coordinator._record_job_timing(item, handle, JobResult(ok=True, duration_s=9.0))

        record = item.timing.phases[PhaseName.PLANNING]
        assert record.attempts == 1


class TestRetryAndCorrectionCauses:
    """Retry routes and fail-back routes record closed causes."""

    def test_retry_with_a_failed_job_records_the_classified_cause(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLAN_REVIEW)
        coordinator._activate_handoff(item, StageName.PLAN_REVIEW, enter=True, result=None)
        handle = JobHandle(job=_agent_job(), on_done_state="REVIEWED")
        clock.advance(3.0)
        coordinator._record_job_timing(
            item, handle, JobResult(ok=False, error="timeout", duration_s=3.0)
        )
        item.payload["retry_delay_s"] = 30.0
        coordinator._route_retry(item, StageOutcome(Disposition.RETRY, "some poll"))

        assert item.timing.causes == {"tool_failure": 1}
        assert item.timing.last_cause is RetryCause.TOOL_FAILURE
        assert item.timing.last_error_class is None
        assert len(coordinator.timers) == 1

    def test_poll_retry_records_no_cause(self, tmp_path: Path) -> None:
        coordinator, _clock = _coordinator(tmp_path)
        item = _item(stage=StageName.MERGE_WAIT)
        item.payload["retry_delay_s"] = 30.0
        coordinator._route_retry(item, StageOutcome(Disposition.RETRY, "merge_wait_poll"))

        assert dict(item.timing.causes) == {}

    def test_fail_back_records_a_correction_cycle(self, tmp_path: Path) -> None:
        coordinator, _clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLAN_REVIEW)
        claim_test_item(coordinator, item)
        route = ROUTES[StageName.PLAN_REVIEW]
        coordinator._route_fail_back(item, StageOutcome(Disposition.FAIL_BACK, "nogo"), route)

        correction = item.timing.phases[PhaseName.CORRECTION]
        assert correction.attempts == 1
        assert item.timing.causes == {"review_findings": 1}
        assert coordinator.queues[StageName.PLANNING].snapshot() == [item]

    def test_fail_back_without_cause_uses_closed_input_only(self, tmp_path: Path) -> None:
        coordinator, _clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        claim_test_item(coordinator, item)
        route = ROUTES[StageName.PLANNING]
        coordinator._route_fail_back(item, StageOutcome(Disposition.FAIL_BACK, "note"), route)

        assert item.timing.causes == {"tool_failure": 1}


class TestTerminalReceipt:
    """Terminal items emit exactly one closed-schema receipt."""

    def test_terminal_result_emits_one_bounded_receipt(self, tmp_path: Path) -> None:
        event_log_path = tmp_path / "pipeline-events.jsonl"
        coordinator, clock = _coordinator(tmp_path, event_log_path=event_log_path)
        item = _item(stage=StageName.MERGE_WAIT)
        item.pr = 11
        item.payload["_impl_source_revision"] = "a" * 40
        coordinator._activate_handoff(item, StageName.MERGE_WAIT, enter=True, result=None)
        clock.advance(8.0)
        item.result = ItemResult(passed=True, reason="merged", final_stage=StageName.MERGE_WAIT)

        coordinator._record_terminal_result(item)
        coordinator._record_terminal_result(item)

        receipts = _receipt_events(coordinator)
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["repo"] == "repo-a"
        assert receipt["issue"] == 7
        assert receipt["pr"] == 11
        assert receipt["head_sha"] == "a" * 40
        assert receipt["passed"] is True
        assert receipt["final_stage"] == "merge_wait"
        assert receipt["wall_s"] == pytest.approx(8.0)
        assert set(receipt) == {
            "repo",
            "issue",
            "pr",
            "head_sha",
            "run_identity",
            "wall_s",
            "active_s",
            "wait_s",
            "phases",
            "causes",
            "current_phase",
            "current_waiting",
            "last_cause",
            "last_error_class",
            "passed",
            "final_stage",
        }

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        timing_records = [r for r in records if r["event"] == "work_item_timing"]
        assert len(timing_records) == 1
        assert str(tmp_path) not in json.dumps(timing_records[0])

    def test_receipt_is_emitted_at_most_once_per_item(self, tmp_path: Path) -> None:
        coordinator, _clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        item.result = ItemResult(passed=True, reason="done", final_stage=StageName.FINISHED)

        coordinator._record_terminal_result(item)
        receipt_before = list(_receipt_events(coordinator))
        coordinator._record_terminal_result(item)
        item.timing.record_cause(RetryCause.TOOL_FAILURE)
        assert _receipt_events(coordinator) == receipt_before

    def test_resumable_park_closes_intervals_and_reports_stall(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PR_REVIEW)
        coordinator._activate_handoff(item, StageName.PR_REVIEW, enter=True, result=None)
        clock.advance(5.0)
        item.timing.record_cause(RetryCause.REVIEW_FINDINGS)

        coordinator._park_resumable(item)

        assert item.timing.closed is True
        receipts = _receipt_events(coordinator)
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["current_phase"] == "implementation"
        assert receipt["current_waiting"] is False
        assert receipt["last_cause"] == "review_findings"
        assert receipt["passed"] is False
        assert receipt["final_stage"] == "pr_review"
        assert receipt["phases"]["implementation"]["elapsed_s"] == pytest.approx(5.0)

    def test_timing_failures_never_route_or_fail_the_item(self, tmp_path: Path) -> None:
        event_log_path = tmp_path / "pipeline-events.jsonl"
        coordinator, _clock = _coordinator(tmp_path, event_log_path=event_log_path)
        item = _item(stage=StageName.PLANNING)
        item.result = ItemResult(passed=True, reason="done", final_stage=StageName.FINISHED)
        item.pr = "not-a-number"  # type: ignore[assignment]

        coordinator._record_terminal_result(item)

        assert _receipt_events(coordinator) == []
        assert item.result.passed is True


class TestConcurrentAndRestartSemantics:
    """Concurrent work keeps identities; closed intervals never re-count."""

    def test_two_items_keep_separate_accumulators(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        first = _item(issue=1, stage=StageName.PLANNING)
        second = _item(issue=2, stage=StageName.IMPLEMENTATION)
        coordinator._activate_handoff(first, StageName.PLANNING, enter=True, result=None)
        clock.advance(4.0)
        coordinator._activate_handoff(second, StageName.IMPLEMENTATION, enter=True, result=None)
        clock.advance(4.0)
        coordinator._activate_handoff(first, StageName.PLAN_REVIEW, enter=True, result=None)

        assert first.timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(8.0)
        assert second.timing.phases[PhaseName.IMPLEMENTATION].elapsed_s == pytest.approx(0.0)
        assert first.timing.current_phase is PhaseName.PLANNING
        assert second.timing.current_phase is PhaseName.IMPLEMENTATION
        assert first.timing is not second.timing

    def test_reseed_makes_a_new_accumulator(self, tmp_path: Path) -> None:
        first = _item(issue=7)
        first.timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        first.timing.close(_EPOCH + 10.0)
        second = _item(issue=7)
        second.timing.enter_phase(PhaseName.PLANNING, _EPOCH + 20.0)

        assert first.timing is not second.timing
        assert second.timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(0.0)
        assert first.timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(10.0)

    def test_receipt_fields_contain_no_free_text(self, tmp_path: Path) -> None:
        coordinator, clock = _coordinator(tmp_path)
        item = _item(stage=StageName.PLANNING)
        coordinator._activate_handoff(item, StageName.PLANNING, enter=True, result=None)
        handle = JobHandle(job=_agent_job(), on_done_state="PLANNED")
        clock.advance(2.0)
        coordinator._record_job_timing(
            item,
            handle,
            JobResult(
                ok=False,
                error="bearer secret /home/operator/private path leaked",
                duration_s=2.0,
            ),
        )
        item.result = ItemResult(
            passed=False, reason="tool failure at /home/operator", final_stage=StageName.PLANNING
        )
        coordinator._record_terminal_result(item)

        receipts = _receipt_events(coordinator)
        assert len(receipts) == 1
        text = json.dumps(receipts[0])
        assert "/home" not in text
        assert "bearer" not in text.lower()
        assert receipts[0]["last_error_class"] == "error"
