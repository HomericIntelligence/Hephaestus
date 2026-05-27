"""Tests for hephaestus.automation.loop_runner.

Focus: phase-isolation invariants. The whole reason for this module
existing is that the previous bash version silently aborted between
phases — these tests pin down that a Python phase failure (whether
subprocess rc!=0, raised exception, or worker crash) does NOT prevent
subsequent phases from being attempted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    ALL_PHASES,
    LoopConfig,
    PhaseResult,
    RepoResult,
    _phase_order_warnings,
    _validate_phases,
    process_repo,
    run_loop,
    run_phase,
)

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


def test_phase_order_warnings_implement_without_review_plans() -> None:
    """Phase order warnings implement without review plans."""
    cfg = LoopConfig(phases=("implement",))
    warnings = _phase_order_warnings(cfg)
    assert any("implement" in w and "review-plans" in w for w in warnings)


def test_phase_order_warnings_address_review_without_review_prs() -> None:
    """Phase order warnings address review without review prs."""
    cfg = LoopConfig(phases=("address-review",))
    warnings = _phase_order_warnings(cfg)
    assert any("address-review" in w and "review-prs" in w for w in warnings)


def test_phase_order_warnings_drive_green_without_implement() -> None:
    """Phase order warnings drive green without implement."""
    cfg = LoopConfig(phases=("drive-green",))
    warnings = _phase_order_warnings(cfg)
    assert any("drive-green" in w for w in warnings)


def test_phase_order_warnings_silent_on_full_pipeline() -> None:
    """Phase order warnings silent on full pipeline."""
    cfg = LoopConfig(phases=ALL_PHASES)
    assert _phase_order_warnings(cfg) == []


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
    for name in ("review-plans", "review-prs", "address-review", "drive-green"):
        assert by_name[name].skipped
        assert by_name[name].skip_reason == "no open issues"
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


def test_build_phase_argv_review_plans_includes_issues() -> None:
    """Build phase argv review plans includes issues."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["script.py"])):
        argv = loop_runner._build_phase_argv("review-plans", cfg, open_issues=[7, 8])
    assert argv is not None
    assert "--issues" in argv
    assert "7" in argv and "8" in argv


def test_build_phase_argv_passes_dry_run() -> None:
    """Build phase argv passes dry run."""
    cfg = LoopConfig(dry_run=True)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None and "--dry-run" in argv


def test_build_phase_argv_implement_has_single_max_workers() -> None:
    """Regression: implement must not duplicate --max-workers."""
    cfg = LoopConfig(max_workers=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/impl", [])):
        argv = loop_runner._build_phase_argv("implement", cfg, open_issues=[])
    assert argv is not None
    assert argv.count("--max-workers") == 1


def test_build_phase_argv_review_plans_omits_no_ui() -> None:
    """Regression: review-plans does NOT receive --no-ui (bash never passed it)."""
    cfg = LoopConfig()
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/py", ["s.py"])):
        argv = loop_runner._build_phase_argv("review-plans", cfg, open_issues=[1])
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


def test_build_phase_argv_plan_omits_max_workers() -> None:
    """Plan phase does not accept --max-workers (auto-discovers internally)."""
    cfg = LoopConfig(max_workers=4)
    with patch.object(loop_runner, "_resolve_phase_bin", return_value=("/x/plan", [])):
        argv = loop_runner._build_phase_argv("plan", cfg, open_issues=[])
    assert argv is not None and "--max-workers" not in argv


def test_phase_env_loop_index_only_for_drive_green() -> None:
    """Regression: HEPH_LOOP_INDEX/HEPH_TOTAL_LOOPS scoped to drive-green only."""
    cfg = LoopConfig(loops=5)
    for phase in ("plan", "review-plans", "implement", "review-prs", "address-review"):
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
