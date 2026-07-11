"""Tests for hephaestus.automation.loop_runner.

loop_runner is a thin wrapper over the queue-based pipeline (epic #1809): it
owns CLI parsing, org/repo scope resolution, PipelineConfig construction, and
the token-preflight + dispatch hand-off. The legacy subprocess-per-phase loop
was removed in #1819; execution lives in ``hephaestus.automation.pipeline``.
These tests pin the parser, phase validation, scope resolution, and dispatch
seams that remain here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    ALL_PHASES,
    LoopConfig,
    _default_phase_timeout_s,
    _phase_order_warnings,
    _preflight_token_scopes,
    _validate_phases,
    main,
)
from hephaestus.utils.helpers import NETWORK_TIMEOUT

# ---------------------------------------------------------------------------
# Phase topology
# ---------------------------------------------------------------------------


def test_all_phases_is_two_stage_loop_body() -> None:
    """Default loop-body phases stay plan+implement; drive-green is separate.

    Plan-review, PR-review, and address-review fold into plan/implement
    (#455/#468/#484). drive-green is the terminal blocking stage (#1560).
    """
    from hephaestus.automation.loop_runner import ALL_POST_LOOP_STAGES, ALL_SELECTABLE

    assert ALL_PHASES == ("plan", "implement")
    assert ALL_POST_LOOP_STAGES == ("drive-green",)
    assert ALL_SELECTABLE == ("plan", "implement", "drive-green")


@pytest.mark.parametrize("dropped", ["review-plans", "review-prs", "address-review"])
def test_dropped_phases_rejected_by_validation(dropped: str) -> None:
    """``--phases`` must reject a retired phase name as unknown."""
    with pytest.raises(SystemExit, match="Unknown phase"):
        _validate_phases(dropped)


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


def test_phase_order_warnings_plan_without_implement_is_queue_safe() -> None:
    """Partial phase selection is a queue entry hint, not an unsafe order."""
    cfg = LoopConfig(phases=("plan",))
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


def test_parse_args_accepts_run_pre_pr_tests() -> None:
    """The queue runner can enable the implementation-stage pre-PR test gate."""
    args = loop_runner._parse_args(["--run-pre-pr-tests"])
    assert args.run_pre_pr_tests is True
    assert loop_runner._parse_args([]).run_pre_pr_tests is False


def test_parse_args_accepts_nitpick() -> None:
    """The loop runner can enable nitpick comments across review phases."""
    assert loop_runner._parse_args(["--nitpick"]).nitpick is True
    assert loop_runner._parse_args([]).nitpick is False


def test_parse_args_accepts_github_throttle_options() -> None:
    """The loop runner accepts explicit child-phase GitHub throttle config."""
    args = loop_runner._parse_args(["--gh-global-rate", "4.5", "--gh-global-burst", "11"])
    assert args.gh_global_rate == 4.5
    assert args.gh_global_burst == 11.0


def test_parse_args_accepts_max_merge_attempts() -> None:
    """--max-merge-attempts is parsed; default is 1 (#1560)."""
    assert loop_runner._parse_args(["--max-merge-attempts", "3"]).max_merge_attempts == 3
    assert loop_runner._parse_args([]).max_merge_attempts == 1


def test_parse_args_accepts_issue_scope() -> None:
    """The loop runner can scope child phases to a comma-separated issue list."""
    args = loop_runner._parse_args(["--issues", "8, 13"])
    assert args.issues == [8, 13]


def test_parse_args_accepts_pr_scope() -> None:
    """The loop runner can scope pipeline seeding to a comma-separated PR list."""
    args = loop_runner._parse_args(["--prs", "77, 78"])
    assert args.prs == [77, 78]


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


def test_parse_args_default_max_workers_is_six() -> None:
    """Omitted --max-workers defaults to 6 for the queue-based loop."""
    args = loop_runner._parse_args([])
    assert args.max_workers == 6


def test_parse_args_serialize_file_overlap_default_on() -> None:
    """File-overlap serialization is on by default; the flag disables it (#1623)."""
    assert loop_runner._parse_args([]).serialize_file_overlap is True
    assert loop_runner._parse_args(["--no-serialize-file-overlap"]).serialize_file_overlap is False


def test_parse_args_model_flag_wires_to_namespace() -> None:
    """--model parses into args.model (the path main() reads into cfg.model)."""
    args = loop_runner._parse_args(["--model", "claude-fable-5"])
    assert args.model == "claude-fable-5"
    # Default is empty so the catch-all is inert unless explicitly passed.
    assert loop_runner._parse_args([]).model == ""


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
    with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
        mock_gh_call.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        names = loop_runner._gh_list_repos("MyOrg")
    # Odysseus is included — no name-based filtering (issue #814).
    assert sorted(names) == ["Odysseus", "keep"]
    invoked_argv = mock_gh_call.call_args[0][0]
    assert "--no-archived" in invoked_argv
    assert "name,isArchived,isFork" in invoked_argv


def test_gh_list_repos_passes_network_timeout() -> None:
    """``gh repo list`` is routed through gh_call's bounded adapter."""
    with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
        mock_gh_call.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        loop_runner._gh_list_repos("MyOrg")
    assert mock_gh_call.call_args.kwargs["timeout"] == NETWORK_TIMEOUT


def test_gh_list_repos_timeout_raises_systemexit() -> None:
    """A timed-out ``gh repo list`` surfaces as a clean SystemExit."""
    with patch("hephaestus.automation.loop_repo_manager.gh_call") as mock_gh_call:
        mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=120)
        with pytest.raises(SystemExit, match="timed out"):
            loop_runner._gh_list_repos("MyOrg")


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


def test_resolve_org_and_repos_cwd_default_uses_remote_repo_not_worktree_dir() -> None:
    """No flags should scope the loop to the GitHub repo, not worktree basename."""
    args = loop_runner._parse_args([])

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="/tmp/Hephaestus/build/.worktrees/issue-1442\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="git@github.com:HomericIntelligence/Hephaestus.git\n",
            stderr="",
        )

    with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "HomericIntelligence"
    assert repos == ["Hephaestus"]


# ---------------------------------------------------------------------------
# Token preflight
# ---------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess stand-in for mocked subprocess.run calls."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_preflight_token_scopes_reads_permissions() -> None:
    """The token preflight ``gh api`` call is routed through gh_call."""
    with patch("hephaestus.automation.loop_runner.gh_call") as mock_gh_call:
        mock_gh_call.return_value = _completed(stdout='{"push": true}')
        _preflight_token_scopes("Org", "Repo")
    assert mock_gh_call.called


def test_preflight_token_scopes_timeout_raises_systemexit() -> None:
    """A timed-out token preflight surfaces as a clean SystemExit."""
    with patch("hephaestus.automation.loop_runner.gh_call") as mock_gh_call:
        mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        with pytest.raises(SystemExit, match="timed out"):
            _preflight_token_scopes("Org", "Repo")


def test_preflight_token_scopes_warns_on_empty_permissions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty permissions log a warning that writes will fail."""
    with (
        patch("hephaestus.automation.loop_runner.gh_call") as mock_gh_call,
        caplog.at_level("WARNING", logger="hephaestus.automation.loop_runner"),
    ):
        mock_gh_call.return_value = _completed(stdout="null")
        _preflight_token_scopes("Org", "Repo")
    assert any("Token permissions" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Default per-agent-job timeout (#684)
# ---------------------------------------------------------------------------


class TestDefaultPhaseTimeout:
    """The default job timeout applies when --phase-timeout is absent (#684)."""

    def test_default_phase_timeout_is_non_none(self) -> None:
        """A fresh LoopConfig has a positive default phase timeout."""
        cfg = LoopConfig()
        assert cfg.phase_timeout_s is not None
        assert cfg.phase_timeout_s == _default_phase_timeout_s()
        assert cfg.phase_timeout_s > 0

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


# ---------------------------------------------------------------------------
# main() → pipeline config wiring
# ---------------------------------------------------------------------------


def _capture_config(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> object:
    """Run main() with dispatch stubbed and return the captured PipelineConfig."""
    from hephaestus.automation.pipeline import coordinator as coordinator_mod

    captured: dict[str, object] = {}

    def _capture(config: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(loop_runner, "_resolve_org_and_repos", lambda args: ("Org", ["Repo"], None))
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", lambda *a, **k: None)
    monkeypatch.setattr(coordinator_mod, "run_pipeline", _capture)
    main(argv)
    return captured["config"]


def test_main_applies_default_phase_timeout_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() builds a PipelineConfig with the default timeout when omitted."""
    config = _capture_config(
        ["--repos", "Repo", "--dry-run", "--loops", "1", "--agent", "claude"], monkeypatch
    )
    assert config.phase_timeout_s == _default_phase_timeout_s()  # type: ignore[attr-defined]


def test_main_disables_phase_timeout_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--phase-timeout 0`` explicitly disables the bound (None)."""
    config = _capture_config(
        ["--repos", "Repo", "--phase-timeout", "0", "--loops", "1", "--agent", "claude"],
        monkeypatch,
    )
    assert config.phase_timeout_s is None  # type: ignore[attr-defined]


def test_main_installs_sigtstp_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() fixes Ctrl+Z (#1784) via the shared install_sigtstp_only helper.

    The pipeline's Coordinator already installs its own cooperative
    SIGINT/SIGTERM/SIGHUP handlers on entry, but never wired SIGTSTP — this
    wrapper installs it independently before dispatching to the coordinator.
    """
    from hephaestus.automation.pipeline import coordinator as coordinator_mod

    monkeypatch.setattr(loop_runner, "_resolve_org_and_repos", lambda args: ("Org", ["Repo"], None))
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", lambda *a, **k: None)
    monkeypatch.setattr(coordinator_mod, "run_pipeline", lambda config: 0)

    with patch("hephaestus.utils.terminal.install_sigtstp_only") as mock_tstp:
        rc = main(["--repos", "Repo", "--dry-run", "--loops", "1", "--agent", "claude"])

    assert rc == 0
    mock_tstp.assert_called_once_with()


def test_main_prefers_current_checkout_parent_for_projects_dir_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop defaults should use the cwd checkout's projects root when available."""
    projects_dir = tmp_path / "projects"
    resolve = patch.object(loop_runner, "resolve_projects_dir", return_value=projects_dir)
    with resolve as resolve_projects_dir:
        config = _capture_config(
            ["--repos", "Repo", "--dry-run", "--loops", "1", "--agent", "claude"], monkeypatch
        )
    resolve_projects_dir.assert_called_once_with(None, prefer_cwd_parent=True)
    assert config.projects_dir == projects_dir  # type: ignore[attr-defined]


def test_main_resolves_agent_before_building_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """PipelineConfig stores the concrete auto-detected provider."""
    resolve = patch.object(loop_runner, "resolve_agent", return_value="codex")
    with resolve as mock_resolve:
        config = _capture_config(["--repos", "Repo", "--dry-run", "--loops", "1"], monkeypatch)
    mock_resolve.assert_called_once_with(None)
    assert config.agent == "codex"  # type: ignore[attr-defined]


def test_main_errors_on_empty_repo_list() -> None:
    """An empty resolved repo list is a clean exit-1, not a pipeline dispatch."""
    with patch.object(loop_runner, "_resolve_org_and_repos", return_value=("Org", [], None)):
        rc = main(["--org", "Org"])
    assert rc == 1


def test_legacy_loop_symbols_removed() -> None:
    """The legacy per-phase subprocess machinery was removed with #1819."""
    assert not hasattr(loop_runner, "_resolve_phase_bin")
    assert not hasattr(loop_runner, "_build_phase_argv")
    assert not hasattr(loop_runner, "run_phase")
    assert not hasattr(loop_runner, "process_repo")
    assert not hasattr(loop_runner, "run_loop")
    assert not hasattr(loop_runner, "_run_post_loop_stages")
    assert not hasattr(loop_runner, "_PHASE_FLAGS")


def test_main_wires_run_pre_pr_tests_to_pipeline_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--run-pre-pr-tests`` reaches the queue implementation stage config."""
    config = _capture_config(
        ["--repos", "Repo", "--run-pre-pr-tests", "--loops", "1", "--agent", "claude"],
        monkeypatch,
    )

    assert config.run_pre_pr_tests is True  # type: ignore[attr-defined]
