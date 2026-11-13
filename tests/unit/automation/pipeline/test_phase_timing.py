"""Unit tests for the bounded phase-timing module.

Covers the closed vocabularies, the retry-cause classification table, field
validation, accumulator idempotence, and the fail-closed receipt schema.
"""

from __future__ import annotations

import math

import pytest

from hephaestus.automation.pipeline.phase_timing import (
    PUBLICATION_OPS,
    TEST_FAILURE_ERROR_CLASS,
    PhaseName,
    PhaseRecord,
    RetryCause,
    WorkItemTiming,
    classify_job_phase,
    classify_retry_cause,
    classify_wait_phase,
    default_retry_cause,
    encode_timing_receipt,
)
from hephaestus.automation.pipeline.routing import ROUTES, StageName

_SHA = "a" * 40


def _identity(**overrides: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "repo": "org/repo",
        "issue": 7,
        "pr": None,
        "head_sha": None,
    }
    identity.update(overrides)
    return identity


class TestClosedVocabularies:
    """Every phase and cause value is closed and stage coverage is total."""

    def test_phase_vocabulary_matches_the_issue_list(self) -> None:
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

    def test_cause_vocabulary_matches_the_issue_list(self) -> None:
        assert {cause.value for cause in RetryCause} == {
            "source_change",
            "review_findings",
            "test_failure",
            "check_failure",
            "remote_movement",
            "tool_failure",
            "external_wait",
        }

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (StageName.REPO, PhaseName.ISSUE_INTAKE),
            (StageName.PLANNING, PhaseName.PLANNING),
            (StageName.PLAN_REVIEW, PhaseName.PLANNING),
            (StageName.IMPLEMENTATION, PhaseName.IMPLEMENTATION),
            (StageName.PR_REVIEW, PhaseName.IMPLEMENTATION),
            (StageName.MERGE_WAIT, PhaseName.MERGE_ADMISSION),
            (StageName.LEARNING, None),
            (StageName.FINISHED, None),
        ],
    )
    def test_phase_for_stage_covers_the_main_lane(
        self, stage: StageName, expected: PhaseName | None
    ) -> None:
        from hephaestus.automation.pipeline.phase_timing import PHASE_FOR_STAGE

        assert PHASE_FOR_STAGE.get(stage) is expected

    def test_publication_ops_are_a_closed_gitjob_subset(self) -> None:
        from hephaestus.automation.pipeline.git_jobs import GIT_OPS

        assert PUBLICATION_OPS <= GIT_OPS

    def test_every_route_note_key_is_classifiable_or_deliberately_unmapped(self) -> None:
        """Every named fail-route key across ROUTES maps to a closed cause.

        The ``*`` default and terminal-only keys (``closed``, ``exhaustion``,
        ``plan_cycles_exhausted``, budget names) are deliberately absent: they
        finish the item instead of starting a correction cycle.
        """
        from hephaestus.automation.pipeline.phase_timing import _NOTE_CAUSES

        terminal_only = {
            "closed",
            "exhaustion",
            "plan_cycles_exhausted",
            "merge_attempts_exhausted",
            # Learning resume routes continue interrupted work; they are not
            # failure-driven retry causes.
            "resume_implementation",
            "resume_plan_review",
        }
        for route in ROUTES.values():
            for note in route.fail_routes:
                if note == "*":
                    continue
                if note in terminal_only:
                    assert note not in _NOTE_CAUSES
                else:
                    assert note in _NOTE_CAUSES, note


class TestClassifyRetryCause:
    """The classification table maps notes and error classes to causes."""

    @pytest.mark.parametrize(
        ("note", "expected"),
        [
            ("plan_changed", RetryCause.SOURCE_CHANGE),
            ("empty_pr_diff", RetryCause.SOURCE_CHANGE),
            ("nogo", RetryCause.REVIEW_FINDINGS),
            ("plan_missing", RetryCause.REVIEW_FINDINGS),
            ("plan_not_go", RetryCause.REVIEW_FINDINGS),
            ("implementation_remediation", RetryCause.REVIEW_FINDINGS),
            ("scope_retraction_before_scope_block", RetryCause.REVIEW_FINDINGS),
            ("merge_blocked_by_unresolved_threads", RetryCause.REVIEW_FINDINGS),
            ("not_implementation_go", RetryCause.REVIEW_FINDINGS),
            ("merge_conflicting", RetryCause.REMOTE_MOVEMENT),
            ("merge_blocked_by_failing_checks", RetryCause.CHECK_FAILURE),
            ("agent_error", RetryCause.TOOL_FAILURE),
        ],
    )
    def test_note_map(self, note: str, expected: RetryCause) -> None:
        assert classify_retry_cause(StageName.MERGE_WAIT, note) is expected

    @pytest.mark.parametrize(
        ("error_class", "expected"),
        [
            (TEST_FAILURE_ERROR_CLASS, RetryCause.TEST_FAILURE),
            ("github_rate_limit", RetryCause.EXTERNAL_WAIT),
            ("github_unavailable", RetryCause.EXTERNAL_WAIT),
            ("lock_timeout", RetryCause.EXTERNAL_WAIT),
            ("lock_metadata_error", RetryCause.EXTERNAL_WAIT),
            ("publish_remote_head_changed", RetryCause.REMOTE_MOVEMENT),
            ("publish_lease_drift", RetryCause.REMOTE_MOVEMENT),
        ],
    )
    def test_error_class_map(self, error_class: str, expected: RetryCause) -> None:
        assert classify_retry_cause(StageName.IMPLEMENTATION, "", error_class) is expected

    @pytest.mark.parametrize(
        "error_class",
        ["timeout", "worker_crash", "error", "process_exit", "circuit_open", "git_operation"],
    )
    def test_unlisted_error_classes_fall_back_to_tool_failure(self, error_class: str) -> None:
        assert classify_retry_cause(StageName.PLANNING, "", error_class) is RetryCause.TOOL_FAILURE

    def test_poll_style_retry_without_failed_job_has_no_cause(self) -> None:
        assert classify_retry_cause(StageName.MERGE_WAIT, "merge_not_ready") is None
        assert classify_retry_cause(StageName.MERGE_WAIT, "", None) is None

    def test_unknown_note_with_failed_job_uses_the_error_class(self) -> None:
        cause = classify_retry_cause(StageName.IMPLEMENTATION, "no commits", "timeout")
        assert cause is RetryCause.TOOL_FAILURE

    def test_stable_cause_values_round_trip_through_the_enum(self) -> None:
        for cause in RetryCause:
            assert RetryCause(cause.value) is cause


class TestDefaultCause:
    """The per-stage default cause table."""

    def test_stage_defaults_keep_the_map_total(self) -> None:
        assert default_retry_cause(StageName.PLAN_REVIEW) is RetryCause.REVIEW_FINDINGS
        assert default_retry_cause(StageName.MERGE_WAIT) is RetryCause.CHECK_FAILURE
        for stage in StageName:
            assert isinstance(default_retry_cause(stage), RetryCause)


class TestClassifyWaitPhase:
    """The parked-wait classification table."""

    @pytest.mark.parametrize(
        ("stage", "wait_cause", "expected"),
        [
            (StageName.MERGE_WAIT, "stage", PhaseName.CI_WAIT),
            (StageName.IMPLEMENTATION, "rate_guard", PhaseName.EXTERNAL_WAIT),
            (StageName.PLANNING, "stage", PhaseName.PLANNING),
            (StageName.LEARNING, "stage", None),
        ],
    )
    def test_wait_phase_classification(
        self, stage: StageName, wait_cause: str, expected: PhaseName | None
    ) -> None:
        assert classify_wait_phase(stage, wait_cause) is expected


class TestClassifyJobPhase:
    """The job-to-phase classification table."""

    def test_build_test_job_maps_to_test_execution(self) -> None:
        assert (
            classify_job_phase("BuildTestJob", None, StageName.IMPLEMENTATION)
            is PhaseName.TEST_EXECUTION
        )

    @pytest.mark.parametrize("op", sorted(PUBLICATION_OPS))
    def test_publication_ops_map_to_publication(self, op: str) -> None:
        assert classify_job_phase("GitJob", op, StageName.IMPLEMENTATION) is PhaseName.PUBLICATION

    def test_other_gitjob_ops_use_the_stage_phase(self) -> None:
        assert (
            classify_job_phase("GitJob", "create_worktree", StageName.IMPLEMENTATION)
            is PhaseName.IMPLEMENTATION
        )

    def test_unmapped_stage_job_has_no_phase(self) -> None:
        assert classify_job_phase("GitHubJob", None, StageName.LEARNING) is None


class TestPhaseRecordValidation:
    """Field validation for PhaseRecord."""

    @pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan"), "1", True])
    def test_add_rejects_invalid_durations(self, bad: object) -> None:
        record = PhaseRecord()
        with pytest.raises(ValueError):
            record.add_active(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            record.add_wait(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            record.add_elapsed(bad)  # type: ignore[arg-type]

    def test_valid_increments_accumulate(self) -> None:
        record = PhaseRecord()
        record.add_active(1.5)
        record.add_wait(2.0)
        record.add_elapsed(0.5)
        record.count_attempt()
        record.count_attempt()
        assert record.active_s == 1.5
        assert record.wait_s == 2.0
        assert record.elapsed_s == 4.0
        assert record.attempts == 2


class TestWorkItemTiming:
    """Accumulator idempotence for WorkItemTiming."""

    def test_enter_leave_records_elapsed(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, 10.0)
        timing.leave_phase(PhaseName.PLANNING, 13.5)
        record = timing.phases[PhaseName.PLANNING]
        assert record.elapsed_s == pytest.approx(3.5)
        assert record.start_s == 10.0
        assert record.end_s == 13.5
        assert timing.wall_total_s() is None  # not closed yet

    def test_reentering_the_open_phase_is_idempotent(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.PLANNING, 10.0)
        timing.enter_phase(PhaseName.PLANNING, 11.0)
        timing.leave_phase(PhaseName.PLANNING, 12.0)
        assert timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(2.0)

    def test_leaving_a_closed_or_different_phase_is_a_noop(self) -> None:
        timing = WorkItemTiming()
        timing.leave_phase(PhaseName.PLANNING, 5.0)
        timing.enter_phase(PhaseName.PLANNING, 5.0)
        timing.leave_phase(PhaseName.PLANNING, 6.0)
        timing.leave_phase(PhaseName.PLANNING, 9.0)
        assert timing.phases[PhaseName.PLANNING].elapsed_s == pytest.approx(1.0)

    def test_begin_end_wait_accrues_wait_seconds(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.MERGE_ADMISSION, 0.0)
        timing.begin_wait(PhaseName.CI_WAIT, 1.0)
        assert timing.current_waiting is True
        timing.end_wait(4.0)
        assert timing.current_waiting is False
        assert timing.phases[PhaseName.CI_WAIT].wait_s == pytest.approx(3.0)
        assert timing.phases[PhaseName.CI_WAIT].elapsed_s == pytest.approx(3.0)
        timing.enter_phase(PhaseName.MERGE_ADMISSION, 4.0)
        timing.leave_phase(PhaseName.MERGE_ADMISSION, 5.0)
        assert timing.phases[PhaseName.MERGE_ADMISSION].elapsed_s == pytest.approx(2.0)

    def test_end_wait_without_wait_is_a_noop(self) -> None:
        timing = WorkItemTiming()
        timing.end_wait(9.0)
        assert timing.phases == {}

    def test_job_attribution_updates_active_and_error_class(self) -> None:
        timing = WorkItemTiming()
        timing.record_job(PhaseName.TEST_EXECUTION, 2.0, None)
        timing.record_job(PhaseName.TEST_EXECUTION, 1.0, TEST_FAILURE_ERROR_CLASS)
        record = timing.phases[PhaseName.TEST_EXECUTION]
        assert record.active_s == pytest.approx(3.0)
        assert record.elapsed_s == pytest.approx(3.0)
        assert timing.last_error_class == TEST_FAILURE_ERROR_CLASS

    @pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan"), "1", True])
    def test_job_attribution_rejects_invalid_durations(self, bad: object) -> None:
        timing = WorkItemTiming()
        with pytest.raises(ValueError):
            timing.record_job(PhaseName.TEST_EXECUTION, bad, None)  # type: ignore[arg-type]

    def test_attempt_counts_and_causes(self) -> None:
        timing = WorkItemTiming()
        timing.record_attempt(PhaseName.CORRECTION, RetryCause.TEST_FAILURE)
        timing.record_attempt(PhaseName.CORRECTION, RetryCause.TEST_FAILURE)
        timing.record_attempt(PhaseName.CORRECTION, RetryCause.REMOTE_MOVEMENT)
        record = timing.phases[PhaseName.CORRECTION]
        assert record.attempts == 3
        assert timing.causes == {"test_failure": 2, "remote_movement": 1}
        assert timing.last_cause is RetryCause.REMOTE_MOVEMENT

    def test_close_is_idempotent_and_closes_open_intervals(self) -> None:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.IMPLEMENTATION, 0.0)
        timing.begin_wait(PhaseName.EXTERNAL_WAIT, 2.0)
        timing.close(7.0)
        timing.close(100.0)
        assert timing.wall_end_s == 7.0
        assert timing.phases[PhaseName.EXTERNAL_WAIT].wait_s == pytest.approx(5.0)
        assert timing.phases[PhaseName.IMPLEMENTATION].elapsed_s == pytest.approx(2.0)
        assert timing.wall_total_s() == pytest.approx(7.0)

    def test_wall_total_requires_start_and_end(self) -> None:
        timing = WorkItemTiming()
        assert timing.wall_total_s() is None
        timing.enter_phase(PhaseName.PLANNING, 3.0)
        assert timing.wall_total_s() is None
        timing.close(9.0)
        assert timing.wall_total_s() == pytest.approx(6.0)


class TestEncodeTimingReceipt:
    """The fail-closed receipt schema."""

    def _filled_timing(self) -> WorkItemTiming:
        timing = WorkItemTiming()
        timing.enter_phase(PhaseName.ISSUE_INTAKE, 0.0)
        timing.leave_phase(PhaseName.ISSUE_INTAKE, 2.0)
        timing.enter_phase(PhaseName.IMPLEMENTATION, 2.0)
        timing.record_job(PhaseName.TEST_EXECUTION, 1.5, None)
        timing.record_attempt(PhaseName.CORRECTION, RetryCause.TEST_FAILURE)
        timing.close(10.0)
        return timing

    def test_receipt_schema_is_exact(self) -> None:
        receipt = encode_timing_receipt(
            _identity(pr=11, head_sha=_SHA),
            self._filled_timing(),
            run_identity="run-1",
            passed=True,
            final_stage="finished",
        )
        assert set(receipt) == {
            "repo",
            "issue",
            "pr",
            "head_sha",
            "run_identity",
            "passed",
            "final_stage",
            "wall_s",
            "active_s",
            "wait_s",
            "phases",
            "causes",
            "current_phase",
            "last_cause",
        }
        assert receipt["repo"] == "org/repo"
        assert receipt["issue"] == 7
        assert receipt["pr"] == 11
        assert receipt["head_sha"] == _SHA
        assert receipt["passed"] is True
        assert receipt["wall_s"] == pytest.approx(10.0)
        assert receipt["active_s"] == pytest.approx(1.5)
        assert receipt["causes"] == {"test_failure": 1}
        assert receipt["last_cause"] == "test_failure"
        phase = receipt["phases"]["issue_intake"]
        assert set(phase) == {"start_s", "end_s", "elapsed_s", "active_s", "wait_s", "attempts"}
        assert receipt["phases"]["issue_intake"]["elapsed_s"] == pytest.approx(2.0)

    def test_enums_round_trip_as_plain_strings(self) -> None:
        receipt = encode_timing_receipt(
            _identity(),
            self._filled_timing(),
            run_identity="run-1",
            passed=False,
            final_stage="implementation",
        )
        assert receipt["current_phase"] == "implementation"
        assert receipt["last_cause"] == "test_failure"
        assert all(isinstance(key, str) for key in receipt["phases"])

    def test_head_sha_must_be_full_lowercase_hex(self) -> None:
        timing = WorkItemTiming()
        for bad in ["A" * 40, "short", "g" * 40, 123]:
            with pytest.raises(ValueError):
                encode_timing_receipt(
                    _identity(head_sha=bad),
                    timing,
                    run_identity="run",
                    passed=True,
                    final_stage="finished",
                )
        receipt = encode_timing_receipt(
            _identity(head_sha=None),
            timing,
            run_identity="run",
            passed=True,
            final_stage="finished",
        )
        assert receipt["head_sha"] is None

    def test_identity_must_have_the_exact_closed_key_set(self) -> None:
        timing = WorkItemTiming()
        with pytest.raises(ValueError):
            encode_timing_receipt(
                {"repo": "r", "issue": 1},
                timing,
                run_identity="run",
                passed=True,
                final_stage="finished",
            )
        with pytest.raises(ValueError):
            encode_timing_receipt(
                {"repo": "r", "issue": 1, "pr": None, "head_sha": None, "extra": 1},
                timing,
                run_identity="run",
                passed=True,
                final_stage="finished",
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"repo": ""},
            {"repo": "x" * 257},
            {"repo": "bad\nrepo"},
            {"issue": -1},
            {"issue": True},
            {"pr": "7"},
        ],
    )
    def test_identity_fields_are_validated(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            encode_timing_receipt(
                _identity(**overrides),
                WorkItemTiming(),
                run_identity="run",
                passed=True,
                final_stage="finished",
            )

    def test_run_identity_and_final_stage_are_validated(self) -> None:
        timing = WorkItemTiming()
        with pytest.raises(ValueError):
            encode_timing_receipt(
                _identity(), timing, run_identity="", passed=True, final_stage="finished"
            )
        with pytest.raises(ValueError):
            encode_timing_receipt(
                _identity(), timing, run_identity="run", passed=True, final_stage=""
            )

    def test_run_identity_may_be_omitted(self) -> None:
        receipt = encode_timing_receipt(
            _identity(),
            WorkItemTiming(),
            run_identity=None,
            passed=True,
            final_stage="finished",
        )
        assert receipt["run_identity"] is None

    def test_non_mapping_identity_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            encode_timing_receipt(
                "not-a-mapping",  # type: ignore[arg-type]
                WorkItemTiming(),
                run_identity="run",
                passed=True,
                final_stage="finished",
            )

    def test_receipt_contains_no_free_text(self) -> None:
        timing = self._filled_timing()
        receipt = encode_timing_receipt(
            _identity(),
            timing,
            run_identity="run-1",
            passed=False,
            final_stage="merge_wait",
        )
        serialized = repr(sorted(receipt))
        for field_value in ("prompt", "/home/", "http", "token"):
            assert field_value not in serialized
        assert math.isfinite(receipt["wall_s"])
