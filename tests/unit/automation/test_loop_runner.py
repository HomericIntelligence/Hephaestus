"""Tests for hephaestus.automation.loop_runner.

Focus: phase-isolation invariants. The whole reason for this module
existing is that the previous bash version silently aborted between
phases — these tests pin down that a Python phase failure (whether
subprocess rc!=0, raised exception, or worker crash) does NOT prevent
subsequent phases from being attempted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.claude_timeouts import gh_cli_timeout
from hephaestus.automation.loop_runner import (
    ALL_PHASES,
    LoopConfig,
    PhaseResult,
    RepoResult,
    _default_phase_timeout_s,
    _ensure_clone,
    _gh_issue_numbers_for,
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
from hephaestus.constants import scripts_dir as _scripts_dir
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

# ---------------------------------------------------------------------------
# Phase topology — the 6→3 stage collapse (#455/#468/#484)
# ---------------------------------------------------------------------------


def test_all_phases_is_three_stage_pipeline() -> None:
    """The pipeline collapsed to exactly (plan, implement, drive-green).

    Plan-review, PR-review, and address-review are no longer standalone phases;
    they fold into plan/implement. This pins the canonical topology so a stray
    re-introduction of a retired phase fails loudly.
    """
    assert ALL_PHASES == ("plan", "implement", "drive-green")


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


def test_phase_order_warnings_drive_green_without_implement_warns() -> None:
    """drive-green selected without implement triggers the cross-stage warning."""
    cfg = LoopConfig(phases=("drive-green",))
    warnings = _phase_order_warnings(cfg)
    assert any("drive-green" in w and "implement" in w for w in warnings)


def test_phase_order_warnings_plan_without_implement_warns() -> None:
    """Plan selected without implement makes the invocation planning-only."""
    cfg = LoopConfig(phases=("plan",))
    warnings = _phase_order_warnings(cfg)
    assert any("planning-only" in w and "implementation PRs" in w for w in warnings)


def test_phase_order_warnings_drive_green_with_implement_silent() -> None:
    """drive-green selected alongside implement produces no warning."""
    cfg = LoopConfig(phases=("implement", "drive-green"))
    assert _phase_order_warnings(cfg) == []


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


def test_parse_args_accepts_issue_scope() -> None:
    """The loop runner can scope child phases to a comma-separated issue list."""
    args = loop_runner._parse_args(["--issues", "8, 13"])
    assert args.issues == [8, 13]


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


def test_process_repo_runs_all_phases_when_all_succeed(
    repo_inputs: tuple[Path, LoopConfig],
) -> None:
    """Process repo runs all phases when all succeed."""
    _, cfg = repo_inputs
    with (
        patch.object(loop_runner, "_rebase_main", return_value="abc1234"),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1, 2]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)
    assert [p.name for p in result.phases] == list(ALL_PHASES)
    assert not result.any_failure


def test_process_repo_continues_after_phase_failure(repo_inputs: tuple[Path, LoopConfig]) -> None:
    """The core regression test: phase 1 failing must NOT stop phases 2-6."""
    _, cfg = repo_inputs
    call_order: list[str] = []

    def fake_run_phase(**kw: object) -> PhaseResult:
        name = str(kw["phase"])
        call_order.append(name)
        rc = 1 if name == "plan" else 0
        return PhaseResult(name=name, rc=rc, elapsed_s=0.1)

    with (
        patch.object(loop_runner, "_rebase_main", return_value="abc1234"),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "run_phase", side_effect=fake_run_phase),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)

    assert call_order == list(ALL_PHASES), "phase 1 failure must not skip subsequent phases"
    assert result.phases[0].failed
    assert not result.phases[1].failed
    assert result.any_failure


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
        patch.object(loop_runner, "_rebase_main", return_value="abc1234"),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)
    by_name = {p.name: p for p in result.phases}
    assert not by_name["plan"].skipped
    for name in ALL_PHASES[1:]:
        assert by_name[name].skipped
        assert by_name[name].skip_reason == "disabled by --phases"


def test_process_repo_skips_issue_phases_when_no_issues(
    repo_inputs: tuple[Path, LoopConfig],
) -> None:
    """Process repo skips issue phases when no issues."""
    _, cfg = repo_inputs
    with (
        patch.object(loop_runner, "_rebase_main", return_value="abc1234"),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result = process_repo("r", loop_idx=1, cfg=cfg)
    by_name = {p.name: p for p in result.phases}
    # Only drive-green requires open issues (PHASES_REQUIRING_ISSUES). With
    # loops=1 it also runs on the final loop, so the skip reason here is the
    # "no open issues" gate rather than "not final loop".
    assert by_name["drive-green"].skipped
    assert by_name["drive-green"].skip_reason == "no open issues"
    # plan and implement auto-discover, so they still run
    assert not by_name["plan"].skipped
    assert not by_name["implement"].skipped


def test_process_repo_skips_drive_green_on_non_final_loop(
    repo_inputs: tuple[Path, LoopConfig],
) -> None:
    """Process repo skips drive green on non final loop."""
    _, cfg = repo_inputs
    cfg = LoopConfig(loops=3, projects_dir=cfg.projects_dir)
    with (
        patch.object(loop_runner, "_rebase_main", return_value="abc1234"),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1]),
        patch.object(loop_runner, "run_phase", side_effect=lambda **kw: _ok(kw["phase"])),
    ):
        result_loop1 = process_repo("r", loop_idx=1, cfg=cfg)
        result_loop3 = process_repo("r", loop_idx=3, cfg=cfg)
    drive_green_loop1 = next(p for p in result_loop1.phases if p.name == "drive-green")
    drive_green_loop3 = next(p for p in result_loop3.phases if p.name == "drive-green")
    assert drive_green_loop1.skipped and drive_green_loop1.skip_reason == "not final loop"
    assert not drive_green_loop3.skipped


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


def test_build_phase_argv_plan_omits_issues() -> None:
    """Build phase argv plan omits issues."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1, 2])
    assert argv is not None
    assert "--issues" not in argv


def test_build_phase_argv_plan_forwards_explicit_issues() -> None:
    """Plan receives --issues only when the loop is explicitly scoped."""
    cfg = LoopConfig(issues=[8, 13])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[1, 2])
    assert argv is not None
    assert argv[argv.index("--issues") + 1 : argv.index("--issues") + 3] == ["8", "13"]


def test_build_phase_argv_implement_forwards_explicit_issues() -> None:
    """Implement receives the loop issue scope when set."""
    cfg = LoopConfig(issues=[8])
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[1, 2])
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
    """drive-green forwards the open-issue list via --issues (the only phase that does)."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("drive-green", cfg, open_issues=[7, 8])
    assert argv is not None
    assert "--issues" in argv
    assert "7" in argv and "8" in argv


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


def test_build_phase_argv_plan_uses_parallel_worker_flag() -> None:
    """Plan phase receives worker count via its --parallel flag."""
    cfg = LoopConfig(max_workers=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None
    assert "--max-workers" not in argv
    assert argv[argv.index("--parallel") + 1] == "4"


def test_phase_env_loop_index_only_for_drive_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: HEPH_LOOP_INDEX/HEPH_TOTAL_LOOPS scoped to drive-green only."""
    # Clean pre-existing vars from the actual environment that _phase_env will copy
    monkeypatch.delenv("HEPH_LOOP_INDEX", raising=False)
    monkeypatch.delenv("HEPH_TOTAL_LOOPS", raising=False)
    cfg = LoopConfig(loops=5)
    for phase in ("plan", "implement"):
        env = loop_runner._phase_env(cfg, loop_idx=3, trunk_sha="abc", phase=phase)
        assert "HEPH_LOOP_INDEX" not in env, f"phase {phase} leaked HEPH_LOOP_INDEX"
        assert "HEPH_TOTAL_LOOPS" not in env, f"phase {phase} leaked HEPH_TOTAL_LOOPS"
    env_dg = loop_runner._phase_env(cfg, loop_idx=3, trunk_sha="abc", phase="drive-green")
    assert env_dg["HEPH_LOOP_INDEX"] == "3"
    assert env_dg["HEPH_TOTAL_LOOPS"] == "5"


def test_phase_env_model_vars_only_when_non_empty() -> None:
    """Model env vars only present when their cfg field is non-empty."""
    cfg_empty = LoopConfig()
    env = loop_runner._phase_env(cfg_empty, loop_idx=1, trunk_sha="abc", phase="plan")
    assert "HEPH_PLANNER_MODEL" not in env
    assert "HEPH_REVIEWER_MODEL" not in env
    assert "HEPH_IMPLEMENTER_MODEL" not in env

    cfg_set = LoopConfig(planner_model="opus", reviewer_model="sonnet", implementer_model="opus")
    env_set = loop_runner._phase_env(cfg_set, loop_idx=1, trunk_sha="abc", phase="plan")
    assert env_set["HEPH_PLANNER_MODEL"] == "opus"
    assert env_set["HEPH_REVIEWER_MODEL"] == "sonnet"
    assert env_set["HEPH_IMPLEMENTER_MODEL"] == "opus"


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
    """``isFork: true`` and ``isArchived: true`` entries are excluded."""
    payload = (
        '[{"name":"keep","isFork":false,"isArchived":false},'
        '{"name":"drop-fork","isFork":true,"isArchived":false},'
        '{"name":"drop-archived","isFork":false,"isArchived":true},'
        '{"name":"Odysseus","isFork":false,"isArchived":false}]'
    )
    with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        names = loop_runner._gh_list_repos("MyOrg")
    assert names == ["keep"]
    invoked_argv = mock_run.call_args[0][0]
    assert "--no-archived" in invoked_argv
    assert "name,isArchived,isFork" in invoked_argv


def test_list_open_issue_numbers_unions_author_and_assignee() -> None:
    """Author + assignee queries are unioned (OR semantics) and sorted."""
    calls = {"author": "10\n12\n", "assignee": "12\n7\n"}

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--author" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=calls["author"], stderr=""
            )
        if "--assignee" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=calls["assignee"], stderr=""
            )
        raise AssertionError(f"unexpected argv: {argv!r}")

    with patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=fake_run):
        nums = loop_runner._list_open_issue_numbers("MyOrg", "MyRepo")
    assert nums == [7, 10, 12]


def test_list_open_issue_numbers_passes_at_me() -> None:
    """``@me`` is passed for both ``--author`` and ``--assignee``."""
    seen_filters: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        for flag in ("--author", "--assignee"):
            if flag in argv:
                idx = argv.index(flag)
                seen_filters.append(f"{flag}={argv[idx + 1]}")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=fake_run):
        loop_runner._list_open_issue_numbers("Org", "Repo")
    assert "--author=@me" in seen_filters
    assert "--assignee=@me" in seen_filters


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

    with patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=fake_run):
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

    with patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=fake_run):
        org, repo = loop_runner._detect_cwd_repo()
    assert org == "HOrg"
    assert repo == "R"


def test_detect_cwd_repo_returns_none_when_not_git() -> None:
    """``git rev-parse`` failure → ``(None, None)``."""
    with patch(
        "hephaestus.automation.loop_runner.subprocess.run",
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
        """Non-skipped plan phase counted in planned."""
        repo_result = RepoResult(repo="TestRepo", loop_idx=1)
        repo_result.phases = [PhaseResult("plan", rc=0)]
        summary = _summarize_loop([repo_result], 1, 3.0)
        assert "planned=1" in summary

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


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess stand-in for mocked subprocess.run calls."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestSubprocessTimeouts:
    """Every unbounded gh/git call in the loop must now pass ``timeout=``."""

    def test_gh_list_repos_passes_timeout(self) -> None:
        """``gh repo list`` is a network op bounded by gh_cli_timeout()."""
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="[]")
            loop_runner._gh_list_repos("MyOrg")
        assert mock_run.call_args.kwargs["timeout"] == gh_cli_timeout()

    def test_gh_issue_numbers_passes_timeout(self) -> None:
        """``gh issue list`` is bounded by gh_cli_timeout()."""
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="1\n2\n")
            _gh_issue_numbers_for("Org", "Repo", "--author")
        assert mock_run.call_args.kwargs["timeout"] == gh_cli_timeout()

    def test_preflight_token_scopes_passes_timeout(self) -> None:
        """The token preflight ``gh api`` call is bounded by gh_cli_timeout()."""
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout='{"push": true}')
            _preflight_token_scopes("Org", "Repo")
        assert mock_run.call_args.kwargs["timeout"] == gh_cli_timeout()

    def test_rate_limit_remaining_passes_timeout(self) -> None:
        """``gh api rate_limit`` is bounded by gh_cli_timeout()."""
        payload = '{"resources":{"graphql":{"remaining":5000,"reset":0}}}'
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=payload)
            _rate_limit_remaining()
        assert mock_run.call_args.kwargs["timeout"] == gh_cli_timeout()

    def test_rebase_main_git_ops_pass_metadata_timeout(self, tmp_path: Path) -> None:
        """The local git ops in _rebase_main carry METADATA_TIMEOUT."""
        with (
            patch("hephaestus.automation.loop_runner.resilient_call") as mock_resilient,
            patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run,
        ):
            mock_resilient.return_value = _completed()
            mock_run.return_value = _completed(stdout="abc1234")
            _rebase_main("Repo", tmp_path)
        # Every direct subprocess.run (rebase / rev-parse) is bounded.
        assert mock_run.call_count >= 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] == METADATA_TIMEOUT

    def test_gh_list_repos_timeout_raises_systemexit(self) -> None:
        """A timed-out ``gh repo list`` surfaces as a clean SystemExit."""
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=120)
            with pytest.raises(SystemExit, match="timed out"):
                loop_runner._gh_list_repos("MyOrg")

    def test_gh_issue_numbers_timeout_returns_empty_set(self) -> None:
        """A timed-out issue query degrades to an empty set, not a crash."""
        with patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=120)
            assert _gh_issue_numbers_for("Org", "Repo", "--author") == set()


class TestResilientCallAdoption:
    """The hang-prone clone/fetch calls route through resilient_call (#684)."""

    def test_ensure_clone_uses_resilient_call_with_network_timeout(self, tmp_path: Path) -> None:
        """``_ensure_clone`` delegates the clone to resilient_call."""
        dest = tmp_path / "Repo"
        with patch("hephaestus.automation.loop_runner.resilient_call") as mock_resilient:
            mock_resilient.return_value = _completed(returncode=0)
            _ensure_clone("Org", "Repo", dest)
        assert mock_resilient.call_count == 1
        # The wrapped callable is subprocess.run; the clone is NETWORK_TIMEOUT-bounded.
        assert mock_resilient.call_args.args[0] is subprocess.run
        assert mock_resilient.call_args.kwargs["timeout"] == NETWORK_TIMEOUT
        assert mock_resilient.call_args.kwargs["circuit_breaker_name"] == "gh-repo-clone"

    def test_rebase_main_fetch_uses_resilient_call(self, tmp_path: Path) -> None:
        """``_rebase_main`` routes the network fetch through resilient_call."""
        with (
            patch("hephaestus.automation.loop_runner.resilient_call") as mock_resilient,
            patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run,
        ):
            mock_resilient.return_value = _completed()
            mock_run.return_value = _completed(stdout="abc1234")
            _rebase_main("Repo", tmp_path)
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

        with patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=_hang):
            with pytest.raises(subprocess.TimeoutExpired):
                _ensure_clone("Org", "Repo", dest)

    def test_rebase_main_fetch_timeout_is_non_fatal(self, tmp_path: Path) -> None:
        """A timed-out fetch is logged and the rebase proceeds against stale main."""

        def _hang(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=120)

        with (
            patch("hephaestus.automation.loop_runner.subprocess.run") as mock_run,
            patch(
                "hephaestus.automation.loop_runner.resilient_call",
                side_effect=_hang,
            ),
        ):
            mock_run.return_value = _completed(stdout="def5678")
            sha = _rebase_main("Repo", tmp_path)
        assert sha == "def5678"

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

        with (
            patch("hephaestus.automation.loop_runner.resilient_call", return_value=_completed()),
            patch("hephaestus.automation.loop_runner.subprocess.run", side_effect=fake_run),
        ):
            sha = _rebase_main("Repo", tmp_path)

        assert sha == "def5678"
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
        assert _default_phase_timeout_s() == 3600.0

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
