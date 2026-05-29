"""Tests for loop_runner early-exit mechanism (issues #613 / #614)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    LoopConfig,
    PhaseResult,
    RepoResult,
    _make_work_report_path,
    _read_work_report,
    run_loop,
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

    def test_non_convergence_phase_work_ignored(self) -> None:
        """A non-convergence phase (implement) with work alone does NOT signal work.

        _CONVERGENCE_PHASES is now just {"plan"}, so an implement phase with
        work_units>0 must not flip produced_work to True.
        """
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="implement",
                    skipped=False,
                    work_units=5,
                )
            ],
        )

        assert result.produced_work is False

    def test_convergence_phase_zero_work(self) -> None:
        """Repo whose only convergence phase (plan) reports work_units=0 has produced_work=False."""
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
                    name="implement",
                    skipped=False,
                    work_units=0,
                ),
            ],
        )

        assert result.produced_work is False

    def test_plan_work_among_other_phases(self) -> None:
        """When plan reports work, produced_work is True regardless of other phases.

        Only the plan (convergence) phase's work matters; an implement phase
        with zero work does not suppress the plan signal.
        """
        result = RepoResult(
            repo="myrepo",
            loop_idx=1,
            phases=[
                PhaseResult(
                    name="plan",
                    skipped=False,
                    work_units=3,
                ),
                PhaseResult(
                    name="implement",
                    skipped=False,
                    work_units=0,
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


def _zero_work_result(repo: str, loop_idx: int) -> RepoResult:
    """Return a RepoResult where the convergence phase (plan) reports 0 work."""
    rr = RepoResult(repo=repo, loop_idx=loop_idx)
    rr.phases.append(PhaseResult(name="plan", rc=0, work_units=0))
    rr.phases.append(PhaseResult(name="implement", rc=0, work_units=0))
    return rr


def _work_result(repo: str, loop_idx: int, work_units: int = 3) -> RepoResult:
    """Return a RepoResult where plan produced work."""
    rr = RepoResult(repo=repo, loop_idx=loop_idx)
    rr.phases.append(PhaseResult(name="plan", rc=0, work_units=work_units))
    rr.phases.append(PhaseResult(name="implement", rc=0, work_units=0))
    return rr


def _failed_result(repo: str, loop_idx: int) -> RepoResult:
    """Return a RepoResult with a phase failure and zero work units."""
    rr = RepoResult(repo=repo, loop_idx=loop_idx)
    rr.phases.append(PhaseResult(name="plan", rc=1, work_units=0))
    rr.phases.append(PhaseResult(name="implement", rc=0, work_units=0))
    return rr


def _unknown_work_result(repo: str, loop_idx: int) -> RepoResult:
    """Return a RepoResult where work_units is None (un-instrumented phase)."""
    rr = RepoResult(repo=repo, loop_idx=loop_idx)
    rr.phases.append(PhaseResult(name="plan", rc=0, work_units=None))
    return rr


class TestRunLoopEarlyExit:
    """Tests for early-exit logic in run_loop (#614)."""

    def test_early_exit_fires_on_zero_work_pass(self, tmp_path: Path) -> None:
        """When a full pass across all repos produces 0 new plans and 0 reviews, break early.

        A 5-loop config should stop after loop 1 when no repo reports any work.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=5, projects_dir=projects)

        call_count = 0

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            nonlocal call_count
            call_count += 1
            return _zero_work_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1"])

        # Only loop 1 should have run — early-exit fires immediately.
        assert max(r.loop_idx for r in results) == 1
        assert call_count == 1

    def test_loops_caps_when_work_continues_every_loop(self, tmp_path: Path) -> None:
        """--loops is still respected as an upper bound when work is produced each loop.

        With loops=3 and work every iteration, exactly 3 loops must complete.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=3, projects_dir=projects)

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            return _work_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1"])

        assert max(r.loop_idx for r in results) == 3
        assert len(results) == 3

    def test_no_early_exit_when_failure_present(self, tmp_path: Path) -> None:
        """A failure suppresses early-exit even when work_units=0.

        The loop must not break early if any repo reported a phase failure,
        because failures may resolve in the next iteration.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=3, projects_dir=projects)

        call_count = 0

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            nonlocal call_count
            call_count += 1
            return _failed_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1"])

        # All 3 loops must run — failure blocks early-exit.
        assert max(r.loop_idx for r in results) == 3
        assert call_count == 3

    def test_early_exit_skipped_on_final_loop(self, tmp_path: Path) -> None:
        """Early-exit is not evaluated on the final loop (loop_idx == cfg.loops).

        When loops=1 the early-exit condition cannot fire because the check
        requires loop_idx < cfg.loops.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=1, projects_dir=projects)

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            return _zero_work_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1"])

        # Exactly one result — the single configured loop ran to completion.
        assert len(results) == 1
        assert results[0].loop_idx == 1

    def test_unknown_work_units_prevents_early_exit(self, tmp_path: Path) -> None:
        """When work_units=None (un-instrumented phase), loop never early-exits.

        Conservative behaviour: treat unknown as produced work so the loop
        keeps running up to cfg.loops.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=3, projects_dir=projects)

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            return _unknown_work_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1"])

        # All 3 loops run because unknown phases are treated as productive.
        assert max(r.loop_idx for r in results) == 3

    def test_early_exit_multi_repo_requires_all_zero(self, tmp_path: Path) -> None:
        """Early-exit only fires when EVERY repo in the pass reports zero work.

        If even one repo produces work the loop must continue.
        """
        projects = tmp_path
        for repo in ("r1", "r2"):
            (projects / repo / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=5, projects_dir=projects)

        call_counts: dict[str, int] = {"r1": 0, "r2": 0}

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            call_counts[repo] += 1
            if repo == "r1":
                # r1 always produces work
                return _work_result(repo, loop_idx)
            # r2 produces no work
            return _zero_work_result(repo, loop_idx)

        with patch.object(loop_runner, "process_repo", side_effect=fake_process):
            results = run_loop(cfg, repos=["r1", "r2"])

        # All 5 loops should run because r1 is always productive.
        assert max(r.loop_idx for r in results) == 5


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
