"""Tests for hephaestus.automation.loop_runner.

Focus: phase-isolation invariants. The whole reason for this module
existing is that the previous bash version silently aborted between
phases — these tests pin down that a Python phase failure (whether
subprocess rc!=0, raised exception, or worker crash) does NOT prevent
subsequent phases from being attempted.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    ALL_PHASES,
    ALL_SELECTABLE,
    LoopConfig,
    PhaseResult,
    RepoResult,
    _default_phase_timeout_s,
    _ensure_clone,
    _phase_order_warnings,
    _preflight_token_scopes,
    _rate_limit_remaining,
    _rebase_main,
    _resolve_phase_bin,
    _summarize_loop,
    _validate_phases,
    main,
    process_repo,
    run_loop,
    run_phase,
)
from hephaestus.automation.state_labels import STATE_SKIP
from hephaestus.constants import scripts_dir as _scripts_dir
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

# ---------------------------------------------------------------------------
# Phase topology — the 6→3 stage collapse (#455/#468/#484)
# ---------------------------------------------------------------------------


def test_all_phases_is_three_stage_pipeline() -> None:
    """The loop body collapsed to exactly (plan, implement); drive-green moved post-loop.

    Plan-review, PR-review, and address-review fold into plan/implement
    (#455/#468/#484); drive-green moved to ALL_POST_LOOP_STAGES (#818).
    """
    from hephaestus.automation.loop_runner import ALL_POST_LOOP_STAGES

    assert ALL_PHASES == ("plan", "implement")
    assert ALL_POST_LOOP_STAGES == ("drive-green",)


@pytest.mark.parametrize("dropped", ["review-plans", "review-prs", "address-review"])
def test_dropped_phases_do_not_resolve(dropped: str) -> None:
    """The three retired phases must not resolve to an executable bin."""
    assert _resolve_phase_bin(dropped) is None


@pytest.mark.parametrize("dropped", ["review-plans", "review-prs", "address-review"])
def test_dropped_phases_rejected_by_validation(dropped: str) -> None:
    """``--phases`` must reject a retired phase name as unknown."""
    with pytest.raises(SystemExit, match="Unknown phase"):
        _validate_phases(dropped)


@pytest.mark.parametrize("shim", ["review_plans.py", "review_issues.py", "address_review.py"])
def test_retired_loop_dispatch_shims_are_deleted(shim: str) -> None:
    """The loop-dispatch shim scripts the retired phases used must be gone.

    Their in-loop logic moved into the planner/implementer; nothing dispatches
    these scripts anymore. (The pr_reviewer/address_review MODULES and the
    manual ``hephaestus-review-prs`` CLI are deliberately kept — only these
    loop shims were removed.)
    """
    assert not (_scripts_dir() / shim).exists()


# ---------------------------------------------------------------------------
# CLI / config validation
# ---------------------------------------------------------------------------


def test_validate_phases_accepts_full_list() -> None:
    """Validate phases accepts full list."""
    assert _validate_phases(",".join(ALL_PHASES)) == ALL_PHASES


def test_validate_phases_accepts_subset() -> None:
    """Validate phases accepts subset."""
    assert _validate_phases("plan,implement") == ("plan", "implement")


def test_validate_phases_rejects_typo() -> None:
    """Validate phases rejects typo."""
    with pytest.raises(SystemExit, match="Unknown phase"):
        _validate_phases("plan,implmnt")


def test_phase_order_warnings_drive_green_no_longer_warns() -> None:
    """Per #818, drive-green without implement is a legitimate operator intent."""
    cfg_alone = LoopConfig(phases=("drive-green",))
    cfg_with = LoopConfig(phases=("implement", "drive-green"))
    assert all("drive-green" not in w for w in _phase_order_warnings(cfg_alone))
    assert all("drive-green" not in w for w in _phase_order_warnings(cfg_with))


def test_phase_order_warnings_plan_without_implement_warns() -> None:
    """Plan selected without implement makes the invocation planning-only."""
    cfg = LoopConfig(phases=("plan",))
    warnings = _phase_order_warnings(cfg)
    assert any("planning-only" in w and "implementation PRs" in w for w in warnings)


def test_phase_order_warnings_silent_on_full_pipeline() -> None:
    """Phase order warnings silent on full pipeline."""
    cfg = LoopConfig(phases=ALL_PHASES)
    assert _phase_order_warnings(cfg) == []


def test_parse_args_agent_defaults_to_auto_detect() -> None:
    """Omitted --agent should defer to runtime auto-detection."""
    args = loop_runner._parse_args([])
    assert args.agent is None


def test_parse_args_accepts_explicit_codex_agent() -> None:
    """Operators can still force Codex explicitly."""
    args = loop_runner._parse_args(["--agent", "codex"])
    assert args.agent == "codex"


def test_parse_args_accepts_no_advise() -> None:
    """The loop runner can disable advise across child phases."""
    args = loop_runner._parse_args(["--no-advise"])
    assert args.no_advise is True


def test_parse_args_accepts_nitpick() -> None:
    """The loop runner can enable nitpick comments across review phases."""
    assert loop_runner._parse_args(["--nitpick"]).nitpick is True
    assert loop_runner._parse_args([]).nitpick is False


def test_parse_args_accepts_github_throttle_options() -> None:
    """The loop runner accepts explicit child-phase GitHub throttle config."""
    args = loop_runner._parse_args(["--gh-global-rate", "4.5", "--gh-global-burst", "11"])
    assert args.gh_global_rate == 4.5
    assert args.gh_global_burst == 11.0


def test_build_phase_argv_implement_forwards_nitpick() -> None:
    """#1083: --nitpick threads into the implement phase argv when set."""
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        on = loop_runner._build_phase_argv("implement", LoopConfig(nitpick=True), open_issues=[1])
        off = loop_runner._build_phase_argv("implement", LoopConfig(nitpick=False), open_issues=[1])
    assert on is not None and off is not None
    assert "--nitpick" in on
    assert "--nitpick" not in off


def test_build_phase_argv_forwards_github_throttle_options() -> None:
    """Child phases receive explicit throttle CLI values from the loop runner."""
    cfg = LoopConfig(gh_global_rate=4.5, gh_global_burst=11)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/phase", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1])
    assert argv is not None
    assert argv[argv.index("--gh-global-rate") + 1] == "4.5"
    assert argv[argv.index("--gh-global-burst") + 1] == "11"


def test_parse_args_accepts_max_merge_attempts() -> None:
    """--max-merge-attempts is parsed; default is 1 (#1560)."""
    assert loop_runner._parse_args(["--max-merge-attempts", "3"]).max_merge_attempts == 3
    assert loop_runner._parse_args([]).max_merge_attempts == 1


def test_build_phase_argv_drive_green_forwards_max_merge_attempts() -> None:
    """drive-green argv carries --max-fix-iterations = cfg.max_merge_attempts (#1560)."""
    cfg = LoopConfig(max_merge_attempts=4, phases=ALL_SELECTABLE)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/dg", [])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[1])
    assert argv is not None
    assert argv[argv.index("--max-fix-iterations") + 1] == "4"


def test_build_phase_argv_non_drive_green_omits_max_fix_iterations() -> None:
    """plan/implement argv must not carry the drive-green merge-attempt flag."""
    cfg = LoopConfig(max_merge_attempts=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/p", [])):
        plan_argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1])
    assert plan_argv is not None
    assert "--max-fix-iterations" not in plan_argv


def test_process_one_issue_tags_skip_when_issue_owns_failing_pr() -> None:
    """A failed drive-green tags state:skip ONLY when the issue owns a stuck PR.

    #1560 + #1576: drive-green's rc is repo-level, so the tag is gated on the
    issue actually owning a genuinely-stuck PR (verified live).
    """
    cfg = LoopConfig(loops=1, phases=ALL_SELECTABLE, max_merge_attempts=2)

    def fake_run_phase(**kw: object) -> PhaseResult:
        name = str(kw["phase"])
        rc = 1 if name == "drive-green" else 0  # PR never merges
        return PhaseResult(name=name, rc=rc, elapsed_s=0.1)

    with (
        patch.object(loop_runner, "run_phase", side_effect=fake_run_phase),
        patch.object(loop_runner, "_issue_owns_genuinely_failing_pr", return_value=True),
        patch.object(loop_runner, "gh_issue_add_labels") as add_labels,
    ):
        loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=42,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )

    add_labels.assert_called_once_with(42, [STATE_SKIP])


def test_process_one_issue_no_skip_when_pr_pending_review() -> None:
    """#1576: a failed drive-green does NOT tag skip when the PR isn't stuck.

    A green-but-awaiting-review PR (or no PR / sibling's PR) makes the ownership
    guard return False, so the issue is never tagged despite the repo-level rc=1.
    """
    cfg = LoopConfig(loops=1, phases=ALL_SELECTABLE, max_merge_attempts=2)

    def fake_run_phase(**kw: object) -> PhaseResult:
        name = str(kw["phase"])
        return PhaseResult(name=name, rc=1 if name == "drive-green" else 0, elapsed_s=0.1)

    with (
        patch.object(loop_runner, "run_phase", side_effect=fake_run_phase),
        patch.object(loop_runner, "_issue_owns_genuinely_failing_pr", return_value=False),
        patch.object(loop_runner, "gh_issue_add_labels") as add_labels,
    ):
        loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=42,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )

    add_labels.assert_not_called()


def test_process_one_issue_no_skip_when_drive_green_merges() -> None:
    """A successful drive-green (merged) must NOT tag state:skip."""
    cfg = LoopConfig(loops=1, phases=ALL_SELECTABLE)
    with (
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
        patch.object(loop_runner, "gh_issue_add_labels") as add_labels,
    ):
        loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=42,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )
    add_labels.assert_not_called()


def test_process_one_issue_dry_run_does_not_tag_skip() -> None:
    """dry-run must not mutate GitHub state even when drive-green fails."""
    cfg = LoopConfig(loops=1, phases=ALL_SELECTABLE, dry_run=True)
    with (
        patch.object(
            loop_runner,
            "run_phase",
            side_effect=lambda **kw: PhaseResult(
                name=str(kw["phase"]), rc=1 if kw["phase"] == "drive-green" else 0
            ),
        ),
        patch.object(loop_runner, "gh_issue_add_labels") as add_labels,
    ):
        loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=42,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )
    add_labels.assert_not_called()


def test_filter_open_issues_drops_closed() -> None:
    """#1576: explicit --issues list drops closed issues before the phase loop."""

    def fake_is_closed(num: int, _cache: object) -> bool:
        return num == 1552

    with (
        patch.object(loop_runner, "prefetch_issue_states", return_value={}),
        patch.object(loop_runner, "is_issue_closed", side_effect=fake_is_closed),
    ):
        kept = loop_runner._filter_open_issues("r", [1554, 1552])
    assert kept == [1554]


def test_filter_open_issues_keeps_all_on_prefetch_failure() -> None:
    """Fail-open: a prefetch error keeps every issue (never silently drop work)."""
    with patch.object(loop_runner, "prefetch_issue_states", side_effect=RuntimeError("boom")):
        kept = loop_runner._filter_open_issues("r", [1554, 1552])
    assert kept == [1554, 1552]


def test_issue_owns_genuinely_failing_pr_true_when_stuck() -> None:
    """#1576: an issue owning a genuinely-stuck PR is tag-eligible."""
    with (
        patch.object(loop_runner, "find_pr_for_issue", return_value=1570),
        patch.object(loop_runner, "pr_is_genuinely_stuck", return_value=True),
    ):
        assert loop_runner._issue_owns_genuinely_failing_pr(1554) is True


def test_issue_owns_genuinely_failing_pr_false_when_pending_review() -> None:
    """#1576: a PR awaiting review is not stuck, so the issue is not tag-eligible."""
    with (
        patch.object(loop_runner, "find_pr_for_issue", return_value=1570),
        patch.object(loop_runner, "pr_is_genuinely_stuck", return_value=False),
    ):
        assert loop_runner._issue_owns_genuinely_failing_pr(1554) is False


def test_issue_owns_genuinely_failing_pr_false_when_no_pr() -> None:
    """#1576: an issue with no PR (e.g. closed / no-PR) is never tag-eligible."""
    with patch.object(loop_runner, "find_pr_for_issue", return_value=None):
        assert loop_runner._issue_owns_genuinely_failing_pr(1552) is False


def test_issue_owns_genuinely_failing_pr_false_on_lookup_error() -> None:
    """#1576: a PR-lookup failure is treated as not-stuck (never tag on uncertainty)."""
    with patch.object(loop_runner, "find_pr_for_issue", side_effect=RuntimeError("boom")):
        assert loop_runner._issue_owns_genuinely_failing_pr(1554) is False


def test_parse_args_accepts_issue_scope() -> None:
    """The loop runner can scope child phases to a comma-separated issue list."""
    args = loop_runner._parse_args(["--issues", "8, 13"])
    assert args.issues == [8, 13]


@pytest.mark.parametrize("bad", ["0", "-1", "33", "100"])
def test_parse_args_rejects_out_of_range_max_workers(bad: str) -> None:
    """Regression for #723: loop_runner must reject --max-workers outside 1-32."""
    with pytest.raises(SystemExit) as excinfo:
        loop_runner._parse_args(["--max-workers", bad])
    assert excinfo.value.code == 2


def test_parse_args_accepts_valid_max_workers() -> None:
    """Valid --max-workers in range 1-32 accepted."""
    args = loop_runner._parse_args(["--max-workers", "8"])
    assert args.max_workers == 8


def test_parse_args_default_max_workers_is_three() -> None:
    """Omitted --max-workers defaults to 3."""
    args = loop_runner._parse_args([])
    assert args.max_workers == 3


# ---------------------------------------------------------------------------
# run_phase — never raises, always returns PhaseResult
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo_dir(tmp_path: Path) -> Path:
    """Create an empty fake repo dir with a .git/ marker."""
    d = tmp_path / "repo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


def test_run_phase_returns_rc_zero_on_success(fake_repo_dir: Path) -> None:
    """Run phase returns rc zero on success."""
    cfg = LoopConfig()
    completed = MagicMock(returncode=0)
    with (
        patch.object(loop_runner, "_resolve_phase_bin", return_value=("/bin/true", [])),
        patch("subprocess.run", return_value=completed) as run_mock,
    ):
        result = run_phase(
            repo="r",
            repo_dir=fake_repo_dir,
            phase="plan",
            cfg=cfg,
            loop_idx=1,
            open_issues=[],
            trunk_sha="abc1234",
        )
    assert isinstance(result, PhaseResult)
    assert result.rc == 0
    assert not result.failed
    assert result.name == "plan"
    run_mock.assert_called_once()


def test_run_phase_returns_rc_nonzero_does_not_raise(fake_repo_dir: Path) -> None:
    """Run phase returns rc nonzero does not raise."""
    cfg = LoopConfig()
    completed = MagicMock(returncode=7)
    with (
        patch.object(loop_runner, "_resolve_phase_bin", return_value=("/bin/false", [])),
        patch("subprocess.run", return_value=completed),
    ):
        result = run_phase(
            repo="r",
            repo_dir=fake_repo_dir,
            phase="implement",
            cfg=cfg,
            loop_idx=1,
            open_issues=[],
            trunk_sha="abc1234",
        )
    assert result.rc == 7
    assert result.failed


def test_run_phase_handles_timeout(fake_repo_dir: Path) -> None:
    """Run phase handles timeout."""
    cfg = LoopConfig(phase_timeout_s=0.01)
    with (
        patch.object(loop_runner, "_resolve_phase_bin", return_value=("/bin/sleep", [])),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.01),
        ),
    ):
        result = run_phase(
            repo="r",
            repo_dir=fake_repo_dir,
            phase="plan",
            cfg=cfg,
            loop_idx=1,
            open_issues=[],
            trunk_sha="abc1234",
        )
    assert result.rc == 124
    assert result.error is not None and "timeout" in result.error.lower()


def test_run_phase_handles_oserror(fake_repo_dir: Path) -> None:
    """Run phase handles oserror."""
    cfg = LoopConfig()
    with (
        patch.object(loop_runner, "_resolve_phase_bin", return_value=("/nope", [])),
        patch("subprocess.run", side_effect=OSError("no such file")),
    ):
        result = run_phase(
            repo="r",
            repo_dir=fake_repo_dir,
            phase="plan",
            cfg=cfg,
            loop_idx=1,
            open_issues=[],
            trunk_sha="abc1234",
        )
    assert result.rc == 126
    assert result.error is not None and "OSError" in result.error


def test_run_phase_handles_unresolved_binary(fake_repo_dir: Path) -> None:
    """Run phase handles unresolved binary."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=None):
        result = run_phase(
            repo="r",
            repo_dir=fake_repo_dir,
            phase="plan",
            cfg=cfg,
            loop_idx=1,
            open_issues=[],
            trunk_sha="abc1234",
        )
    assert result.rc == 127
    assert result.error is not None


# ---------------------------------------------------------------------------
# process_repo — phase isolation is the headline invariant
# ---------------------------------------------------------------------------


def _ok(name: str) -> PhaseResult:
    return PhaseResult(name=name, rc=0, elapsed_s=0.1)


def _fail(name: str, rc: int = 1) -> PhaseResult:
    return PhaseResult(name=name, rc=rc, elapsed_s=0.1)


@pytest.fixture
def repo_inputs(tmp_path: Path) -> tuple[Path, LoopConfig]:
    """Build a projects_dir + LoopConfig for process_repo tests."""
    projects = tmp_path
    (projects / "r" / ".git").mkdir(parents=True)
    cfg = LoopConfig(loops=1, projects_dir=projects)
    return projects, cfg


def test_process_repo_runs_selected_phases_per_issue(
    repo_inputs: tuple[Path, LoopConfig],
) -> None:
    """Issue-major (#1560): each issue runs the SELECTED phases; others skip.

    With the default ``--phases`` (plan, implement), drive-green is recorded
    skipped per issue. Phases that ran are the selected ones, once per issue.
    """
    _, cfg = repo_inputs  # default phases = ALL_PHASES = (plan, implement)
    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1, 2]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)

    ran = [p.name for p in result.phases if not p.skipped]
    # Each selected phase runs once per issue (2 issues × {plan, implement}).
    assert sorted(ran) == ["implement", "implement", "plan", "plan"]
    # drive-green is not selected by default → recorded skipped, never run.
    assert all(p.skipped for p in result.phases if p.name == "drive-green")
    assert not result.any_failure


def test_process_repo_drive_green_runs_per_issue_when_selected(
    repo_inputs: tuple[Path, LoopConfig],
) -> None:
    """When drive-green is selected it runs per issue (the blocking phase)."""
    projects, _ = repo_inputs
    cfg = LoopConfig(loops=1, projects_dir=projects, phases=ALL_SELECTABLE)
    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[7]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)

    ran = [p.name for p in result.phases if not p.skipped]
    # One issue, full selectable sequence in order.
    assert ran == list(ALL_SELECTABLE)


def test_process_one_issue_continues_after_phase_failure() -> None:
    """Phase isolation per issue: a failed plan must NOT skip later phases."""
    call_order: list[str] = []

    def fake_run_phase(**kw: object) -> PhaseResult:
        name = str(kw["phase"])
        call_order.append(name)
        rc = 1 if name == "plan" else 0
        return PhaseResult(name=name, rc=rc, elapsed_s=0.1)

    cfg = LoopConfig(loops=1, phases=ALL_SELECTABLE)
    with patch.object(loop_runner, "run_phase", side_effect=fake_run_phase):
        phases = loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=1,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )

    assert call_order == list(ALL_SELECTABLE), "a failed phase must not skip subsequent phases"
    assert phases[0].failed  # plan
    assert not phases[1].failed  # implement
    assert any(p.failed for p in phases)


def test_process_one_issue_skips_disabled_phases() -> None:
    """--phases selection is honored per issue: unselected phases are skipped."""
    ran: list[str] = []

    def fake_run_phase(**kw: object) -> PhaseResult:
        ran.append(str(kw["phase"]))
        return PhaseResult(name=str(kw["phase"]), rc=0, elapsed_s=0.1)

    cfg = LoopConfig(loops=1, phases=("implement",))  # only implement selected
    with patch.object(loop_runner, "run_phase", side_effect=fake_run_phase):
        phases = loop_runner._process_one_issue(
            repo="r",
            repo_dir=Path("/tmp/r"),
            issue=1,
            cfg=cfg,
            loop_idx=1,
            trunk_sha="abc1234",
        )

    assert ran == ["implement"], "only the selected phase runs"
    assert {p.name for p in phases if p.skipped} == {"plan", "drive-green"}


def test_process_repo_swallows_worker_exceptions(repo_inputs: tuple[Path, LoopConfig]) -> None:
    """If process_repo itself blows up, it returns a RepoResult — never raises."""
    _, cfg = repo_inputs
    with patch.object(loop_runner, "_rebase_main", side_effect=RuntimeError("boom")):
        result = process_repo("r", loop_idx=1, cfg=cfg)
    assert isinstance(result, RepoResult)
    assert result.runner_error is not None
    assert "boom" in result.runner_error
    assert result.any_failure


def test_process_repo_skips_disabled_phases(repo_inputs: tuple[Path, LoopConfig]) -> None:
    """Process repo skips disabled phases."""
    _, cfg = repo_inputs
    cfg = LoopConfig(loops=1, projects_dir=cfg.projects_dir, phases=("plan",))
    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)
    by_name = {p.name: p for p in result.phases}
    assert not by_name["plan"].skipped
    for name in ALL_PHASES[1:]:
        assert by_name[name].skipped
        assert by_name[name].skip_reason == "disabled by --phases"


def test_process_repo_logs_trunk_stale_when_fetch_fails(
    repo_inputs: tuple[Path, LoopConfig], caplog: pytest.LogCaptureFixture
) -> None:
    """When _rebase_main returns fetch_ok=False, the [repo] trunk= log line carries '(stale)'.

    Operators see refresh failures (#993).
    """
    _, cfg = repo_inputs
    with (
        patch.object(loop_runner, "_rebase_main", return_value=("abc1234", False)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
        caplog.at_level("INFO", logger="hephaestus.automation.loop_runner"),
    ):
        process_repo("r", loop_idx=1, cfg=cfg)
    assert any("trunk=abc1234 (stale)" in rec.message for rec in caplog.records), [
        r.message for r in caplog.records
    ]


# ---------------------------------------------------------------------------
# run_loop — outer driver swallows future exceptions
# ---------------------------------------------------------------------------


def test_run_loop_swallows_future_exception(repo_inputs: tuple[Path, LoopConfig]) -> None:
    """Swallow future exceptions so the outer loop survives.

    If a future itself somehow raises (process_repo's safety net is bypassed),
    run_loop still records a RepoResult with runner_error and continues.
    """
    _, cfg = repo_inputs

    def crash(*_args: object, **_kw: object) -> RepoResult:
        raise RuntimeError("simulated thread crash")

    with patch.object(loop_runner, "process_repo", side_effect=crash):
        results = run_loop(cfg, repos=["r1", "r2"])

    assert len(results) == 2
    assert all(r.runner_error is not None for r in results)
    assert all("simulated thread crash" in (r.runner_error or "") for r in results)


def test_run_loop_continues_when_one_repo_fails(repo_inputs: tuple[Path, LoopConfig]) -> None:
    """Run loop continues when one repo fails."""
    _, cfg = repo_inputs

    def fake(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
        rr = RepoResult(repo=repo, loop_idx=loop_idx)
        if repo == "broken":
            rr.runner_error = "boom"
        else:
            rr.phases.append(PhaseResult(name="plan", rc=0))
        return rr

    with patch.object(loop_runner, "process_repo", side_effect=fake):
        results = run_loop(cfg, repos=["ok1", "broken", "ok2"])

    by_repo = {r.repo: r for r in results}
    assert by_repo["broken"].runner_error == "boom"
    assert not by_repo["ok1"].any_failure
    assert not by_repo["ok2"].any_failure


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------


def test_build_phase_argv_plan_forwards_open_issues_when_unscoped() -> None:
    """Unscoped plan forwards the loop-discovered open-issue list via --issues.

    This avoids the child phase re-running its own ``gh issue list`` — the
    loop already discovered the issues once per loop and passes them down.
    """
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1, 2])
    assert argv is not None
    assert "--issues" in argv
    assert argv[argv.index("--issues") + 1 : argv.index("--issues") + 3] == ["1", "2"]


def test_build_phase_argv_plan_omits_issues_when_none_discovered() -> None:
    """Plan omits --issues when there are no open issues to forward."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None
    assert "--issues" not in argv


def test_build_phase_argv_implement_forwards_open_issues_when_unscoped() -> None:
    """Unscoped implement forwards the loop-discovered open-issue list via --issues."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[3, 5])
    assert argv is not None
    assert "--issues" in argv
    assert argv[argv.index("--issues") + 1 : argv.index("--issues") + 3] == ["3", "5"]


def test_build_phase_argv_plan_forwards_explicit_issues() -> None:
    """Plan receives the operator's explicit scope.

    When the loop is scoped, ``process_repo`` sets ``open_issues = cfg.issues``
    (loop_runner.py:1069), so the explicit scope flows through the open-issue
    list — that is what the child phase receives.
    """
    cfg = LoopConfig(issues=[8, 13])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[8, 13])
    assert argv is not None
    assert argv[argv.index("--issues") + 1 : argv.index("--issues") + 3] == ["8", "13"]


def test_build_phase_argv_implement_forwards_explicit_issues() -> None:
    """Implement receives the loop issue scope when set (via open_issues)."""
    cfg = LoopConfig(issues=[8])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[8])
    assert argv is not None
    assert argv[argv.index("--issues") + 1] == "8"


def test_build_phase_argv_forwards_resolved_agent() -> None:
    """Every child phase receives the concrete provider selected by the loop."""
    cfg = LoopConfig(agent="codex")
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None
    assert argv[argv.index("--agent") + 1] == "codex"


def test_build_phase_argv_drive_green_includes_issues() -> None:
    """drive-green passes explicit cfg.issues, but not open_issues (uses PR-discovery by default).

    PR-discovery auto-discovers failing PRs; open_issues is only used by other phases.
    """
    cfg = LoopConfig(issues=[7, 8])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[1, 2])
    assert argv is not None
    assert "--issues" in argv
    assert "7" in argv and "8" in argv
    # Important: the open_issues passed to _build_phase_argv are NOT used for drive-green
    # when cfg.issues is empty; only explicit cfg.issues are forwarded.


def test_build_phase_argv_drive_green_omits_issues_when_unscoped() -> None:
    """drive-green omits --issues when cfg.issues is empty (uses PR-discovery mode)."""
    cfg = LoopConfig(issues=[])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[7, 8])
    assert argv is not None
    assert "--issues" not in argv


def test_build_phase_argv_passes_dry_run() -> None:
    """Build phase argv passes dry run."""
    cfg = LoopConfig(dry_run=True)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None and "--dry-run" in argv


def test_build_phase_argv_passes_no_advise_to_all_phases() -> None:
    """Loop-level --no-advise is forwarded to every current child phase."""
    cfg = LoopConfig(no_advise=True)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/phase", [])):
        for phase in ("plan", "implement", "drive-green"):
            argv = loop_runner._build_phase_argv(phase, cfg, open_issues=[1])
            assert argv is not None
            assert "--no-advise" in argv


def test_build_phase_argv_implement_has_single_max_workers() -> None:
    """Regression: implement must not duplicate --max-workers."""
    cfg = LoopConfig(max_workers=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[])
    assert argv is not None
    assert argv.count("--max-workers") == 1


def test_build_phase_argv_plan_omits_no_ui() -> None:
    """Regression: plan does NOT receive --no-ui (per _PHASE_FLAGS)."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1])
    assert argv is not None
    assert "--no-ui" not in argv


def test_build_phase_argv_implement_includes_no_ui() -> None:
    """Implement DOES receive --no-ui."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[])
    assert argv is not None and "--no-ui" in argv


def test_build_phase_argv_implement_no_follow_up_on_loop_3() -> None:
    """Regression: implement gets --no-follow-up on loop >= 3 (bash FOLLOW_UP_FLAG)."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv_loop2 = loop_runner._build_phase_argv("implement", cfg, [], loop_idx=2)
        argv_loop3 = loop_runner._build_phase_argv("implement", cfg, [], loop_idx=3)
        argv_loop5 = loop_runner._build_phase_argv("implement", cfg, [], loop_idx=5)
    assert argv_loop2 is not None and "--no-follow-up" not in argv_loop2
    assert argv_loop3 is not None and "--no-follow-up" in argv_loop3
    assert argv_loop5 is not None and "--no-follow-up" in argv_loop5


def test_resolve_phase_bin_falls_back_to_python_module_when_console_script_absent() -> None:
    """Source checkouts without installed console scripts should still run phases."""
    with patch("hephaestus.automation.loop_runner.shutil.which", return_value=None):
        resolved = _resolve_phase_bin("plan")
    assert resolved is not None
    executable, leading = resolved
    assert executable == sys.executable
    assert leading == ["-m", "hephaestus.automation.planner"]


@pytest.mark.parametrize(
    ("phase", "script_name", "module"),
    [
        ("plan", "hephaestus-plan-issues", "hephaestus.automation.planner"),
        ("implement", "hephaestus-implement-issues", "hephaestus.automation.implementer"),
    ],
)
def test_resolve_phase_bin_ignores_broken_console_script_shebang(
    tmp_path: Path,
    phase: str,
    script_name: str,
    module: str,
) -> None:
    """Broken Pixi entry-point stubs must not block source-checkout fallback."""
    script = tmp_path / script_name
    script.write_text("#!/definitely/missing/python\nprint('stale')\n", encoding="utf-8")
    script.chmod(0o755)

    with patch("hephaestus.automation.loop_runner.shutil.which", return_value=str(script)):
        resolved = _resolve_phase_bin(phase)

    assert resolved == (sys.executable, ["-m", module])


def test_build_phase_argv_plan_uses_parallel_worker_flag() -> None:
    """Plan phase receives worker count via its --parallel flag."""
    cfg = LoopConfig(max_workers=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None
    assert "--max-workers" not in argv
    assert argv[argv.index("--parallel") + 1] == "4"


def test_build_phase_argv_drive_green_default_omits_all_flag() -> None:
    """Default drive-green (no --drive-green-all) omits --all flag (#821)."""
    cfg = LoopConfig(phases=("drive-green",))
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[1], loop_idx=1)
    assert argv is not None and "--all" not in argv


def test_build_phase_argv_drive_green_all_flag_appends_all() -> None:
    """--drive-green-all flag appends --all to drive-green phase argv (#821)."""
    cfg = LoopConfig(phases=("drive-green",), drive_green_all=True)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[1], loop_idx=1)
    assert argv is not None and "--all" in argv


def test_build_phase_argv_drive_green_all_flag_not_passed_to_other_phases() -> None:
    """--drive-green-all flag only affects drive-green phase, not others (#821)."""
    cfg = LoopConfig(phases=("implement",), drive_green_all=True)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[1], loop_idx=1)
    assert argv is not None and "--all" not in argv


def test_phase_env_does_not_inject_loop_index_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop env vars are no longer injected for any phase (#820)."""
    # _phase_env copies os.environ; clean any ambient vars so the assertion
    # tests injection behavior, not the inherited environment.
    monkeypatch.delenv("HEPH_LOOP_INDEX", raising=False)
    monkeypatch.delenv("HEPH_TOTAL_LOOPS", raising=False)
    cfg = LoopConfig(loops=5)
    for phase in ("plan", "implement", "drive-green"):
        env = loop_runner._phase_env(cfg, loop_idx=3, trunk_sha="abc", phase=phase)
        assert "HEPH_LOOP_INDEX" not in env, f"phase {phase} unexpectedly has HEPH_LOOP_INDEX"
        assert "HEPH_TOTAL_LOOPS" not in env, f"phase {phase} unexpectedly has HEPH_TOTAL_LOOPS"


def test_phase_env_model_vars_only_when_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model env vars only present when their cfg field is non-empty."""
    for var in ("HEPH_PLANNER_MODEL", "HEPH_REVIEWER_MODEL", "HEPH_IMPLEMENTER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HEPH_ADVISE_MODEL", raising=False)

    cfg_empty = LoopConfig()
    env = loop_runner._phase_env(cfg_empty, loop_idx=1, trunk_sha="abc", phase="plan")
    assert "HEPH_PLANNER_MODEL" not in env
    assert "HEPH_REVIEWER_MODEL" not in env
    assert "HEPH_IMPLEMENTER_MODEL" not in env
    assert "HEPH_ADVISE_MODEL" not in env

    cfg_set = LoopConfig(planner_model="opus", reviewer_model="sonnet", implementer_model="opus")
    env_set = loop_runner._phase_env(cfg_set, loop_idx=1, trunk_sha="abc", phase="plan")
    assert env_set["HEPH_PLANNER_MODEL"] == "opus"
    assert env_set["HEPH_REVIEWER_MODEL"] == "sonnet"
    assert env_set["HEPH_IMPLEMENTER_MODEL"] == "opus"


def test_phase_env_model_fans_out_to_worker_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """--model sets worker phase env vars; advise is selected at call sites."""
    for var in (
        "HEPH_PLANNER_MODEL",
        "HEPH_REVIEWER_MODEL",
        "HEPH_IMPLEMENTER_MODEL",
        "HEPH_ADVISE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = LoopConfig(model="claude-fable-5")
    env = loop_runner._phase_env(cfg, loop_idx=1, trunk_sha="abc", phase="plan")
    assert env["HEPH_PLANNER_MODEL"] == "claude-fable-5"
    assert env["HEPH_REVIEWER_MODEL"] == "claude-fable-5"
    assert env["HEPH_IMPLEMENTER_MODEL"] == "claude-fable-5"
    assert "HEPH_ADVISE_MODEL" not in env


def test_phase_env_per_phase_flag_overrides_catch_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-phase flag wins over the catch-all --model for that phase only."""
    for var in (
        "HEPH_PLANNER_MODEL",
        "HEPH_REVIEWER_MODEL",
        "HEPH_IMPLEMENTER_MODEL",
        "HEPH_ADVISE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = LoopConfig(model="claude-fable-5", reviewer_model="claude-sonnet-4-6")
    env = loop_runner._phase_env(cfg, loop_idx=1, trunk_sha="abc", phase="plan")
    assert env["HEPH_PLANNER_MODEL"] == "claude-fable-5"
    assert env["HEPH_REVIEWER_MODEL"] == "claude-sonnet-4-6"
    assert env["HEPH_IMPLEMENTER_MODEL"] == "claude-fable-5"
    assert "HEPH_ADVISE_MODEL" not in env


def test_parse_args_model_flag_wires_to_namespace() -> None:
    """--model parses into args.model (the path main() reads into cfg.model)."""
    args = loop_runner._parse_args(["--model", "claude-fable-5"])
    assert args.model == "claude-fable-5"
    # Default is empty so the catch-all is inert unless explicitly passed.
    assert loop_runner._parse_args([]).model == ""


def test_phase_env_trunk_sha_always_set() -> None:
    """HEPH_TRUNK_GITHASH is always exported (session naming relies on it)."""
    cfg = LoopConfig()
    for phase in loop_runner.ALL_PHASES:
        env = loop_runner._phase_env(cfg, loop_idx=1, trunk_sha="f936537", phase=phase)
        assert env["HEPH_TRUNK_GITHASH"] == "f936537"


# ---------------------------------------------------------------------------
# CLI scope refinements: fork filter, comma-only --repos, cwd default, --org
# ---------------------------------------------------------------------------


def test_parse_repo_list_comma_only() -> None:
    """Comma-separated input is parsed; whitespace is stripped."""
    assert loop_runner._parse_repo_list("foo, bar,baz") == ["foo", "bar", "baz"]
    assert loop_runner._parse_repo_list("") == []
    assert loop_runner._parse_repo_list("solo") == ["solo"]


def test_repos_argparse_rejects_space_separated() -> None:
    """Argparse treats space-separated values as positional; raises SystemExit."""
    with pytest.raises(SystemExit):
        loop_runner._parse_args(["--repos", "foo", "bar"])


def test_gh_list_repos_filters_forks_and_archived() -> None:
    """``isFork: true`` and ``isArchived: true`` entries are excluded.

    Repo names are NOT filtered — only isArchived/isFork status gates inclusion.
    """
    payload = (
        '[{"name":"keep","isFork":false,"isArchived":false},'
        '{"name":"drop-fork","isFork":true,"isArchived":false},'
        '{"name":"drop-archived","isFork":false,"isArchived":true},'
        '{"name":"Odysseus","isFork":false,"isArchived":false}]'
    )
    with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        names = loop_runner._gh_list_repos("MyOrg")
    # Odysseus is included — no name-based filtering (issue #814).
    assert sorted(names) == ["Odysseus", "keep"]
    invoked_argv = mock_run.call_args[0][0]
    assert "--no-archived" in invoked_argv
    assert "name,isArchived,isFork" in invoked_argv


def test_gh_list_repos_does_not_filter_by_name() -> None:
    """Regression for #814: only isArchived/isFork gate inclusion, never name."""
    payload = (
        '[{"name":"Odysseus","isFork":false,"isArchived":false},'
        '{"name":"Hephaestus","isFork":false,"isArchived":false},'
        '{"name":"AnyName","isFork":false,"isArchived":false}]'
    )
    with patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        names = loop_runner._gh_list_repos("MyOrg")
    assert sorted(names) == ["AnyName", "Hephaestus", "Odysseus"]


def test_list_open_issue_numbers_returns_all_open_sorted() -> None:
    """A single all-open query is parsed and sorted ascending."""

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="12\n7\n10\n", stderr="")

    with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
        nums = loop_runner._list_open_issue_numbers("MyOrg", "MyRepo")
    assert nums == [7, 10, 12]


def test_list_open_issue_numbers_queries_all_open_no_me_filter() -> None:
    """The canonical discovery is repo-wide open issues — NO @me filter.

    Matching the child phases' ``gh_list_open_issues`` semantics keeps the
    loop's convergence / failing-PR gates in agreement with the phases.
    """
    seen_argv: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_argv.extend(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
        loop_runner._list_open_issue_numbers("Org", "Repo")
    assert "--author" not in seen_argv
    assert "--assignee" not in seen_argv
    assert "@me" not in seen_argv
    assert "--repo" in seen_argv
    assert "Org/Repo" in seen_argv
    assert seen_argv[seen_argv.index("--state") + 1] == "open"


def test_resolve_org_and_repos_cwd_default() -> None:
    """No flags + cwd is a github repo → run for that single repo."""
    args = loop_runner._parse_args([])
    with patch(
        "hephaestus.automation.loop_runner._detect_cwd_repo",
        return_value=("MyOrg", "MyRepo"),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "MyOrg"
    assert repos == ["MyRepo"]


def test_resolve_org_and_repos_errors_when_no_scope_and_not_git() -> None:
    """No flags + cwd is not a github repo → return error message."""
    args = loop_runner._parse_args([])
    with patch(
        "hephaestus.automation.loop_runner._detect_cwd_repo",
        return_value=(None, None),
    ):
        _, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is not None
    assert "cwd is not a github.com repo" in err
    assert repos == []


def test_resolve_org_and_repos_org_no_arg_autodetects() -> None:
    """``--org`` with no value → detect org from cwd, enumerate."""
    args = loop_runner._parse_args(["--org"])
    assert args.org is loop_runner._ORG_AUTODETECT
    with (
        patch(
            "hephaestus.automation.loop_runner._detect_cwd_repo",
            return_value=("DetectedOrg", "AnyRepo"),
        ),
        patch(
            "hephaestus.automation.loop_runner._gh_list_repos",
            return_value=["a", "b"],
        ) as mock_list,
        patch(
            "hephaestus.automation.loop_runner._sort_repos_by_open_count",
            side_effect=lambda _org, r: r,
        ),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "DetectedOrg"
    assert repos == ["a", "b"]
    mock_list.assert_called_once_with("DetectedOrg")


def test_resolve_org_and_repos_org_no_arg_errors_when_not_git() -> None:
    """``--org`` with no value + cwd not a github repo → error."""
    args = loop_runner._parse_args(["--org"])
    with patch(
        "hephaestus.automation.loop_runner._detect_cwd_repo",
        return_value=(None, None),
    ):
        _, _, err = loop_runner._resolve_org_and_repos(args)
    assert err is not None
    assert "--org with no argument" in err


def test_resolve_org_and_repos_org_named() -> None:
    """``--org NAME`` enumerates the named org without cwd detection."""
    args = loop_runner._parse_args(["--org", "ExplicitOrg"])
    with (
        patch(
            "hephaestus.automation.loop_runner._detect_cwd_repo",
        ) as mock_detect,
        patch(
            "hephaestus.automation.loop_runner._gh_list_repos",
            return_value=["x"],
        ),
        patch(
            "hephaestus.automation.loop_runner._sort_repos_by_open_count",
            side_effect=lambda _org, r: r,
        ),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "ExplicitOrg"
    assert repos == ["x"]
    mock_detect.assert_not_called()


def test_resolve_org_and_repos_repos_flag_uses_cwd_org() -> None:
    """``--repos foo,bar`` uses cwd-detected org without enumerating."""
    args = loop_runner._parse_args(["--repos", "foo,bar"])
    assert args.repos == ["foo", "bar"]
    with (
        patch(
            "hephaestus.automation.loop_runner._detect_cwd_repo",
            return_value=("CwdOrg", "Whatever"),
        ),
        patch("hephaestus.automation.loop_runner._gh_list_repos") as mock_list,
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "CwdOrg"
    assert repos == ["foo", "bar"]
    mock_list.assert_not_called()


def test_resolve_org_and_repos_repos_flag_falls_back_to_explicit_org() -> None:
    """``--repos foo --org Bar`` (not in a git repo) uses ``Bar`` as the org."""
    args = loop_runner._parse_args(["--repos", "foo", "--org", "Bar"])
    with patch(
        "hephaestus.automation.loop_runner._detect_cwd_repo",
        return_value=(None, None),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "Bar"
    assert repos == ["foo"]


def test_resolve_org_and_repos_repos_flag_prefers_explicit_org() -> None:
    """``--repos foo --org Bar`` should not be overridden by the cwd repo's org."""
    args = loop_runner._parse_args(["--repos", "foo", "--org", "Bar"])
    with patch(
        "hephaestus.automation.loop_runner._detect_cwd_repo",
        return_value=("CwdOrg", "CurrentRepo"),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "Bar"
    assert repos == ["foo"]


def test_detect_cwd_repo_parses_ssh_url() -> None:
    """SSH origin ``git@github.com:Org/Repo.git`` yields ``Org``."""

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="/tmp/MyRepo\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="git@github.com:MyOrg/MyRepo.git\n", stderr=""
        )

    with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
        org, repo = loop_runner._detect_cwd_repo()
    assert org == "MyOrg"
    assert repo == "MyRepo"


def test_detect_cwd_repo_parses_https_url() -> None:
    """HTTPS origin ``https://github.com/Org/Repo`` yields ``Org``."""

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="/tmp/R\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="https://github.com/HOrg/R.git\n", stderr=""
        )

    with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
        org, repo = loop_runner._detect_cwd_repo()
    assert org == "HOrg"
    assert repo == "R"


def test_detect_cwd_repo_returns_none_when_not_git() -> None:
    """``git rev-parse`` failure → ``(None, None)``."""
    with patch(
        "hephaestus.automation.loop_repo_manager.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, ["git"]),
    ):
        org, repo = loop_runner._detect_cwd_repo()
    assert (org, repo) == (None, None)


# ---------------------------------------------------------------------------
# _summarize_loop
# ---------------------------------------------------------------------------


class TestSummarizeLoop:
    """Tests for loop summary generation."""

    def test_empty_loop_results(self) -> None:
        """Empty loop (no repos) produces zero counts."""
        summary = _summarize_loop([], 1, 10.5)
        assert summary == "loop 1: planned=0 implemented=0 skipped=0 elapsed=10s"

    def test_all_skipped_phases(self) -> None:
        """All skipped phases counted in skipped column."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [
            PhaseResult("plan", skipped=True, skip_reason="no issues"),
            PhaseResult("implement", skipped=True, skip_reason="no issues"),
        ]
        summary = _summarize_loop([repo_result], 2, 5.0)
        assert "planned=0" in summary
        assert "implemented=0" in summary
        assert "skipped=2" in summary
        assert "loop 2" in summary

    def test_counts_plan_phase(self) -> None:
        """Plan phase with unknown work_units falls back to counting as 1.

        ``work_units is None`` means the phase did not report (un-instrumented),
        so it is counted conservatively as 1 — matching ``produced_work``'s
        "unknown counts as work" convention.
        """
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [PhaseResult("plan", rc=0)]
        summary = _summarize_loop([repo_result], 1, 3.0)
        assert "planned=1" in summary

    def test_plan_counts_work_units(self) -> None:
        """Plan count reflects the issues actually planned (work_units)."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [PhaseResult("plan", rc=0, work_units=3)]
        summary = _summarize_loop([repo_result], 1, 3.0)
        assert "planned=3" in summary

    def test_plan_zero_work_units_counts_zero(self) -> None:
        """A plan phase that ran but planned 0 issues must not inflate the count.

        Regression for the 2026-06-21 output.log: a run scoped to closed issues
        logged ``planned=4`` directly above ``produced 0 new plans``. With
        ``work_units == 0`` the count must read ``planned=0`` so the human
        summary agrees with the convergence signal.
        """
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [PhaseResult("plan", rc=0, work_units=0)]
        summary = _summarize_loop([repo_result], 1, 3.0)
        assert "planned=0" in summary

    def test_multiple_repos_sum_plan_work_units(self) -> None:
        """Plan work_units are summed across repos."""
        repo1 = RepoResult(repo="Repo1", loop_idx=1)
        repo1.phases = [PhaseResult("plan", rc=0, work_units=2)]
        repo2 = RepoResult(repo="Repo2", loop_idx=1)
        repo2.phases = [PhaseResult("plan", rc=0, work_units=0)]
        summary = _summarize_loop([repo1, repo2], 1, 10.0)
        assert "planned=2" in summary

    def test_counts_implement_phases(self) -> None:
        """Non-skipped implement phases counted in implemented."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [
            PhaseResult("implement", rc=0),
        ]
        summary = _summarize_loop([repo_result], 1, 3.0)
        assert "implemented=1" in summary

    def test_mixed_phases(self) -> None:
        """Mix of skipped, planned, and implemented phases."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [
            # No work_units → counted as 1 via the unknown-fallback path.
            PhaseResult("plan", rc=0),
            PhaseResult("implement", rc=1),  # failed but still counted as implemented
            PhaseResult("drive-green", skipped=True, skip_reason="no issues"),
        ]
        summary = _summarize_loop([repo_result], 3, 7.5)
        assert "loop 3" in summary
        assert "planned=1" in summary
        assert "implemented=1" in summary
        assert "skipped=1" in summary

    def test_elapsed_formatting(self) -> None:
        """Elapsed time formatted with no decimal places."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        summary = _summarize_loop([repo_result], 1, 45.6)
        assert "elapsed=46s" in summary

    def test_multiple_repos_aggregated(self) -> None:
        """Results from multiple repos aggregated together."""
        repo1 = RepoResult(repo="Repo1", loop_idx=1)
        repo1.phases = [PhaseResult("plan", rc=0)]
        repo2 = RepoResult(repo="Repo2", loop_idx=1)
        repo2.phases = [
            PhaseResult("plan", skipped=True, skip_reason="no issues"),
            PhaseResult("implement", rc=0),
        ]
        summary = _summarize_loop([repo1, repo2], 1, 10.0)
        assert "planned=1" in summary
        assert "implemented=1" in summary
        assert "skipped=1" in summary


# ---------------------------------------------------------------------------
# Subprocess timeout discipline (#684)
# ---------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess stand-in for mocked subprocess.run calls."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestSubprocessTimeouts:
    """Every unbounded gh/git call in the loop must now pass ``timeout=``."""

    def test_gh_list_repos_passes_timeout(self) -> None:
        """``gh repo list`` is routed through gh_call's bounded adapter."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.return_value = _completed(stdout="[]")
            loop_runner._gh_list_repos("MyOrg")
        assert mock_gh_call.call_args.kwargs.get("timeout") is None

    def test_gh_issue_numbers_passes_timeout(self) -> None:
        """``gh issue list`` is routed through gh_call's bounded adapter."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.return_value = _completed(stdout="1\n2\n")
            loop_runner._list_open_issue_numbers("Org", "Repo")
        assert mock_gh_call.call_args.kwargs.get("timeout") is None

    def test_preflight_token_scopes_passes_timeout(self) -> None:
        """The token preflight ``gh api`` call is routed through gh_call."""
        with patch("hephaestus.automation.loop_runner.gh_call") as mock_gh_call:
            mock_gh_call.return_value = _completed(stdout='{"push": true}')
            _preflight_token_scopes("Org", "Repo")
        assert mock_gh_call.call_args.kwargs.get("timeout") is None

    def test_rate_limit_remaining_passes_timeout(self) -> None:
        """``gh api rate_limit`` is routed through gh_call."""
        payload = '{"resources":{"graphql":{"remaining":5000,"reset":0}}}'
        with patch("hephaestus.automation.loop_runner.gh_call") as mock_gh_call:
            mock_gh_call.return_value = _completed(stdout=payload)
            _rate_limit_remaining()
        assert mock_gh_call.call_args.kwargs.get("timeout") is None

    def test_rebase_main_git_ops_pass_metadata_timeout(self, tmp_path: Path) -> None:
        """The local git ops in _rebase_main carry METADATA_TIMEOUT."""
        with (
            patch("hephaestus.automation.loop_repo_manager.resilient_call") as mock_resilient,
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run,
        ):
            mock_resilient.return_value = _completed()

            def _run(argv: list[str], **_: object):
                if "status" in argv:
                    return _completed(stdout="")
                if "symbolic-ref" in argv:
                    return _completed(stdout="origin/main")
                if "rev-parse" in argv:
                    return _completed(stdout="abc1234")
                return _completed()

            mock_run.side_effect = _run
            _rebase_main("Repo", tmp_path)
        # Every direct subprocess.run (rebase / rev-parse) is bounded.
        assert mock_run.call_count >= 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] == METADATA_TIMEOUT

    def test_gh_list_repos_timeout_raises_systemexit(self) -> None:
        """A timed-out ``gh repo list`` surfaces as a clean SystemExit."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=120)
            with pytest.raises(SystemExit, match="timed out"):
                loop_runner._gh_list_repos("MyOrg")

    def test_gh_issue_numbers_timeout_returns_empty_list(self) -> None:
        """A timed-out issue query degrades to an empty list, not a crash."""
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=120)
            assert loop_runner._list_open_issue_numbers("Org", "Repo") == []


class TestResilientCallAdoption:
    """The hang-prone network calls are bounded."""

    def test_ensure_clone_uses_gh_call_with_network_timeout(self, tmp_path: Path) -> None:
        """``_ensure_clone`` delegates the clone to gh_call."""
        dest = tmp_path / "Repo"
        with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
            mock_gh_call.return_value = _completed(returncode=0)
            _ensure_clone("Org", "Repo", dest)
        assert mock_gh_call.call_count == 1
        assert mock_gh_call.call_args.args[0] == ["repo", "clone", "Org/Repo", str(dest)]
        assert mock_gh_call.call_args.kwargs["timeout"] == NETWORK_TIMEOUT

    def test_rebase_main_fetch_uses_resilient_call(self, tmp_path: Path) -> None:
        """``_rebase_main`` routes the network fetch through resilient_call."""
        with (
            patch("hephaestus.automation.loop_repo_manager.resilient_call") as mock_resilient,
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run,
        ):
            mock_resilient.return_value = _completed()

            def _run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if "symbolic-ref" in argv:
                    return _completed(stdout="origin/main")
                if "rev-list" in argv:
                    return _completed(stdout="0")
                if "rev-parse" in argv:
                    return _completed(stdout="abc1234")
                return _completed()

            mock_run.side_effect = _run
            sha, fetch_ok = _rebase_main("Repo", tmp_path)
        assert sha == "abc1234"
        assert fetch_ok is True
        assert mock_resilient.call_count == 1
        # The wrapped callable is the module's subprocess.run (here the patched mock).
        assert mock_resilient.call_args.args[0] is mock_run
        assert mock_resilient.call_args.kwargs["timeout"] == NETWORK_TIMEOUT
        assert mock_resilient.call_args.kwargs["circuit_breaker_name"] == "git-fetch"

    def test_resilient_call_terminates_hung_clone(self, tmp_path: Path) -> None:
        """A subprocess that never returns is killed: TimeoutExpired propagates.

        ``resilient_call`` does not retry intentional timeouts (it is not in the
        transient set), so the TimeoutExpired surfaces and ``_ensure_clone``
        converts the failed clone into a RuntimeError rather than hanging.
        """
        dest = tmp_path / "Repo"

        def _hang(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="gh repo clone", timeout=120)

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=_hang):
            with pytest.raises(subprocess.TimeoutExpired):
                _ensure_clone("Org", "Repo", dest)

    def test_rebase_main_fetch_timeout_is_non_fatal(self, tmp_path: Path) -> None:
        """A timed-out fetch is logged; the rebase proceeds against stale main.

        The returned ``fetch_ok`` flag is False so the caller can mark the
        log line as stale (#993).
        """

        def _hang(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=120)

        with (
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run,
            patch(
                "hephaestus.automation.loop_repo_manager.resilient_call",
                side_effect=_hang,
            ),
        ):
            mock_run.return_value = _completed(stdout="def5678")
            sha, fetch_ok = _rebase_main("Repo", tmp_path)
        assert sha == "def5678"
        assert fetch_ok is False

    def test_rebase_main_uses_origin_head_default_branch(self, tmp_path: Path) -> None:
        """Repos whose default branch is master must not be rebased against origin/main."""
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "symbolic-ref" in argv:
                return _completed(stdout="origin/master\n")
            if "rev-parse" in argv:
                return _completed(stdout="def5678")
            return _completed()

        _lrm = "hephaestus.automation.loop_repo_manager"
        with (
            patch(f"{_lrm}.resilient_call", return_value=_completed()),
            patch(f"{_lrm}.subprocess.run", side_effect=fake_run),
        ):
            sha, fetch_ok = _rebase_main("Repo", tmp_path)

        assert sha == "def5678"
        assert fetch_ok is True
        assert ["git", "-C", str(tmp_path), "rebase", "origin/master", "--quiet"] in calls
        unexpected_reset = [
            "git",
            "-C",
            str(tmp_path),
            "reset",
            "--hard",
            "origin/main",
            "--quiet",
        ]
        assert unexpected_reset not in calls

    def test_rebase_main_preserves_local_commits_when_rebase_fails(self, tmp_path: Path) -> None:
        """A failed rebase must not hard-reset away local commits ahead of origin."""
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "symbolic-ref" in argv:
                return _completed(stdout="origin/main\n")
            if "rev-list" in argv:
                return _completed(stdout="2\n")
            if "rebase" in argv and "--quiet" in argv:
                return _completed(returncode=1)
            if "rev-parse" in argv:
                return _completed(stdout="local12\n")
            return _completed()

        _lrm = "hephaestus.automation.loop_repo_manager"
        with (
            patch(f"{_lrm}.resilient_call", return_value=_completed()),
            patch(f"{_lrm}.subprocess.run", side_effect=fake_run),
        ):
            sha, fetch_ok = _rebase_main("Repo", tmp_path)

        assert sha == "local12"
        assert fetch_ok is True
        assert ["git", "-C", str(tmp_path), "rebase", "--abort"] in calls
        assert not any(
            call[:5] == ["git", "-C", str(tmp_path), "reset", "--hard"] for call in calls
        )

    def test_rebase_main_fetch_nonzero_rc_marks_stale(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-zero git fetch rc (e.g. macOS sandboxing) must surface and mark stale.

        The returned ``fetch_ok`` flag is False. The SHA value itself stays a
        clean 7-char hash so ``HEPH_TRUNK_GITHASH`` and session naming downstream
        remain unaffected.

        Regression for #993: previously rc=1 silently passed through
        ``resilient_call`` (subprocess.run with ``check=False`` does not raise)
        and the loop logged a trunk SHA as if the refresh had succeeded.
        """
        fetch_failure = _completed(
            returncode=1,
            stderr="error: cannot open .git/FETCH_HEAD: Operation not permitted\n",
        )

        with (
            patch(
                "hephaestus.automation.loop_repo_manager.resilient_call",
                return_value=fetch_failure,
            ),
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run,
            caplog.at_level("WARNING", logger="hephaestus.automation.loop_repo_manager"),
        ):
            mock_run.return_value = _completed(stdout="abc1234")
            sha, fetch_ok = _rebase_main("Repo", tmp_path)

        assert sha == "abc1234"  # clean SHA — no suffix
        assert fetch_ok is False
        assert any(
            "git fetch failed" in rec.message and "rc=1" in rec.message for rec in caplog.records
        ), caplog.records

    def test_rebase_main_fetch_success_returns_clean(self, tmp_path: Path) -> None:
        """A zero-rc fetch returns ``fetch_ok=True`` and the unmodified SHA."""
        with (
            patch(
                "hephaestus.automation.loop_repo_manager.resilient_call",
                return_value=_completed(returncode=0),
            ),
            patch("hephaestus.automation.loop_repo_manager.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="abc1234")
            sha, fetch_ok = _rebase_main("Repo", tmp_path)
        assert sha == "abc1234"
        assert fetch_ok is True


class TestDefaultPhaseTimeout:
    """run_phase must apply a default timeout when --phase-timeout is absent (#684)."""

    def test_default_phase_timeout_is_non_none(self) -> None:
        """A fresh LoopConfig has a positive default phase timeout."""
        cfg = LoopConfig()
        assert cfg.phase_timeout_s is not None
        assert cfg.phase_timeout_s == _default_phase_timeout_s()
        assert cfg.phase_timeout_s > 0

    def test_run_phase_passes_default_timeout_to_subprocess(self, fake_repo_dir: Path) -> None:
        """When no override is given, run_phase forwards the default timeout."""
        cfg = LoopConfig()
        completed = MagicMock(returncode=0)
        with (
            patch.object(loop_runner, "_resolve_phase_bin", return_value=("/bin/true", [])),
            patch("subprocess.run", return_value=completed) as run_mock,
        ):
            run_phase(
                repo="r",
                repo_dir=fake_repo_dir,
                phase="plan",
                cfg=cfg,
                loop_idx=1,
                open_issues=[],
                trunk_sha="abc1234",
            )
        assert run_mock.call_args.kwargs["timeout"] == _default_phase_timeout_s()

    def test_default_phase_timeout_reads_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HEPH_PHASE_TIMEOUT`` overrides the built-in default."""
        monkeypatch.setenv("HEPH_PHASE_TIMEOUT", "42")
        assert _default_phase_timeout_s() == 42.0

    def test_default_phase_timeout_ignores_malformed_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric override falls back to the default instead of crashing."""
        monkeypatch.setenv("HEPH_PHASE_TIMEOUT", "not-a-number")
        assert _default_phase_timeout_s() == 7800.0

    def test_main_applies_default_phase_timeout_when_flag_absent(self) -> None:
        """``main`` builds a LoopConfig with the default timeout when --phase-timeout is omitted."""
        captured: dict[str, LoopConfig] = {}

        def _capture(cfg: LoopConfig, _repos: list[str]) -> list[RepoResult]:
            captured["cfg"] = cfg
            return []

        with (
            patch.object(
                loop_runner,
                "_resolve_org_and_repos",
                return_value=("Org", ["Repo"], None),
            ),
            patch.object(loop_runner, "_preflight_token_scopes"),
            patch.object(loop_runner, "_clone_missing_repos"),
            patch.object(loop_runner, "run_loop", side_effect=_capture),
        ):
            main(["--repos", "Repo", "--dry-run", "--loops", "1", "--agent", "claude"])
        assert captured["cfg"].phase_timeout_s == _default_phase_timeout_s()

    def test_main_resolves_agent_before_building_config(self) -> None:
        """LoopConfig stores the concrete auto-detected provider."""
        captured: dict[str, LoopConfig] = {}

        def _capture(cfg: LoopConfig, _repos: list[str]) -> list[RepoResult]:
            captured["cfg"] = cfg
            return []

        with (
            patch.object(
                loop_runner,
                "_resolve_org_and_repos",
                return_value=("Org", ["Repo"], None),
            ),
            patch.object(loop_runner, "_preflight_token_scopes"),
            patch.object(loop_runner, "_clone_missing_repos"),
            patch.object(loop_runner, "resolve_agent", return_value="codex") as mock_resolve,
            patch.object(loop_runner, "run_loop", side_effect=_capture),
        ):
            main(["--repos", "Repo", "--dry-run", "--loops", "1"])

        mock_resolve.assert_called_once_with(None)
        assert captured["cfg"].agent == "codex"

    def test_main_disables_phase_timeout_when_zero(self) -> None:
        """``--phase-timeout 0`` explicitly disables the bound (None)."""
        captured: dict[str, LoopConfig] = {}

        def _capture(cfg: LoopConfig, _repos: list[str]) -> list[RepoResult]:
            captured["cfg"] = cfg
            return []

        with (
            patch.object(
                loop_runner,
                "_resolve_org_and_repos",
                return_value=("Org", ["Repo"], None),
            ),
            patch.object(loop_runner, "_preflight_token_scopes"),
            patch.object(loop_runner, "_clone_missing_repos"),
            patch.object(loop_runner, "run_loop", side_effect=_capture),
        ):
            main(["--repos", "Repo", "--phase-timeout", "0", "--loops", "1", "--agent", "claude"])
        assert captured["cfg"].phase_timeout_s is None


def test_shutdown_event_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """_request_shutdown sets the Event; _shutdown_requested observes it."""
    import threading

    # Isolate from any prior test that may have set the module-level Event.
    monkeypatch.setattr(loop_runner, "_SHUTDOWN_EVENT", threading.Event())

    assert loop_runner._shutdown_requested() is False
    loop_runner._request_shutdown(signal.SIGINT, None)
    assert loop_runner._shutdown_requested() is True
