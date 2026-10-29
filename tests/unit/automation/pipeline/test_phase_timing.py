"""Tests for the bounded phase-timing and retry-cause module (#3295)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from hephaestus.automation.pipeline.phase_timing import (
    PHASE_FOR_STAGE,
    PUBLICATION_OPS,
    PhaseName,
    PhaseRecord,
    RetryCause,
    WorkItemTiming,
    classify_retry_cause,
    encode_timing_receipt,
)
from hephaestus.automation.pipeline.routing import StageName

_EPOCH = 1_700_000_000.0


def _identity(**overrides: Any) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "repo": "repo-a",
        "issue": 7,
        "pr": 11,
        "head_sha": "a" * 40,
    }
    identity.update(overrides)
    return identity


class TestClosedVocabularies:
    """The phase and retry-cause vocabularies stay closed and stable."""

    def test_phase_vocabulary_matches_the_issue(self) -> None:
        assert {phase.value for phase in PhaseName} == {
            "issue_intake",
            "planning",
            "implementation",
            "test_execution",
            "publication",
            "ci_wait",
            "correction",
            "merge_admission",
            "external_wait",
        }

    def test_retry_cause_vocabulary_matches_the_issue(self) -> None:
        assert {cause.value for cause in RetryCause} == {
            "source_change",
            "review_findings",
            "test_failure",
            "check_failure",
            "remote_movement",
            "tool_failure",
            "external_wait",
        }

    def test_stage_phase_map_is_a_subset_of_stages(self) -> None:
        assert set(PHASE_FOR_STAGE) <= set(StageName)
        assert set(PHASE_FOR_STAGE.values()) <= set(PhaseName)
        assert PHASE_FOR_STAGE == {
            StageName.REPO: PhaseName.ISSUE_INTAKE,
            StageName.PLANNING: PhaseName.PLANNING,
            StageName.PLAN_REVIEW: PhaseName.PLANNING,
            StageName.IMPLEMENTATION: PhaseName.IMPLEMENTATION,
            StageName.PR_REVIEW: PhaseName.IMPLEMENTATION,
            StageName.MERGE_WAIT: PhaseName.MERGE_ADMISSION,
        }

    def test_publication_ops_are_a_closed_subset(self) -> None:
        from hephaestus.automation.pipeline.git_jobs import GIT_OPS

        assert PUBLICATION_OPS <= GIT_OPS
        expected = {
            "commit_push",
            "publish_dirty_direct_continuation",
            "finish_dirty_direct_publication",
        }
        assert set(PUBLICATION_OPS) == expected


class TestClassifyRetryCause:
    """The classification table maps closed inputs to closed causes only."""

    @pytest.mark.parametrize(
        ("note", "cause"),
        [
            ("plan_changed", RetryCause.SOURCE_CHANGE),
            ("nogo", RetryCause.REVIEW_FINDINGS),
            ("plan_missing", RetryCause.REVIEW_FINDINGS),
            ("plan_not_go", RetryCause.REVIEW_FINDINGS),
            ("implementation_remediation", RetryCause.REVIEW_FINDINGS),
            ("scope_retraction_before_scope_block", RetryCause.REVIEW_FINDINGS),
            ("merge_not_ready", RetryCause.CHECK_FAILURE),
            ("reviewed_head_drift", RetryCause.REMOTE_MOVEMENT),
            ("reviewed_head_missing", RetryCause.REMOTE_MOVEMENT),
            ("merge_conflicting", RetryCause.REMOTE_MOVEMENT),
            ("not_implementation_go", RetryCause.REMOTE_MOVEMENT),
            ("rebase_conflict", RetryCause.REMOTE_MOVEMENT),
        ],
    )
    def test_named_notes_classify_from_the_closed_table(self, note: str, cause: RetryCause) -> None:
        assert classify_retry_cause(StageName.PLAN_REVIEW, note, None) is cause

    @pytest.mark.parametrize("error_class", ["timeout", "worker_crash", "error"])
    def test_tool_error_classes_classify_as_tool_failure(self, error_class: str) -> None:
        assert (
            classify_retry_cause(StageName.IMPLEMENTATION, "unknown note", error_class)
            is RetryCause.TOOL_FAILURE
        )

    def test_named_note_takes_precedence_over_error_class(self) -> None:
        assert (
            classify_retry_cause(StageName.PLAN_REVIEW, "nogo", "timeout")
            is RetryCause.REVIEW_FINDINGS
        )

    def test_poll_notes_record_no_cause(self) -> None:
        assert classify_retry_cause(StageName.MERGE_WAIT, "merge_wait_poll", None) is None

    def test_interrupted_records_no_cause(self) -> None:
        assert classify_retry_cause(StageName.PLANNING, "", "interrupted") is None

    @pytest.mark.parametrize(
        ("stage", "cause"),
        [
            (StageName.REPO, RetryCause.TOOL_FAILURE),
            (StageName.PLANNING, RetryCause.TOOL_FAILURE),
            (StageName.PLAN_REVIEW, RetryCause.REVIEW_FINDINGS),
            (StageName.IMPLEMENTATION, RetryCause.TOOL_FAILURE),
            (StageName.PR_REVIEW, RetryCause.REVIEW_FINDINGS),
            (StageName.MERGE_WAIT, RetryCause.CHECK_FAILURE),
        ],
    )
    def test_unknown_notes_use_the_stage_default(self, stage: StageName, cause: RetryCause) -> None:
        assert classify_retry_cause(stage, "notes never seen before", None) is cause

    def test_unmapped_stage_defaults_to_tool_failure(self) -> None:
        assert classify_retry_cause(StageName.FINISHED, "anything", None) is RetryCause.TOOL_FAILURE

    @pytest.mark.parametrize("cause", list(RetryCause))
    def test_every_cause_is_reachable(self, cause: RetryCause) -> None:
        inputs: dict[RetryCause, tuple[StageName, str, str | None]] = {
            RetryCause.SOURCE_CHANGE: (StageName.PLAN_REVIEW, "plan_changed", None),
            RetryCause.REVIEW_FINDINGS: (StageName.PLAN_REVIEW, "nogo", None),
            RetryCause.TEST_FAILURE: (StageName.IMPLEMENTATION, "tests_failed", None),
            RetryCause.CHECK_FAILURE: (StageName.MERGE_WAIT, "merge_not_ready", None),
            RetryCause.REMOTE_MOVEMENT: (StageName.MERGE_WAIT, "merge_conflicting", None),
            RetryCause.TOOL_FAILURE: (StageName.IMPLEMENTATION, "", "timeout"),
            RetryCause.EXTERNAL_WAIT: (StageName.PLANNING, "rate_guard", None),
        }
        stage, note, error_class = inputs[cause]
        assert classify_retry_cause(stage, note, error_class) is cause


class TestPhaseRecordValidation:
    """Phase records reject invalid durations like the event validators."""

    @pytest.mark.parametrize("duration", [-1.0, float("inf"), float("nan")])
    def test_add_interval_rejects_invalid_durations(self, duration: float) -> None:
        record = PhaseRecord()
        with pytest.raises((TypeError, ValueError)):
            record.add_interval(_EPOCH, _EPOCH + duration)

    def test_add_interval_rejects_an_inverted_interval(self) -> None:
        record = PhaseRecord()
        with pytest.raises(ValueError):
            record.add_interval(_EPOCH + 5.0, _EPOCH)

    @pytest.mark.parametrize("now", [True, "now", None])
    def test_add_interval_rejects_non_numeric_marks(self, now: object) -> None:
        record = PhaseRecord()
        with pytest.raises((TypeError, ValueError)):
            record.add_interval(now, _EPOCH)  # type: ignore[arg-type]


class TestWorkItemTiming:
    """The accumulator keeps bounded, idempotent per-item timing facts."""

    def test_phases_enter_leave_and_wait_accumulate(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        timing.leave_phase(PhaseName.PLANNING, _EPOCH + 10.0)
        record = timing.phases[PhaseName.PLANNING]
        assert record.elapsed_s == pytest.approx(10.0)
        assert record.active_s == pytest.approx(10.0)
        assert record.wait_s == 0.0
        assert record.start_s == _EPOCH
        assert record.end_s == _EPOCH + 10.0

    def test_wait_intervals_diverts_time_from_the_current_phase(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.MERGE_ADMISSION, _EPOCH)
        timing.begin_wait(PhaseName.CI_WAIT, _EPOCH + 5.0)
        timing.end_wait(_EPOCH + 65.0)
        timing.leave_phase(PhaseName.MERGE_ADMISSION, _EPOCH + 70.0)
        admission = timing.phases[PhaseName.MERGE_ADMISSION]
        wait = timing.phases[PhaseName.CI_WAIT]
        assert admission.elapsed_s == pytest.approx(10.0)
        assert admission.wait_s == 0.0
        assert admission.active_s == pytest.approx(10.0)
        assert wait.wait_s == pytest.approx(60.0)
        assert wait.elapsed_s == pytest.approx(60.0)
        assert wait.active_s == 0.0
        assert wait.attempts == 1
        assert timing.current_waiting is False

    def test_same_phase_wait_records_inside_the_phase(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH)
        timing.begin_wait(PhaseName.IMPLEMENTATION, _EPOCH + 5.0)
        timing.end_wait(_EPOCH + 20.0)
        timing.leave_phase(PhaseName.IMPLEMENTATION, _EPOCH + 25.0)
        record = timing.phases[PhaseName.IMPLEMENTATION]
        assert record.elapsed_s == pytest.approx(25.0)
        assert record.wait_s == pytest.approx(15.0)
        assert record.active_s == pytest.approx(10.0)

    def test_record_job_attributes_duration_to_an_implicit_phase(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH)
        timing.record_job(PhaseName.TEST_EXECUTION, 30.0, None, _EPOCH + 100.0)
        timing.record_job(PhaseName.PUBLICATION, 10.0, "error", _EPOCH + 120.0)
        timing.leave_phase(PhaseName.IMPLEMENTATION, _EPOCH + 200.0)
        tests = timing.phases[PhaseName.TEST_EXECUTION]
        publication = timing.phases[PhaseName.PUBLICATION]
        implementation = timing.phases[PhaseName.IMPLEMENTATION]
        assert tests.active_s == pytest.approx(30.0)
        assert tests.attempts == 1
        assert publication.active_s == pytest.approx(10.0)
        assert publication.attempts == 1
        assert implementation.elapsed_s == pytest.approx(200.0)
        assert implementation.active_s == pytest.approx(160.0)
        assert timing.last_error_class == "error"

    def test_record_job_in_the_current_phase_counts_attempts_only(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        timing.record_job(PhaseName.PLANNING, 5.0, None, _EPOCH + 5.0)
        timing.leave_phase(PhaseName.PLANNING, _EPOCH + 10.0)
        record = timing.phases[PhaseName.PLANNING]
        assert record.attempts == 1
        assert record.active_s == pytest.approx(10.0)

    def test_record_cause_counts_and_tracks_the_last_cause(self) -> None:
        timing = WorkItemTiming()
        timing.record_cause(RetryCause.REVIEW_FINDINGS)
        timing.record_cause(RetryCause.TOOL_FAILURE)
        timing.record_cause(RetryCause.TOOL_FAILURE)
        assert timing.causes == {"review_findings": 1, "tool_failure": 2}
        assert timing.last_cause is RetryCause.TOOL_FAILURE

    def test_record_cause_resets_the_error_class(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH)
        timing.record_job(PhaseName.IMPLEMENTATION, 5.0, "timeout", _EPOCH + 5.0)
        assert timing.last_error_class == "timeout"
        timing.record_cause(RetryCause.TOOL_FAILURE)
        assert timing.last_error_class is None

    def test_record_correction_counts_attempts_and_causes(self) -> None:
        timing = WorkItemTiming()
        timing.record_correction(RetryCause.REMOTE_MOVEMENT, _EPOCH)
        correction = timing.phases[PhaseName.CORRECTION]
        assert correction.attempts == 1
        assert timing.causes == {"remote_movement": 1}
        assert timing.last_cause is RetryCause.REMOTE_MOVEMENT
        assert correction.start_s == _EPOCH

    def test_record_job_rejects_invalid_durations(self) -> None:
        timing = WorkItemTiming()
        with pytest.raises((TypeError, ValueError)):
            timing.record_job(PhaseName.TEST_EXECUTION, float("inf"), None, _EPOCH)
        with pytest.raises((TypeError, ValueError)):
            timing.record_job(PhaseName.TEST_EXECUTION, -1.0, None, _EPOCH)

    def test_record_job_rejects_unbounded_error_text(self) -> None:
        timing = WorkItemTiming()
        with pytest.raises(ValueError):
            timing.record_job(PhaseName.TEST_EXECUTION, 1.0, "free " * 100, _EPOCH)

    def test_close_is_idempotent_and_stops_recording(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        timing.close(_EPOCH + 10.0)
        timing.close(_EPOCH + 30.0)
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH + 40.0)
        timing.begin_wait(PhaseName.EXTERNAL_WAIT, _EPOCH + 41.0)
        record = timing.phases[PhaseName.PLANNING]
        assert record.elapsed_s == pytest.approx(10.0)
        assert PhaseName.IMPLEMENTATION not in timing.phases
        assert PhaseName.EXTERNAL_WAIT not in timing.phases
        assert timing.current_phase is PhaseName.PLANNING
        assert timing.current_waiting is False

    def test_close_closes_an_open_wait_once(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.MERGE_ADMISSION, _EPOCH)
        timing.begin_wait(PhaseName.CI_WAIT, _EPOCH + 5.0)
        timing.close(_EPOCH + 15.0)
        wait = timing.phases[PhaseName.CI_WAIT]
        assert wait.wait_s == pytest.approx(10.0)
        assert timing.current_phase is PhaseName.CI_WAIT
        assert timing.current_waiting is True

    def test_separate_accumulators_keep_separate_identities(self) -> None:
        first = WorkItemTiming()
        second = WorkItemTiming()
        first.enter_phase(PhaseName.PLANNING, _EPOCH)
        second.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH + 1.0)
        first.record_cause(RetryCause.REVIEW_FINDINGS)
        second.record_cause(RetryCause.TOOL_FAILURE)
        assert first.causes == {"review_findings": 1}
        assert second.causes == {"tool_failure": 1}
        assert first.current_phase is PhaseName.PLANNING
        assert second.current_phase is PhaseName.IMPLEMENTATION

    def test_marks_are_finite_or_rejected(self) -> None:
        timing = WorkItemTiming()
        with pytest.raises((TypeError, ValueError)):
            timing.enter_phase(PhaseName.PLANNING, math.inf)
        with pytest.raises((TypeError, ValueError)):
            timing.enter_phase(PhaseName.PLANNING, math.nan)


class TestEncodeTimingReceipt:
    """The receipt encoder is fail-closed with an exact closed schema."""

    def _timing(self) -> WorkItemTiming:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        timing.leave_phase(PhaseName.PLANNING, _EPOCH + 10.0)
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH + 10.0)
        timing.record_job(PhaseName.TEST_EXECUTION, 30.0, None, _EPOCH + 40.0)
        timing.record_cause(RetryCause.TEST_FAILURE)
        timing.leave_phase(PhaseName.IMPLEMENTATION, _EPOCH + 110.0)
        timing.close(_EPOCH + 110.0)
        return timing

    def test_receipt_schema_is_exact(self) -> None:
        receipt = encode_timing_receipt(
            _identity(),
            self._timing(),
            run_identity="run-1",
            passed=True,
            final_stage="merge_wait",
            now_fn=lambda: _EPOCH + 120.0,
        )
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
        assert receipt["repo"] == "repo-a"
        assert receipt["issue"] == 7
        assert receipt["pr"] == 11
        assert receipt["head_sha"] == "a" * 40
        assert receipt["run_identity"] == "run-1"
        assert receipt["passed"] is True
        assert receipt["final_stage"] == "merge_wait"
        assert receipt["wall_s"] == pytest.approx(110.0)
        assert receipt["active_s"] == pytest.approx(110.0)
        assert receipt["wait_s"] == 0.0
        assert receipt["causes"] == {"test_failure": 1}
        assert receipt["last_cause"] == "test_failure"
        assert receipt["last_error_class"] is None
        assert receipt["current_phase"] is None
        assert receipt["current_waiting"] is False

    def test_phase_entries_have_the_closed_shape(self) -> None:
        receipt = encode_timing_receipt(
            _identity(),
            self._timing(),
            run_identity="run-1",
            passed=True,
            final_stage="merge_wait",
            now_fn=lambda: _EPOCH + 120.0,
        )
        phases = receipt["phases"]
        assert set(phases) == {"planning", "implementation", "test_execution"}
        for phase in phases.values():
            assert set(phase) == {
                "start_s",
                "end_s",
                "elapsed_s",
                "active_s",
                "wait_s",
                "attempts",
                "class",
            }
            assert phase["class"] in {"active", "waiting"}
        assert phases["planning"]["elapsed_s"] == pytest.approx(10.0)
        assert phases["test_execution"]["active_s"] == pytest.approx(30.0)
        assert phases["test_execution"]["attempts"] == 1

    def test_wait_phases_carry_the_waiting_class(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.MERGE_ADMISSION, _EPOCH)
        timing.begin_wait(PhaseName.CI_WAIT, _EPOCH + 1.0)
        timing.end_wait(_EPOCH + 11.0)
        receipt = encode_timing_receipt(
            _identity(),
            timing,
            run_identity="run-1",
            passed=False,
            final_stage="merge_wait",
            now_fn=lambda: _EPOCH + 20.0,
        )
        assert receipt["phases"]["ci_wait"]["class"] == "waiting"
        assert receipt["phases"]["merge_admission"]["class"] == "active"
        assert receipt["wait_s"] == pytest.approx(10.0)

    def test_stalled_item_reports_current_phase_and_last_cause(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.IMPLEMENTATION, _EPOCH)
        timing.record_cause(RetryCause.REVIEW_FINDINGS)
        timing.begin_wait(PhaseName.IMPLEMENTATION, _EPOCH + 5.0)
        timing.close(_EPOCH + 15.0)
        receipt = encode_timing_receipt(
            _identity(pr=None, head_sha=None),
            timing,
            run_identity="run-1",
            passed=False,
            final_stage="pr_review",
            now_fn=lambda: _EPOCH + 15.0,
        )
        assert receipt["current_phase"] == "implementation"
        assert receipt["current_waiting"] is True
        assert receipt["last_cause"] == "review_findings"
        assert receipt["pr"] is None
        assert receipt["head_sha"] is None
        assert receipt["passed"] is False

    def test_close_does_not_double_count_completed_intervals(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, _EPOCH)
        timing.close(_EPOCH + 10.0)
        first = encode_timing_receipt(
            _identity(),
            timing,
            run_identity="run-1",
            passed=True,
            final_stage="finished",
            now_fn=lambda: _EPOCH + 60.0,
        )
        second = encode_timing_receipt(
            _identity(),
            timing,
            run_identity="run-1",
            passed=True,
            final_stage="finished",
            now_fn=lambda: _EPOCH + 3600.0,
        )
        assert first == second

    def test_receipt_rejects_free_text_and_foreign_keys(self) -> None:
        with pytest.raises(ValueError):
            encode_timing_receipt(
                _identity(extra="leak"),
                self._timing(),
                run_identity="run-1",
                passed=True,
                final_stage="finished",
                now_fn=lambda: _EPOCH,
            )

    @pytest.mark.parametrize(
        "bad_sha",
        ["", "a" * 39, "A" * 40, "g" * 40, "/private/path/secret", "a" * 41],
    )
    def test_receipt_rejects_invalid_head_sha(self, bad_sha: str) -> None:
        with pytest.raises(ValueError):
            encode_timing_receipt(
                _identity(head_sha=bad_sha),
                self._timing(),
                run_identity="run-1",
                passed=True,
                final_stage="finished",
                now_fn=lambda: _EPOCH,
            )

    @pytest.mark.parametrize(
        "field",
        ["repo", "run_identity", "final_stage"],
    )
    def test_receipt_rejects_unbounded_text_fields(self, field: str) -> None:
        kwargs: dict[str, Any] = {
            "run_identity": "run-1",
            "passed": True,
            "final_stage": "finished",
            "now_fn": lambda: _EPOCH,
        }
        identity = _identity()
        if field in identity:
            identity[field] = "x" * 300
        else:
            kwargs[field] = "x" * 300
        with pytest.raises(ValueError):
            encode_timing_receipt(identity, self._timing(), **kwargs)

    def test_receipt_fields_contain_no_free_text(self) -> None:
        receipt = encode_timing_receipt(
            _identity(),
            self._timing(),
            run_identity="run-1",
            passed=True,
            final_stage="finished",
            now_fn=lambda: _EPOCH + 1.0,
        )
        text = repr(receipt)
        assert "/home" not in text
        assert "token" not in text.lower()
        assert "Bearer" not in text
