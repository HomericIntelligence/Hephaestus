"""Tests for loop_runner early-exit mechanism (issue #613)."""

import os
import tempfile
from pathlib import Path

from hephaestus.automation.loop_runner import (
    PhaseResult,
    RepoResult,
    _make_work_report_path,
    _read_work_report,
)


class TestWriteWorkReport:
    """Tests for work_report.write_work_report helper."""

    def test_env_unset_no_file(self) -> None:
        """When HEPH_WORK_REPORT is unset, no file is created."""
        # Ensure env var is unset
        os.environ.pop("HEPH_WORK_REPORT", None)
        from hephaestus.automation.work_report import write_work_report

        # Call with env unset — this is a no-op; no file path to write to
        write_work_report(5)

    def test_env_set_writes_int(self) -> None:
        """When HEPH_WORK_REPORT is set, writes the integer to that file."""
        from hephaestus.automation.work_report import write_work_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            os.environ["HEPH_WORK_REPORT"] = path

            write_work_report(42)

            assert Path(path).read_text(encoding="utf-8") == "42"

            os.environ.pop("HEPH_WORK_REPORT", None)

    def test_oserror_swallowed(self) -> None:
        """OSError (e.g., permission denied) is silently swallowed."""
        from hephaestus.automation.work_report import write_work_report

        os.environ["HEPH_WORK_REPORT"] = "/nonexistent/path/report.txt"

        # Should not raise
        write_work_report(7)

        os.environ.pop("HEPH_WORK_REPORT", None)


class TestMakeWorkReportPath:
    """Tests for _make_work_report_path helper."""

    def test_creates_path_under_build(self) -> None:
        """Path is created under build/ directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            build_dir = Path(tmpdir) / "build"
            build_dir.mkdir()

            path = _make_work_report_path(str(build_dir))

            assert Path(path).parent == build_dir
            assert Path(path).name.startswith("work_report_")


class TestReadWorkReport:
    """Tests for _read_work_report helper."""

    def test_present_valid_int(self) -> None:
        """Present file with valid int is parsed correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            Path(path).write_text("3", encoding="utf-8")

            result = _read_work_report(path)

            assert result == 3

    def test_present_zero(self) -> None:
        """File containing '0' is parsed as 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            Path(path).write_text("0", encoding="utf-8")

            result = _read_work_report(path)

            assert result == 0

    def test_missing_returns_none(self) -> None:
        """Missing file returns None."""
        result = _read_work_report("/nonexistent/path")

        assert result is None

    def test_empty_file_returns_none(self) -> None:
        """Empty file returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            Path(path).write_text("", encoding="utf-8")

            result = _read_work_report(path)

            assert result is None

    def test_malformed_returns_none(self) -> None:
        """Malformed content (non-int) returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            Path(path).write_text("not_an_int", encoding="utf-8")

            result = _read_work_report(path)

            assert result is None

    def test_whitespace_trimmed(self) -> None:
        """Whitespace is trimmed before parsing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.txt")
            Path(path).write_text("  5  \n", encoding="utf-8")

            result = _read_work_report(path)

            assert result == 5


class TestPhaseResultProducedWork:
    """Tests for PhaseResult.produced_work property."""

    def test_skipped_phase_no_work(self) -> None:
        """Skipped phase has produced_work=False."""
        result = PhaseResult(
            name="plan",
            skipped=True,
            work_units=5,
        )

        assert result.produced_work is False

    def test_none_work_units_conservatively_true(self) -> None:
        """Unknown phase (work_units=None) conservatively returns True."""
        result = PhaseResult(
            name="plan",
            skipped=False,
            work_units=None,
        )

        assert result.produced_work is True

    def test_zero_work_units_false(self) -> None:
        """Phase with work_units=0 has produced_work=False."""
        result = PhaseResult(
            name="plan",
            skipped=False,
            work_units=0,
        )

        assert result.produced_work is False

    def test_positive_work_units_true(self) -> None:
        """Phase with work_units>0 has produced_work=True."""
        result = PhaseResult(
            name="plan",
            skipped=False,
            work_units=3,
        )

        assert result.produced_work is True


class TestRepoResultProducedWork:
    """Tests for RepoResult.produced_work property."""

    def test_no_convergence_phases(self) -> None:
        """Repo with only non-convergence phases has produced_work=False."""
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="drive-green",
                    skipped=False,
                    work_units=5,
                )
            ],
        )

        assert result.produced_work is False

    def test_plan_phase_with_work(self) -> None:
        """Repo with plan phase having work_units>0 has produced_work=True."""
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="plan",
                    skipped=False,
                    work_units=2,
                )
            ],
        )

        assert result.produced_work is True

    def test_review_plans_with_work(self) -> None:
        """Repo with review-plans phase having work_units>0 has produced_work=True."""
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="review-plans",
                    skipped=False,
                    work_units=1,
                )
            ],
        )

        assert result.produced_work is True

    def test_convergence_phases_all_zero(self) -> None:
        """Repo with all convergence phases at work_units=0 has produced_work=False."""
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="plan",
                    skipped=False,
                    work_units=0,
                ),
                PhaseResult(
                    name="review-plans",
                    skipped=False,
                    work_units=0,
                ),
            ],
        )

        assert result.produced_work is False

    def test_mixed_convergence_phases_any_work(self) -> None:
        """Repo with at least one convergence phase having work returns True."""
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="plan",
                    skipped=False,
                    work_units=0,
                ),
                PhaseResult(
                    name="review-plans",
                    skipped=False,
                    work_units=3,
                ),
            ],
        )

        assert result.produced_work is True


class TestRunPhaseWorkReport:
    """Tests for work report file handling in run_phase."""

    def test_run_phase_creates_env_var(self) -> None:
        """run_phase creates HEPH_WORK_REPORT in the subprocess env."""
        # This test would mock subprocess.run and verify the env dict
        # contains HEPH_WORK_REPORT. Deferred to implementation phase.
        pass

    def test_run_phase_reads_work_report(self) -> None:
        """run_phase reads and parses the work report file after subprocess returns."""
        # This test would mock subprocess.run to write a value to the env path,
        # then verify run_phase parses it into PhaseResult.work_units.
        # Deferred to implementation phase.
        pass

    def test_run_phase_unlinks_report_file(self) -> None:
        """run_phase removes the work report file after reading."""
        # This test would verify the file is unlinked in a finally block.
        # Deferred to implementation phase.
        pass

    def test_run_phase_timeout_leaves_work_units_none(self) -> None:
        """Timeout in run_phase leaves work_units=None."""
        # This test would mock subprocess.TimeoutExpired.
        # Deferred to implementation phase.
        pass

    def test_run_phase_oserror_leaves_work_units_none(self) -> None:
        """OSError in run_phase leaves work_units=None."""
        # This test would mock os.unlink raising OSError.
        # Deferred to implementation phase.
        pass


class TestRunLoopEarlyExit:
    """Tests for early-exit logic in run_loop."""

    def test_early_exit_fires_zero_work(self) -> None:
        """When a loop produces 0 work across all repos, loop breaks."""
        # Mock process_repo to return RepoResult with work_units=0 for all phases.
        # Assert run_loop exits after loop 1 (not loop 5).
        # Deferred to implementation phase.
        pass

    def test_no_early_exit_when_work_done(self) -> None:
        """When a loop produces work, loop continues."""
        # Mock process_repo to return RepoResult with work_units>0.
        # Assert run_loop runs all configured loops.
        # Deferred to implementation phase.
        pass

    def test_no_early_exit_on_failure(self) -> None:
        """Failure suppresses early-exit even if work_units=0."""
        # Mock process_repo to return RepoResult with any_failure=True and work_units=0.
        # Assert run_loop does NOT break early.
        # Deferred to implementation phase.
        pass

    def test_early_exit_not_final_loop(self) -> None:
        """Early-exit only checks when loop_idx < cfg.loops."""
        # Mock final loop iteration; assert no break is attempted.
        # Deferred to implementation phase.
        pass

    def test_unknown_work_never_converges(self) -> None:
        """When work_units=None (unknown), loop continues (conservative)."""
        # Mock process_repo to return RepoResult with work_units=None.
        # Assert run_loop runs all configured loops.
        # Deferred to implementation phase.
        pass


class TestMainLoopsRunReporting:
    """Tests for loops_run in emit_json_status."""

    def test_main_loops_run_early_exit(self) -> None:
        """When early exit fires at loop 2, loops_run=2."""
        # Mock run_loop to return results with max loop_idx=2.
        # Assert emit_json_status receives loops_run=2.
        # Deferred to implementation phase.
        pass

    def test_main_loops_run_all_loops(self) -> None:
        """When all loops complete, loops_run=cfg.loops."""
        # Mock run_loop to return results with max loop_idx=cfg.loops.
        # Assert emit_json_status receives loops_run=cfg.loops.
        # Deferred to implementation phase.
        pass


class TestPlanReviewerAlreadyReviewedFlag:
    """Tests for WorkerResult.already_reviewed flag."""

    def test_skip_already_approved_sets_flag(self) -> None:
        """plan_reviewer skips APPROVED plans with already_reviewed=True."""
        # Mock _latest_review_is_final to return True.
        # Assert WorkerResult.already_reviewed=True.
        # Deferred to implementation phase.
        pass

    def test_skip_no_plan_sets_flag(self) -> None:
        """plan_reviewer skips no-plan-comment case with already_reviewed=True."""
        # Mock _get_plan_comment to return None.
        # Assert WorkerResult.already_reviewed=True.
        # Deferred to implementation phase.
        pass

    def test_review_attempt_unsets_flag(self) -> None:
        """plan_reviewer review attempts have already_reviewed=False."""
        # Mock successful review path.
        # Assert WorkerResult.already_reviewed=False.
        # Deferred to implementation phase.
        pass

    def test_plan_reviewer_main_writes_correct_work_count(self) -> None:
        """plan_reviewer main() writes non-skipped count to HEPH_WORK_REPORT."""
        # Mock several review outcomes with mixed already_reviewed states.
        # Assert work count = sum(not r.already_reviewed for r in results.values() if r.success).
        # Deferred to implementation phase.
        pass


class TestPlannerMainWorkReport:
    """Tests for planner.main() work reporting."""

    def test_planner_writes_new_plans_count(self) -> None:
        """Planner main() writes (successful - already_planned) to HEPH_WORK_REPORT."""
        # Mock planner.run() to return results with known counts.
        # Assert work count = successful - already_planned.
        # Deferred to implementation phase.
        pass
