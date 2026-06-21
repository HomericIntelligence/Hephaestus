"""Tests for drive-green as a per-issue blocking loop-body phase (#1560).

Supersedes the post-loop-terminal-stage model (#818): drive-green now runs
INSIDE the issue-major loop body, once per issue per loop (the blocking phase
that waits for each issue's PR to merge), rather than once per repo after all
loops. ``_run_post_loop_stages`` is no longer auto-invoked by ``run_loop`` —
``post_loop_phases`` is therefore always empty in the default path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    ALL_PHASES,
    ALL_POST_LOOP_STAGES,
    ALL_SELECTABLE,
    LoopConfig,
    PhaseResult,
)


def _ok(name: str, work_units: int = 0) -> PhaseResult:
    return PhaseResult(name=name, rc=0, work_units=work_units)


def _cfg(tmp_path: Path, **overrides: object) -> LoopConfig:
    projects = tmp_path / "Projects"
    projects.mkdir()
    base: dict[str, object] = {"projects_dir": projects, "org": "testorg"}
    base.update(overrides)
    return LoopConfig(**base)  # type: ignore[arg-type]


def _patch_run_loop_externals(calls: list):
    """Patch external collaborators of run_loop so tests are hermetic."""

    def _record(**kw):
        calls.append((kw["loop_idx"], kw["repo"], kw["phase"]))
        return _ok(kw["phase"], work_units=0)

    return [
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        # Post-merge: drive-green's work-discovery gate is _count_failing_prs
        # (#819). Return >0 so the post-loop stage actually invokes run_phase.
        patch.object(loop_runner, "_count_failing_prs", return_value=1),
        patch.object(
            loop_runner,
            "_resolve_repo_dir",
            side_effect=lambda projects_dir, repo: projects_dir / repo,
        ),
        patch.object(loop_runner, "_clone_missing_repos"),
        patch.object(loop_runner, "_preflight_token_scopes"),
        patch.object(loop_runner, "run_phase", side_effect=_record),
    ]


def _ensure_repo_dirs(cfg: LoopConfig, repos: list[str]) -> None:
    for r in repos:
        (cfg.projects_dir / r / ".git").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Symbol-level invariants
# ---------------------------------------------------------------------------


def test_all_phases_excludes_drive_green() -> None:
    """ALL_PHASES is (plan, implement); drive-green is a post-loop stage."""
    assert ALL_PHASES == ("plan", "implement")
    assert ALL_POST_LOOP_STAGES == ("drive-green",)
    assert ALL_SELECTABLE == ("plan", "implement", "drive-green")


def test_final_loop_only_symbols_removed() -> None:
    """Regression: FINAL_LOOP_ONLY_PHASES and _has_pending_final_loop_phase deleted."""
    assert not hasattr(loop_runner, "FINAL_LOOP_ONLY_PHASES")
    assert not hasattr(loop_runner, "_has_pending_final_loop_phase")


# ---------------------------------------------------------------------------
# drive-green is now a per-issue loop-body phase (#1560)
# ---------------------------------------------------------------------------


def test_drive_green_alone_runs_per_issue_in_loop_body(tmp_path: Path) -> None:
    """`--phases drive-green` → drive-green runs per issue in the loop body, no plan/implement.

    With early-exit disabled (drive-green is not a convergence phase, and one
    issue is discovered each loop), it runs once per issue per loop. The result
    is recorded under ``phases`` (loop body), never ``post_loop_phases``.
    """
    cfg = _cfg(tmp_path, loops=1, phases=("drive-green",))
    repos = ["r1", "r2"]
    _ensure_repo_dirs(cfg, repos)
    calls: list = []
    cms = _patch_run_loop_externals(calls)
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5], cms[6]:
        results = loop_runner.run_loop(cfg, repos)
    dg_calls = [c for c in calls if c[2] == "drive-green"]
    loop_phase_calls = [c for c in calls if c[2] in ALL_PHASES]
    # One issue per repo, one loop → drive-green once per repo's issue.
    assert sorted(c[1] for c in dg_calls) == ["r1", "r2"], dg_calls
    assert loop_phase_calls == [], loop_phase_calls
    # Recorded in the loop body (phases), NOT post_loop_phases (#1560).
    assert all(not r.post_loop_phases for r in results), results
    body = [(r.repo, p.name) for r in results for p in r.phases if not p.skipped]
    assert sorted(body) == [("r1", "drive-green"), ("r2", "drive-green")]


# ---------------------------------------------------------------------------
# Acceptance criterion 2: drive-green alongside loop phases, runs once
# ---------------------------------------------------------------------------


def test_drive_green_with_loop_phases_runs_per_issue_each_loop(tmp_path: Path) -> None:
    """`--phases plan,implement,drive-green --loops 3` → all three run per issue, each loop.

    Issue-major (#1560): each loop runs plan→implement→drive-green for the
    discovered issue, so all three appear once per loop (3 loops → 3 each).
    """
    cfg = _cfg(tmp_path, loops=3, phases=ALL_SELECTABLE)
    repos = ["r1"]
    _ensure_repo_dirs(cfg, repos)
    calls: list = []

    def _record(**kw):
        calls.append((kw["loop_idx"], kw["repo"], kw["phase"]))
        # plan reports work to keep early-exit from firing
        wu = 1 if kw["phase"] == "plan" else 0
        return PhaseResult(name=kw["phase"], rc=0, work_units=wu)

    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "_count_failing_prs", return_value=1),
        patch.object(loop_runner, "_resolve_repo_dir", side_effect=lambda pd, r: pd / r),
        patch.object(loop_runner, "_clone_missing_repos"),
        patch.object(loop_runner, "_preflight_token_scopes"),
        patch.object(loop_runner, "run_phase", side_effect=_record),
    ):
        loop_runner.run_loop(cfg, repos)
    dg = [c for c in calls if c[2] == "drive-green"]
    plan = [c for c in calls if c[2] == "plan"]
    impl = [c for c in calls if c[2] == "implement"]
    # One issue, 3 loops, full sequence each loop → 3 of each phase.
    assert len(dg) == 3, dg
    assert len(plan) == 3, plan
    assert len(impl) == 3, impl


# ---------------------------------------------------------------------------
# Acceptance criterion 3: --phases plan,implement does NOT invoke drive-green
# ---------------------------------------------------------------------------


def test_phases_plan_implement_does_not_invoke_drive_green(tmp_path: Path) -> None:
    """`--phases plan,implement --loops 5` → no drive-green anywhere."""
    cfg = _cfg(tmp_path, loops=5, phases=("plan", "implement"))
    repos = ["r1", "r2"]
    _ensure_repo_dirs(cfg, repos)
    calls: list = []
    cms = _patch_run_loop_externals(calls)
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5], cms[6]:
        results = loop_runner.run_loop(cfg, repos)
    assert all(c[2] != "drive-green" for c in calls), calls
    assert all(not r.post_loop_phases for r in results), results


def test_phases_plan_only_does_not_invoke_drive_green(tmp_path: Path) -> None:
    """`--phases plan` → no drive-green anywhere."""
    cfg = _cfg(tmp_path, loops=3, phases=("plan",))
    repos = ["r1"]
    _ensure_repo_dirs(cfg, repos)
    calls: list = []
    cms = _patch_run_loop_externals(calls)
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5], cms[6]:
        results = loop_runner.run_loop(cfg, repos)
    assert all(c[2] != "drive-green" for c in calls), calls
    assert all(not r.post_loop_phases for r in results), results


# ---------------------------------------------------------------------------
# Early-exit interaction: drive-green runs in the loop body of loop 1 (#1560)
# ---------------------------------------------------------------------------


def test_early_exit_loop_1_runs_drive_green_in_loop_body(tmp_path: Path) -> None:
    """Early-exit (zero-work) after loop 1 still drives each issue green in-body.

    Under issue-major, drive-green runs per issue inside loop 1 (no separate
    post-loop pass). Early-exit then fires after loop 1, so plan and drive-green
    each run exactly once per repo.
    """
    cfg = _cfg(tmp_path, loops=5, phases=ALL_SELECTABLE)
    repos = ["r1", "r2"]
    _ensure_repo_dirs(cfg, repos)
    calls: list = []
    cms = _patch_run_loop_externals(calls)  # all phases report work_units=0
    with cms[0], cms[1], cms[2], cms[3], cms[4], cms[5], cms[6]:
        results = loop_runner.run_loop(cfg, repos)
    plan_calls = [c for c in calls if c[2] == "plan"]
    dg_calls = [c for c in calls if c[2] == "drive-green"]
    # Early-exit fires after loop 1 → plan invoked exactly once per repo
    assert sorted(c[1] for c in plan_calls) == ["r1", "r2"], plan_calls
    # drive-green ran in the loop body of loop 1, once per repo's issue
    assert sorted(c[1] for c in dg_calls) == ["r1", "r2"], dg_calls
    # No post-loop stage records anymore.
    assert all(not r.post_loop_phases for r in results), results


# ---------------------------------------------------------------------------
# Other regressions
# ---------------------------------------------------------------------------


def test_drive_green_without_implement_no_warning() -> None:
    """Drive-green without implement is now legitimate, not a warning."""
    cfg = LoopConfig(phases=("drive-green",))
    warnings = loop_runner._phase_order_warnings(cfg)
    assert all("drive-green" not in w for w in warnings), warnings


def test_drive_green_with_implement_still_no_warning() -> None:
    """Drive-green + implement also produces no warning."""
    cfg = LoopConfig(phases=("implement", "drive-green"))
    warnings = loop_runner._phase_order_warnings(cfg)
    assert all("drive-green" not in w for w in warnings), warnings


def test_plan_without_implement_still_warns() -> None:
    """Plan-only configuration still emits the predecessor warning."""
    cfg = LoopConfig(phases=("plan",))
    warnings = loop_runner._phase_order_warnings(cfg)
    assert any("plan" in w and "implement" in w for w in warnings)


def test_post_loop_phase_env_omits_loop_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-loop drive-green env no longer carries loop-index vars (#820/#1061).

    The HEPH_LOOP_INDEX/HEPH_TOTAL_LOOPS env-gating contract was removed; the
    terminal-pass semantics are expressed by the dedicated post-loop stage, not
    by env vars, so even ``loop_idx=cfg.loops`` must not inject them.
    """
    monkeypatch.delenv("HEPH_LOOP_INDEX", raising=False)
    monkeypatch.delenv("HEPH_TOTAL_LOOPS", raising=False)
    cfg = _cfg(tmp_path, loops=5)
    env = loop_runner._phase_env(cfg, loop_idx=cfg.loops, trunk_sha="abc", phase="drive-green")
    assert "HEPH_LOOP_INDEX" not in env
    assert "HEPH_TOTAL_LOOPS" not in env


def test_validate_phases_accepts_all_selectable() -> None:
    """_validate_phases accepts loop phases and post-loop stages."""
    assert loop_runner._validate_phases("plan,implement,drive-green") == (
        "plan",
        "implement",
        "drive-green",
    )
    assert loop_runner._validate_phases("drive-green") == ("drive-green",)
    with pytest.raises(SystemExit):
        loop_runner._validate_phases("nonexistent")


def test_run_post_loop_stages_skips_when_no_failing_prs(tmp_path: Path) -> None:
    """drive-green is skipped with reason 'no failing PRs' when nothing needs CI driving.

    Post-merge with #819 / PR #1060, drive-green's work-discovery gate is
    ``_count_failing_prs`` rather than the open-issues list — drive-green
    polls existing PRs and there is no work when every PR is already green.
    """
    cfg = _cfg(tmp_path, loops=1, phases=("drive-green",))
    repos = ["r1"]
    _ensure_repo_dirs(cfg, repos)
    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "_count_failing_prs", return_value=0),
        patch.object(loop_runner, "_resolve_repo_dir", side_effect=lambda pd, r: pd / r),
        patch.object(loop_runner, "run_phase") as run_phase_mock,
    ):
        results = loop_runner._run_post_loop_stages(cfg, repos)
    run_phase_mock.assert_not_called()
    assert len(results) == 1
    assert len(results[0].post_loop_phases) == 1
    assert results[0].post_loop_phases[0].skipped is True
    assert results[0].post_loop_phases[0].skip_reason == "no failing PRs"


def test_run_post_loop_stages_records_crash_in_runner_error(tmp_path: Path) -> None:
    """An exception in the helper is captured in runner_error, not propagated."""
    cfg = _cfg(tmp_path, loops=1, phases=("drive-green",))
    repos = ["r1"]
    _ensure_repo_dirs(cfg, repos)
    with (
        patch.object(loop_runner, "_rebase_main", side_effect=RuntimeError("boom")),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "_resolve_repo_dir", side_effect=lambda pd, r: pd / r),
    ):
        results = loop_runner._run_post_loop_stages(cfg, repos)
    assert len(results) == 1
    assert "RuntimeError: boom" in (results[0].runner_error or "")
