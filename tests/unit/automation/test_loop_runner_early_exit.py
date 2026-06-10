"""Tests for loop_runner early-exit mechanism (issues #613 / #614)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    LoopConfig,
    PhaseResult,
    RepoResult,
    _make_work_report_path,
    _read_work_report,
    run_loop,
)

if TYPE_CHECKING:
    from hephaestus.automation.plan_reviewer import PlanReviewer

# Patch target for the subprocess.run call inside run_phase. Patching the
# fully-qualified module attribute (rather than ``loop_runner.subprocess.run``)
# keeps mypy from flagging ``subprocess`` as a non-exported attribute.
SUBPROCESS_RUN = "hephaestus.automation.loop_runner.subprocess.run"


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
    """Tests for work report file handling in run_phase.

    ``run_phase`` injects ``HEPH_WORK_REPORT`` (a temp path under
    ``<projects_dir>/../build``) into the subprocess env, then in a ``finally``
    block reads the integer the child wrote there into ``PhaseResult.work_units``
    and unlinks the file. The ``drive-green`` phase is used here because its
    binary always resolves (``sys.executable`` + a script path), so argv
    construction never short-circuits before the env is built.
    """

    def _cfg(self, tmp_path: Path) -> LoopConfig:
        """Build a LoopConfig whose build dir (projects_dir/../build) is writable."""
        projects = tmp_path / "Projects"
        projects.mkdir()
        return LoopConfig(loops=1, projects_dir=projects, phase_timeout_s=30.0)

    def _run_phase(self, tmp_path: Path, cfg: LoopConfig) -> PhaseResult:
        """Invoke run_phase for the drive-green stage with stable arguments."""
        return loop_runner.run_phase(
            repo="r1",
            repo_dir=tmp_path,
            phase="drive-green",
            cfg=cfg,
            loop_idx=1,
            open_issues=[7],
            trunk_sha="abc1234",
        )

    def test_run_phase_creates_env_var(self, tmp_path: Path) -> None:
        """run_phase injects HEPH_WORK_REPORT into the subprocess env."""
        cfg = self._cfg(tmp_path)
        captured_env: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            captured_env.update(kwargs["env"])
            return MagicMock(returncode=0)

        with patch(SUBPROCESS_RUN, side_effect=fake_run):
            result = self._run_phase(tmp_path, cfg)

        assert "HEPH_WORK_REPORT" in captured_env
        # The path lives under the sibling build/ directory of projects_dir.
        report_path = Path(captured_env["HEPH_WORK_REPORT"])
        assert report_path.parent == cfg.projects_dir.parent / "build"
        assert result.rc == 0

    def test_run_phase_reads_work_report(self, tmp_path: Path) -> None:
        """run_phase parses the integer the child wrote into work_units."""
        cfg = self._cfg(tmp_path)

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            # Emulate a child phase reporting 4 work units.
            Path(kwargs["env"]["HEPH_WORK_REPORT"]).write_text("4", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch(SUBPROCESS_RUN, side_effect=fake_run):
            result = self._run_phase(tmp_path, cfg)

        assert result.work_units == 4
        assert result.rc == 0

    def test_run_phase_unlinks_report_file(self, tmp_path: Path) -> None:
        """run_phase removes the work report file after reading it."""
        cfg = self._cfg(tmp_path)
        seen_path: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            path = kwargs["env"]["HEPH_WORK_REPORT"]
            seen_path["path"] = path
            Path(path).write_text("2", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch(SUBPROCESS_RUN, side_effect=fake_run):
            self._run_phase(tmp_path, cfg)

        # The temp report file the child wrote must not survive the call.
        assert not Path(seen_path["path"]).exists()

    def test_run_phase_timeout_leaves_work_units_none(self, tmp_path: Path) -> None:
        """A subprocess timeout yields rc=124 and work_units=None."""
        cfg = self._cfg(tmp_path)

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30.0)

        with patch(SUBPROCESS_RUN, side_effect=fake_run):
            result = self._run_phase(tmp_path, cfg)

        assert result.rc == 124
        assert result.work_units is None
        assert result.error is not None and "timeout" in result.error

    def test_run_phase_oserror_leaves_work_units_none(self, tmp_path: Path) -> None:
        """An OSError launching the subprocess yields rc=126 and work_units=None."""
        cfg = self._cfg(tmp_path)

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            raise OSError("exec format error")

        with patch(SUBPROCESS_RUN, side_effect=fake_run):
            result = self._run_phase(tmp_path, cfg)

        assert result.rc == 126
        assert result.work_units is None
        assert result.error is not None and "OSError" in result.error


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
        """When a non-final pass produces 0 new plans and 0 reviews, break early.

        A 5-loop config with no final-loop-only phases should stop after loop 1
        when no repo reports any work.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=5, projects_dir=projects, phases=("plan", "implement"))

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

    def test_no_early_exit_while_drive_green_has_failing_prs(self, tmp_path: Path) -> None:
        """drive-green selected + a failing PR blocks early-exit before the final loop.

        Even with 0 new plan work, the loop must keep going while there is still
        drive-green work (an open PR that isn't green / implementation-go) to do.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=3, projects_dir=projects)  # default phases incl. drive-green

        call_count = 0

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            nonlocal call_count
            call_count += 1
            return _zero_work_result(repo, loop_idx)

        with (
            patch.object(loop_runner, "process_repo", side_effect=fake_process),
            patch.object(loop_runner, "_count_failing_prs", return_value=1),
        ):
            results = run_loop(cfg, repos=["r1"])

        # A failing PR keeps the loop running to the cap.
        assert max(r.loop_idx for r in results) == 3
        assert call_count == 3

    def test_early_exit_when_drive_green_selected_but_no_failing_prs(self, tmp_path: Path) -> None:
        """drive-green selected but NO failing PR + 0 plan work → converge early.

        The user's model: drive-green runs after the implement loop; once there
        is no plan work and every PR is green/implementation-go, stop — don't
        spin out the full --loops just because drive-green is selected.
        """
        projects = tmp_path
        (projects / "r1" / ".git").mkdir(parents=True)
        cfg = LoopConfig(loops=5, projects_dir=projects)  # default phases incl. drive-green

        call_count = 0

        def fake_process(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
            nonlocal call_count
            call_count += 1
            return _zero_work_result(repo, loop_idx)

        with (
            patch.object(loop_runner, "process_repo", side_effect=fake_process),
            patch.object(loop_runner, "_count_failing_prs", return_value=0),
        ):
            results = run_loop(cfg, repos=["r1"])

        # No plan work and no failing PR → early-exit after loop 1.
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
    """Tests for loops_run in emit_json_status.

    ``main`` derives ``loops_run`` from ``max(r.loop_idx for r in results)`` and
    forwards it to ``emit_json_status`` on the ``--json`` path. These tests pin
    run_loop's result list and assert the reported count, covering both the
    early-exit case (fewer loops than configured) and the full-run case.
    """

    def _run_main_with_results(
        self, results: list[RepoResult], configured_loops: int
    ) -> dict[str, object]:
        """Drive ``main`` with run_loop stubbed to ``results``; return JSON kwargs."""
        captured: dict[str, object] = {}

        def fake_emit(code: int, **kwargs: object) -> None:
            captured["code"] = code
            captured.update(kwargs)

        argv = [
            "--json",
            "--repos",
            "r1",
            "--loops",
            str(configured_loops),
            "--dry-run",
            "--agent",
            "claude",
        ]
        with (
            patch.object(loop_runner, "_resolve_org_and_repos", return_value=("Org", ["r1"], None)),
            patch.object(loop_runner, "_clone_missing_repos"),
            patch.object(loop_runner, "run_loop", return_value=results),
            patch.object(loop_runner, "emit_json_status", side_effect=fake_emit),
        ):
            rc = loop_runner.main(argv)

        captured["rc"] = rc
        return captured

    def test_main_loops_run_early_exit(self) -> None:
        """When early exit fires at loop 2 of 5, loops_run=2."""
        results = [
            RepoResult(
                repo="r1", loop_idx=1, phases=[PhaseResult(name="plan", rc=0, work_units=0)]
            ),
            RepoResult(
                repo="r1", loop_idx=2, phases=[PhaseResult(name="plan", rc=0, work_units=0)]
            ),
        ]

        captured = self._run_main_with_results(results, configured_loops=5)

        assert captured["loops_run"] == 2
        assert captured["failed_repos"] == []
        assert captured["rc"] == 0

    def test_main_loops_run_all_loops(self) -> None:
        """When all configured loops complete, loops_run==cfg.loops."""
        results = [
            RepoResult(repo="r1", loop_idx=i, phases=[PhaseResult(name="plan", rc=0, work_units=1)])
            for i in (1, 2, 3)
        ]

        captured = self._run_main_with_results(results, configured_loops=3)

        assert captured["loops_run"] == 3
        assert captured["failed_repos"] == []
        assert captured["rc"] == 0


class TestPlanReviewerAlreadyReviewedFlag:
    """Tests for WorkerResult.already_reviewed flag.

    ``already_reviewed`` is the per-issue convergence signal (#613): a
    short-circuited review (latest verdict already GO, or no plan to review)
    sets it True so it does NOT count as work, while an actual review pass
    leaves it False. ``plan_reviewer.main`` sums the False-and-successful
    results into the work report.
    """

    def _reviewer(self) -> PlanReviewer:
        from hephaestus.automation.models import PlanReviewerOptions
        from hephaestus.automation.plan_reviewer import PlanReviewer

        return PlanReviewer(
            PlanReviewerOptions(issues=[123], dry_run=False, max_workers=1, enable_ui=False)
        )

    def test_skip_already_approved_sets_flag(self) -> None:
        """A latest-GO plan short-circuits with success=True, already_reviewed=True."""
        reviewer = self._reviewer()
        with patch.object(reviewer, "_latest_review_is_final", return_value=True):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is True

    def test_skip_no_plan_sets_flag(self) -> None:
        """No plan comment short-circuits with success=True, already_reviewed=True."""
        reviewer = self._reviewer()
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(reviewer, "_get_latest_plan", return_value=None),
        ):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is True

    def test_review_attempt_unsets_flag(self) -> None:
        """A real review pass leaves already_reviewed=False."""
        reviewer = self._reviewer()
        with (
            patch.object(reviewer, "_latest_review_is_final", return_value=False),
            patch.object(reviewer, "_get_latest_plan", return_value="# Implementation Plan\nDo it"),
            patch(
                "hephaestus.automation.plan_reviewer.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(reviewer, "_run_claude_analysis", return_value="Looks good\nVerdict: GO"),
            patch.object(reviewer, "_post_review") as mock_post,
        ):
            result = reviewer._review_issue(123, slot_id=0)

        assert result.success is True
        assert result.already_reviewed is False
        mock_post.assert_called_once()

    def test_plan_reviewer_main_writes_correct_work_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() reports only successful, non-skipped reviews as work units."""
        from hephaestus.automation import plan_reviewer as plan_reviewer_mod
        from hephaestus.automation.models import WorkerResult

        # Two genuine reviews, one short-circuited skip, one failure → work=2.
        results = {
            1: WorkerResult(issue_number=1, success=True, already_reviewed=False),
            2: WorkerResult(issue_number=2, success=True, already_reviewed=False),
            3: WorkerResult(issue_number=3, success=True, already_reviewed=True),
            4: WorkerResult(issue_number=4, success=False, already_reviewed=False),
        }
        mock_reviewer = MagicMock()
        mock_reviewer.run.return_value = results
        captured: dict[str, int] = {}

        monkeypatch.setattr(
            "sys.argv",
            ["plan-reviewer", "--issues", "1", "2", "3", "4", "--agent", "claude"],
        )
        with (
            patch.object(plan_reviewer_mod, "PlanReviewer", return_value=mock_reviewer),
            patch.object(
                plan_reviewer_mod,
                "write_work_report",
                side_effect=lambda n: captured.__setitem__("work", n),
            ),
        ):
            rc = plan_reviewer_mod.main()

        # issue 4 failed → rc=1, but the work report still reflects the 2 real reviews.
        assert rc == 1
        assert captured["work"] == 2


class TestPlannerMainWorkReport:
    """Tests for planner.main() work reporting.

    The planner's convergence signal (#613) is the count of NEW plans:
    ``max(0, successful - already_planned)``. A pass that only re-confirms
    existing plans reports zero work, which lets the loop converge.
    """

    def test_planner_writes_new_plans_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() writes (successful - already_planned) new plans to the report."""
        from hephaestus.automation import planner as planner_mod
        from hephaestus.automation.models import PlanResult

        # 3 successes, 1 of which already had a plan → 2 new plans.
        results = {
            10: PlanResult(issue_number=10, success=True, plan_already_exists=False),
            11: PlanResult(issue_number=11, success=True, plan_already_exists=False),
            12: PlanResult(issue_number=12, success=True, plan_already_exists=True),
        }
        mock_planner = MagicMock()
        mock_planner.run.return_value = results
        captured: dict[str, int] = {}

        monkeypatch.setattr(
            "sys.argv", ["planner", "--issues", "10", "11", "12", "--agent", "claude"]
        )
        with (
            patch.object(planner_mod, "Planner", return_value=mock_planner),
            patch.object(
                planner_mod,
                "write_work_report",
                side_effect=lambda n: captured.__setitem__("work", n),
            ),
        ):
            rc = planner_mod.main()

        assert rc == 0
        assert captured["work"] == 2

    def test_planner_reports_zero_when_all_plans_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pass that only re-confirms existing plans reports zero work units."""
        from hephaestus.automation import planner as planner_mod
        from hephaestus.automation.models import PlanResult

        results = {
            10: PlanResult(issue_number=10, success=True, plan_already_exists=True),
            11: PlanResult(issue_number=11, success=True, plan_already_exists=True),
        }
        mock_planner = MagicMock()
        mock_planner.run.return_value = results
        captured: dict[str, int] = {}

        monkeypatch.setattr("sys.argv", ["planner", "--issues", "10", "11", "--agent", "claude"])
        with (
            patch.object(planner_mod, "Planner", return_value=mock_planner),
            patch.object(
                planner_mod,
                "write_work_report",
                side_effect=lambda n: captured.__setitem__("work", n),
            ),
        ):
            rc = planner_mod.main()

        assert rc == 0
        assert captured["work"] == 0


class TestHasPendingDriveGreenWork:
    """Convergence gate: drive-green pending-work detection (#1128 review)."""

    def test_not_selected_returns_false(self) -> None:
        """No drive-green phase → no terminal work to wait for."""
        cfg = LoopConfig(phases=("plan", "implement"))
        assert loop_runner._has_pending_drive_green_work(cfg, ["r1"]) is False

    def test_explicit_issues_keeps_looping(self) -> None:
        """--issues scope → keep looping (repo-wide PR scan is the wrong signal).

        ``_count_failing_prs`` has no issue filter, so a clean repo-wide scan
        would wrongly converge before the terminal drive-green pass runs against
        the pinned issues. Must return True and must NOT call _count_failing_prs.
        """
        cfg = LoopConfig(issues=[7, 8])  # default phases include drive-green
        with patch.object(loop_runner, "_count_failing_prs") as mock_count:
            assert loop_runner._has_pending_drive_green_work(cfg, ["r1"]) is True
        mock_count.assert_not_called()

    def test_failing_pr_means_pending(self) -> None:
        """A failing PR in any repo → pending drive-green work."""
        cfg = LoopConfig()  # default phases, no --issues
        with patch.object(loop_runner, "_count_failing_prs", side_effect=[0, 2]):
            assert loop_runner._has_pending_drive_green_work(cfg, ["r1", "r2"]) is True

    def test_no_failing_pr_means_converged(self) -> None:
        """All repos green → no pending drive-green work (loop may converge)."""
        cfg = LoopConfig()
        with patch.object(loop_runner, "_count_failing_prs", return_value=0):
            assert loop_runner._has_pending_drive_green_work(cfg, ["r1", "r2"]) is False
