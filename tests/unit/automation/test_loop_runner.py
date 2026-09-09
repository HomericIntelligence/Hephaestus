"""Test queue CLI scope, admission, configuration, and dispatch."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from hephaestus.automation import pipeline_cli as loop_runner
from hephaestus.automation.loop_repo_manager import _detect_cwd_repo
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline_cli import _preflight_token_scopes, main

# ---------------------------------------------------------------------------
# Phase topology
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI / config validation
# ---------------------------------------------------------------------------


def test_parse_args_agent_defaults_to_auto_detect() -> None:
    """Omitted --agent should defer to runtime auto-detection."""
    args = loop_runner.parse_args([])
    assert args.agent is None


def test_parse_args_accepts_explicit_codex_agent() -> None:
    """Operators can still force Codex explicitly."""
    args = loop_runner.parse_args(["--agent", "codex"])
    assert args.agent == "codex"


def test_plan_review_reset_requires_and_accepts_explicit_issues() -> None:
    """A reset can never broaden from an explicit issue scope to discovery."""
    with pytest.raises(SystemExit):
        loop_runner.parse_args(["--reset-plan-review-session"])
    args = loop_runner.parse_args(["--issues", "8,13", "--reset-plan-review-session"])
    assert args.issues == [8, 13]
    assert args.reset_plan_review_session is True


def test_parse_args_defaults_event_log_retention() -> None:
    """Event-log retention defaults are conservative and explicit."""
    args = loop_runner.parse_args([])
    assert args.event_log_retention_days == 30
    assert args.event_log_retention_count == 100


def test_parse_args_accepts_zero_event_log_retention_limits() -> None:
    """Operators can independently disable either retention dimension."""
    args = loop_runner.parse_args(
        ["--event-log-retention-days", "0", "--event-log-retention-count", "0"]
    )
    assert args.event_log_retention_days == 0
    assert args.event_log_retention_count == 0


@pytest.mark.parametrize("flag", ["--event-log-retention-days", "--event-log-retention-count"])
def test_parse_args_rejects_negative_event_log_retention_limits(flag: str) -> None:
    """Retention limits reject negative values at the CLI boundary."""
    with pytest.raises(SystemExit):
        loop_runner.parse_args([flag, "-1"])


def test_main_wires_private_evidence_receipt_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The normal loop can opt into bounded worker receipts for an evidence run."""
    config = cast(
        PipelineConfig,
        _capture_config(["--agent", "codex", "--evidence-receipt-dir", str(tmp_path)], monkeypatch),
    )

    assert config.evidence_receipt_dir == tmp_path


def test_loop_help_documents_explicit_gh_root_override() -> None:
    """The executable exception is an explicit, discoverable CLI authority."""
    assert "--gh-extra-path-root" in loop_runner.build_parser().format_help()


def test_parse_args_rejects_gh_root_that_escapes_through_a_symlink(tmp_path: Path) -> None:
    """The explicit root cannot authorize an executable outside its boundary."""
    gh_root = tmp_path / "gh-root"
    gh_root.mkdir()
    outside = tmp_path / "outside-gh"
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    (gh_root / "bin").mkdir()
    (gh_root / "bin" / "gh").symlink_to(outside)

    with pytest.raises(SystemExit) as excinfo:
        loop_runner.parse_args(["--gh-extra-path-root", str(gh_root)])

    assert excinfo.value.code == 2


def test_parse_args_accepts_no_advise() -> None:
    """The loop runner can disable advise across child phases."""
    args = loop_runner.parse_args(["--no-advise"])
    assert args.no_advise is True


def test_parse_args_accepts_run_pre_pr_tests() -> None:
    """The queue runner can enable the implementation-stage pre-PR test gate."""
    args = loop_runner.parse_args(["--run-pre-pr-tests"])
    assert args.run_pre_pr_tests is True
    assert loop_runner.parse_args([]).run_pre_pr_tests is False


def test_parse_args_keeps_pre_pr_timeout_unset_until_an_operator_overrides_it() -> None:
    """The stage selects its repository-specific timeout when the flag is absent."""
    assert loop_runner.parse_args([]).pre_pr_test_timeout is None
    assert loop_runner.parse_args(["--pre-pr-test-timeout", "9000"]).pre_pr_test_timeout == 9000


def test_parse_args_accepts_nitpick() -> None:
    """The loop runner can enable nitpick comments across review phases."""
    assert loop_runner.parse_args(["--nitpick"]).nitpick is True
    assert loop_runner.parse_args([]).nitpick is False


def test_parse_args_accepts_github_throttle_options() -> None:
    """The loop runner accepts explicit child-phase GitHub throttle config."""
    args = loop_runner.parse_args(["--gh-global-rate", "4.5", "--gh-global-burst", "11"])
    assert args.gh_global_rate == 4.5
    assert args.gh_global_burst == 11.0


def test_parse_args_accepts_merge_attempts() -> None:
    """--merge-attempts is parsed; default is 5 (#2246, was --max-merge-attempts #1560)."""
    assert loop_runner.parse_args(["--merge-attempts", "3"]).merge_attempts == 3
    assert loop_runner.parse_args([]).merge_attempts == 5


def test_parse_args_accepts_review_iterations() -> None:
    """The review budget is explicit and does not reuse the reseed counter."""
    assert loop_runner.parse_args(["--review-iterations", "10"]).review_iterations == 10
    assert loop_runner.parse_args([]).review_iterations is None


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_parse_args_rejects_non_positive_review_iterations(bad: str) -> None:
    """An explicit review budget must allow at least one review round."""
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.parse_args(["--review-iterations", bad])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_parse_args_rejects_non_positive_merge_attempts(bad: str) -> None:
    """The merge-poll budget must leave at least one current-run poll available."""
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.parse_args(["--merge-attempts", bad])
    assert excinfo.value.code == 2


def test_parse_args_accepts_issue_scope() -> None:
    """The loop runner can scope child phases to a comma-separated issue list."""
    args = loop_runner.parse_args(["--issues", "8, 13"])
    assert args.issues == [8, 13]


def test_parse_args_accepts_pr_scope() -> None:
    """The loop runner can scope pipeline seeding to a comma-separated PR list."""
    args = loop_runner.parse_args(["--prs", "77, 78"])
    assert args.prs == [77, 78]


@pytest.mark.parametrize("bad", ["0", "-1", "33", "100"])
def test_parse_args_rejects_out_of_range_max_workers(bad: str) -> None:
    """Regression for #723: loop_runner must reject --max-workers outside 1-32."""
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.parse_args(["--max-workers", bad])
    assert excinfo.value.code == 2


def test_parse_args_accepts_valid_max_workers() -> None:
    """Valid --max-workers in range 1-32 accepted."""
    args = loop_runner.parse_args(["--max-workers", "8"])
    assert args.max_workers == 8


def test_parse_args_default_max_workers_is_six() -> None:
    """Omitted --max-workers defaults to 6 for the queue-based loop."""
    args = loop_runner.parse_args([])
    assert args.max_workers == 6


def test_parse_args_learning_lane_defaults_and_overrides() -> None:
    """Learning has one worker and one queue slot unless set explicitly."""
    defaults = loop_runner.parse_args([])
    assert defaults.learning_workers == 1
    assert defaults.learning_queue_capacity == 1
    assert defaults.no_learn is False

    configured = loop_runner.parse_args(
        ["--learning-workers", "2", "--learning-queue-capacity", "3", "--no-learn"]
    )
    assert configured.learning_workers == 2
    assert configured.learning_queue_capacity == 3
    assert configured.no_learn is True


def test_parse_args_serialize_file_overlap_default_on() -> None:
    """File-overlap serialization is on by default; the flag disables it (#1623)."""
    assert loop_runner.parse_args([]).serialize_file_overlap is True
    assert loop_runner.parse_args(["--no-serialize-file-overlap"]).serialize_file_overlap is False


def test_parse_args_model_flag_wires_to_namespace() -> None:
    """--model parses into args.model (the path main() reads into cfg.model)."""
    args = loop_runner.parse_args(["--model", "claude-fable-5"])
    assert args.model == "claude-fable-5"
    # Default is empty so the catch-all is inert unless explicitly passed.
    assert loop_runner.parse_args([]).model == ""


@pytest.mark.parametrize(
    "flag",
    [
        "--planner-reasoning-effort",
        "--reviewer-reasoning-effort",
        "--implementer-reasoning-effort",
    ],
)
def test_parse_args_rejects_removed_reasoning_effort_flags(flag: str) -> None:
    """Model references are the only CLI reasoning-effort input."""
    with pytest.raises(SystemExit):
        loop_runner.parse_args([flag, "high"])


def test_parse_args_keeps_a_free_form_effort_in_the_model_reference() -> None:
    """The parser does not validate a provider effort value."""
    args = loop_runner.parse_args(["--reviewer-model", "gpt-6-astra:future-effort"])

    assert args.reviewer_model == "gpt-6-astra:future-effort"


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
        loop_runner.parse_args(["--repos", "foo", "bar"])


def test_resolve_org_and_repos_cwd_default() -> None:
    """No flags + cwd is a github repo → run for that single repo."""
    args = loop_runner.parse_args([])
    with patch(
        "hephaestus.automation.pipeline_cli._detect_cwd_repo",
        return_value=("MyOrg", "MyRepo"),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "MyOrg"
    assert repos == ["MyRepo"]


def test_resolve_org_and_repos_errors_when_no_scope_and_not_git() -> None:
    """No flags + cwd is not a github repo → return error message."""
    args = loop_runner.parse_args([])
    with patch(
        "hephaestus.automation.pipeline_cli._detect_cwd_repo",
        return_value=(None, None),
    ):
        _, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is not None
    assert "cwd is not a github.com repo" in err
    assert repos == []


def test_resolve_org_and_repos_org_no_arg_autodetects() -> None:
    """``--org`` with no value resolves a streamed organization source."""
    args = loop_runner.parse_args(["--org"])
    assert args.org is loop_runner._ORG_AUTODETECT
    with (
        patch(
            "hephaestus.automation.pipeline_cli._detect_cwd_repo",
            return_value=("DetectedOrg", "AnyRepo"),
        ),
        patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list,
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "DetectedOrg"
    assert repos == []
    mock_list.assert_not_called()


def test_resolve_org_and_repos_org_no_arg_errors_when_not_git() -> None:
    """``--org`` with no value + cwd not a github repo → error."""
    args = loop_runner.parse_args(["--org"])
    with patch(
        "hephaestus.automation.pipeline_cli._detect_cwd_repo",
        return_value=(None, None),
    ):
        _, _, err = loop_runner._resolve_org_and_repos(args)
    assert err is not None
    assert "--org with no argument" in err


def test_resolve_org_and_repos_org_named() -> None:
    """``--org NAME`` streams the named org without cwd detection."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg"])
    with (
        patch(
            "hephaestus.automation.pipeline_cli._detect_cwd_repo",
        ) as mock_detect,
        patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list,
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "ExplicitOrg"
    assert repos == []
    mock_detect.assert_not_called()
    mock_list.assert_not_called()


def test_resolve_org_and_repos_org_defers_discovery_without_issue_scan() -> None:
    """--org does not enumerate repos before the bounded coordinator source."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg"])
    with (
        patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list,
        patch(
            "hephaestus.automation.loop_repo_manager._iter_open_issue_meta",
            side_effect=AssertionError("--org must not enumerate each repo's issue metadata"),
        ) as mock_issues,
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)

    assert err is None
    assert org == "ExplicitOrg"
    assert repos == []
    mock_list.assert_not_called()
    mock_issues.assert_not_called()


@pytest.mark.parametrize("scope", [("--issues", "42"), ("--prs", "42")])
def test_resolve_org_and_repos_rejects_numeric_scope_without_concrete_repo(
    scope: tuple[str, str],
) -> None:
    """Org-wide direct numeric scopes must not materialize or pick a repo."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg", *scope])
    with patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list:
        org, repos, err = loop_runner._resolve_org_and_repos(args)

    assert org == "ExplicitOrg"
    assert repos == []
    assert err == "--org with --issues/--prs requires exactly one --repos REPO scope."
    mock_list.assert_not_called()


@pytest.mark.parametrize("scope", [("--issues", "42"), ("--prs", "42")])
def test_resolve_org_and_repos_rejects_multi_repo_numeric_scope(
    scope: tuple[str, str],
) -> None:
    """A direct numeric target has one unambiguous repository owner."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg", "--repos", "a,b", *scope])

    org, repos, err = loop_runner._resolve_org_and_repos(args)

    assert org == ""
    assert repos == []
    assert err == "--issues/--prs require exactly one repository via --repos REPO."


@pytest.mark.parametrize("scope", [("--issues", "42"), ("--prs", "42")])
def test_resolve_org_and_repos_accepts_single_repo_numeric_scope(
    scope: tuple[str, str],
) -> None:
    """One explicit repository preserves the direct numeric-scope contract."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg", "--repos", "target", *scope])

    org, repos, err = loop_runner._resolve_org_and_repos(args)

    assert (org, repos, err) == ("ExplicitOrg", ["target"], None)


def test_resolve_org_and_repos_dry_run_keeps_discovery_read_only() -> None:
    """Organization discovery stays read-only when the loop is a dry run."""
    args = loop_runner.parse_args(["--org", "ExplicitOrg", "--dry-run"])
    epic = {"number": 81, "title": "Epic: roadmap", "labels": ["epic"]}
    with (
        patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list,
        patch(
            "hephaestus.automation.loop_repo_manager._iter_open_issue_meta",
            return_value=[epic],
        ),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)

    assert err is None
    assert org == "ExplicitOrg"
    assert repos == []
    mock_list.assert_not_called()


def test_resolve_org_and_repos_repos_flag_uses_cwd_org() -> None:
    """``--repos foo,bar`` uses cwd-detected org without enumerating."""
    args = loop_runner.parse_args(["--repos", "foo,bar"])
    assert args.repos == ["foo", "bar"]
    with (
        patch(
            "hephaestus.automation.pipeline_cli._detect_cwd_repo",
            return_value=("CwdOrg", "Whatever"),
        ),
        patch("hephaestus.automation.pipeline_cli._iter_gh_repos") as mock_list,
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "CwdOrg"
    assert repos == ["foo", "bar"]
    mock_list.assert_not_called()


def test_resolve_org_and_repos_repos_flag_falls_back_to_explicit_org() -> None:
    """``--repos foo --org Bar`` (not in a git repo) uses ``Bar`` as the org."""
    args = loop_runner.parse_args(["--repos", "foo", "--org", "Bar"])
    with patch(
        "hephaestus.automation.pipeline_cli._detect_cwd_repo",
        return_value=(None, None),
    ):
        org, repos, err = loop_runner._resolve_org_and_repos(args)
    assert err is None
    assert org == "Bar"
    assert repos == ["foo"]


def test_resolve_org_and_repos_repos_flag_prefers_explicit_org() -> None:
    """``--repos foo --org Bar`` should not be overridden by the cwd repo's org."""
    args = loop_runner.parse_args(["--repos", "foo", "--org", "Bar"])
    with patch(
        "hephaestus.automation.pipeline_cli._detect_cwd_repo",
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
        org, repo = _detect_cwd_repo()
    assert org == "MyOrg"
    assert repo == "MyRepo"


def test_resolve_org_and_repos_cwd_default_uses_remote_repo_not_worktree_dir() -> None:
    """No flags should scope the loop to the GitHub repo, not worktree basename."""
    args = loop_runner.parse_args([])

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
    with patch("hephaestus.automation.pipeline_cli.gh_call") as mock_gh_call:
        mock_gh_call.return_value = _completed(stdout='{"push": true}')
        _preflight_token_scopes("Org", "Repo")
    assert mock_gh_call.called


def test_preflight_token_scopes_timeout_raises_systemexit() -> None:
    """A timed-out token preflight surfaces as a clean SystemExit."""
    with patch("hephaestus.automation.pipeline_cli.gh_call") as mock_gh_call:
        mock_gh_call.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        with pytest.raises(SystemExit, match="timed out"):
            _preflight_token_scopes("Org", "Repo")


def test_preflight_token_scopes_404_explains_repository_access() -> None:
    """An HTTP 404 identifies repository name and access as possible causes."""
    error = subprocess.CalledProcessError(
        1,
        ["gh", "api"],
        stderr="gh: Not Found (HTTP 404)",
    )
    with patch("hephaestus.automation.pipeline_cli.gh_call", side_effect=error):
        with pytest.raises(SystemExit) as exc_info:
            _preflight_token_scopes("Org", "Repo")

    message = str(exc_info.value)
    assert "GitHub returned HTTP 404 for Org/Repo" in message
    assert "repository name is correct" in message
    assert "current account has access" in message
    assert "gh repo view Org/Repo" in message
    assert "gh auth status" in message
    assert "Required scopes:" not in message
    assert "gh: Not Found (HTTP 404)" in message


def test_preflight_token_scopes_warns_on_empty_permissions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty permissions log a warning that writes will fail."""
    with (
        patch("hephaestus.automation.pipeline_cli.gh_call") as mock_gh_call,
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
        """The parser sets a positive default job timeout."""
        cfg = loop_runner.parse_args([])
        assert cfg.phase_timeout is not None
        assert cfg.phase_timeout == 7800.0
        assert cfg.phase_timeout > 0

    def test_default_phase_timeout_ignores_removed_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removed environment flag does not change the default."""
        monkeypatch.setenv("HEPH_PHASE_TIMEOUT", "42")
        assert loop_runner.parse_args([]).phase_timeout == 7800.0

    def test_default_phase_timeout_ignores_malformed_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unused environment value does not change the default."""
        monkeypatch.setenv("HEPH_PHASE_TIMEOUT", "not-a-number")
        assert loop_runner.parse_args([]).phase_timeout == 7800.0


# ---------------------------------------------------------------------------
# main() → pipeline config wiring
# ---------------------------------------------------------------------------


def _capture_config(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, *, profile: str = "full"
) -> object:
    """Run main() with dispatch stubbed and return the captured PipelineConfig."""
    from hephaestus.automation.pipeline import coordinator as coordinator_mod

    captured: dict[str, object] = {}

    def _capture(config: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(loop_runner, "_resolve_org_and_repos", lambda args: ("Org", ["Repo"], None))
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", lambda *a, **k: None)
    monkeypatch.setattr(loop_runner, "event_log_lifecycle", lambda *a, **k: nullcontext())
    monkeypatch.setattr(coordinator_mod, "run_pipeline", _capture)
    main(argv, profile=profile)
    return captured["config"]


def _capture_main_config(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> object:
    """Run main with only its coordinator dispatch replaced."""
    from hephaestus.automation.pipeline import coordinator as coordinator_mod

    captured: dict[str, object] = {}

    def capture(config: object) -> int:
        captured["config"] = config
        return 0

    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop_runner, "event_log_lifecycle", lambda *a, **k: nullcontext())
    monkeypatch.setattr(coordinator_mod, "run_pipeline", capture)

    assert main(argv) == 0
    return captured["config"]


def test_main_applies_default_phase_timeout_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() builds a PipelineConfig with the default timeout when omitted."""
    config = _capture_config(
        ["--repos", "Repo", "--dry-run", "--loops", "1", "--agent", "claude"], monkeypatch
    )
    assert config.phase_timeout_s == 7800.0  # type: ignore[attr-defined]


def test_main_disables_phase_timeout_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--phase-timeout 0`` explicitly disables the bound (None)."""
    config = _capture_config(
        ["--repos", "Repo", "--phase-timeout", "0", "--loops", "1", "--agent", "claude"],
        monkeypatch,
    )
    assert config.phase_timeout_s is None  # type: ignore[attr-defined]


def test_main_passes_org_discovery_as_a_resettable_pipeline_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An org-wide run never materializes repository names in PipelineConfig."""
    config = _capture_main_config(
        ["--org", "Org", "--dry-run", "--loops", "1", "--agent", "codex"], monkeypatch
    )

    assert config.repos == []  # type: ignore[attr-defined]
    assert callable(config.repo_source_factory)  # type: ignore[attr-defined]


@pytest.mark.parametrize("port", [0, 65535])
def test_metrics_port_parser_accepts_tcp_port_range(port: int) -> None:
    """The opt-in local metrics endpoint accepts every valid TCP port."""
    assert loop_runner.parse_args(["--metrics-port", str(port)]).metrics_port == port


@pytest.mark.parametrize("port", [-1, 65536])
def test_metrics_port_parser_rejects_out_of_range_values(port: int) -> None:
    """Bad port values fail during CLI parsing, before coordinator setup."""
    with pytest.raises(SystemExit):
        loop_runner.parse_args(["--metrics-port", str(port)])


def test_main_wires_metrics_port_to_pipeline_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validated CLI port reaches the coordinator's opt-in configuration."""
    config = _capture_config(
        ["--repos", "Repo", "--metrics-port", "9123", "--loops", "1", "--agent", "claude"],
        monkeypatch,
    )
    assert config.metrics_port == 9123  # type: ignore[attr-defined]


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


def test_main_wires_external_checkout_as_explicit_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external noncanonical checkout is not rewritten as ``projects_dir / repo``.

    The loop may be launched from an isolated worktree whose path bears no
    relationship to its remote repository name.  Its matching repo therefore
    needs an explicit coordinator path, while other requested repos retain the
    normal projects-root discovery fallback.
    """
    checkout = tmp_path / "isolated" / "automation-worktree"
    projects_dir = tmp_path / "configured-projects"

    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **_kwargs: ("Org", "Repo"))
    monkeypatch.setattr(loop_runner, "get_repo_root", lambda: checkout)
    with patch.object(loop_runner, "resolve_projects_dir", return_value=projects_dir):
        config = _capture_main_config(
            ["--repos", "Repo,Other", "--dry-run", "--loops", "1", "--agent", "claude"],
            monkeypatch,
        )

    assert config.projects_dir == projects_dir  # type: ignore[attr-defined]
    assert config.repo_roots == {"Repo": checkout}  # type: ignore[attr-defined]
    assert "Other" not in config.repo_roots  # type: ignore[attr-defined]


def test_main_does_not_override_explicit_projects_dir_with_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``--projects-dir`` remains authoritative for every repo."""
    checkout = tmp_path / "isolated" / "automation-worktree"
    projects_dir = tmp_path / "configured-projects"

    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **_kwargs: ("Org", "Repo"))
    monkeypatch.setattr(loop_runner, "get_repo_root", lambda: checkout)
    config = _capture_main_config(
        [
            "--repos",
            "Repo,Other",
            "--projects-dir",
            str(projects_dir),
            "--dry-run",
            "--loops",
            "1",
            "--agent",
            "claude",
        ],
        monkeypatch,
    )

    assert config.projects_dir == projects_dir  # type: ignore[attr-defined]
    assert config.repo_roots == {}  # type: ignore[attr-defined]


def test_main_ignores_removed_projects_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poisoned removed projects root cannot override typed path resolution."""
    checkout = tmp_path / "isolated" / "automation-worktree"
    projects_dir = tmp_path / "configured-projects"
    projects_dir.mkdir()

    monkeypatch.setenv("PROJECTS_ROOT", str(projects_dir))
    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **_kwargs: ("Org", "Repo"))
    monkeypatch.setattr(loop_runner, "get_repo_root", lambda: checkout)
    with patch.object(loop_runner, "resolve_projects_dir", return_value=checkout.parent):
        config = _capture_main_config(
            ["--repos", "Repo,Other", "--dry-run", "--loops", "1", "--agent", "claude"],
            monkeypatch,
        )

    assert config.projects_dir == checkout.parent  # type: ignore[attr-defined]
    assert config.repo_roots == {"Repo": checkout}  # type: ignore[attr-defined]


def test_main_does_not_override_projects_root_for_automation_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Automation worktrees continue using their base checkout through projects_dir."""
    projects_dir = tmp_path / "projects"
    base_checkout = projects_dir / "Repo"
    automation_worktree = base_checkout / "build" / ".worktrees" / "issue-99"

    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **_kwargs: ("Org", "Repo"))
    monkeypatch.setattr(loop_runner, "get_repo_root", lambda: automation_worktree)
    with patch.object(loop_runner, "resolve_projects_dir", return_value=projects_dir):
        config = _capture_main_config(
            ["--repos", "Repo,Other", "--dry-run", "--loops", "1", "--agent", "claude"],
            monkeypatch,
        )

    assert config.projects_dir == projects_dir  # type: ignore[attr-defined]
    assert config.repo_roots == {}  # type: ignore[attr-defined]


def test_main_uses_noncanonical_automation_worktree_base_as_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal worktree under a renamed checkout must not become its own base.

    ``resolve_projects_dir`` correctly unwraps an automation issue worktree
    to the parent of its base checkout.  When that base checkout itself has a
    noncanonical name, the loop must still use the base checkout, rather than
    creating a nested ``build/.worktrees`` tree under the issue checkout.
    """
    projects_dir = tmp_path / "projects"
    base_checkout = projects_dir / "renamed-base-checkout"
    automation_worktree = base_checkout / "build" / ".worktrees" / "issue-99"

    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **_kwargs: ("Org", "Repo"))
    monkeypatch.setattr(loop_runner, "get_repo_root", lambda: automation_worktree)
    with patch.object(loop_runner, "resolve_projects_dir", return_value=projects_dir):
        config = _capture_main_config(
            ["--repos", "Repo,Other", "--dry-run", "--loops", "1", "--agent", "claude"],
            monkeypatch,
        )

    assert config.projects_dir == projects_dir  # type: ignore[attr-defined]
    assert config.repo_roots == {"Repo": base_checkout}  # type: ignore[attr-defined]
    assert "Other" not in config.repo_roots  # type: ignore[attr-defined]


def test_main_resolves_agent_before_building_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """PipelineConfig stores the concrete auto-detected provider."""
    resolve = patch.object(loop_runner, "resolve_agent", return_value="codex")
    with resolve as mock_resolve:
        config = _capture_config(["--repos", "Repo", "--dry-run", "--loops", "1"], monkeypatch)
    mock_resolve.assert_called_once_with(
        None,
        disable_pi_automation=False,
        auth_status_timeout=10,
        pi_isolation_adapter=None,
        pi_dir=None,
        model_references=("",),
    )
    assert config.agent == "codex"  # type: ignore[attr-defined]


def test_main_threads_codex_isolation_inputs_into_pipeline_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop keeps all explicit Codex isolation inputs typed."""
    lock_path = (tmp_path / "deployment-lock.json").absolute()
    digest = "a" * 64

    config = _capture_config(
        [
            "--repos",
            "Repo",
            "--dry-run",
            "--loops",
            "1",
            "--agent",
            "codex",
            "--codex-isolation-adapter",
            "production",
            "--codex-isolation-deployment-lock",
            str(lock_path),
            "--codex-isolation-deployment-lock-sha256",
            digest,
        ],
        monkeypatch,
    )

    assert config.codex_isolation_adapter == "production"  # type: ignore[attr-defined]
    assert config.codex_isolation_deployment_lock == lock_path  # type: ignore[attr-defined]
    assert config.codex_isolation_deployment_lock_sha256 == digest  # type: ignore[attr-defined]


def test_main_reports_invalid_model_before_scope_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop reports invalid input before repository discovery or dispatch."""

    def reject_invalid_fallback(agent: str | None, **kwargs: object) -> str:
        assert agent == "codex"
        assert kwargs["model_references"] == ("", "unknown")
        raise ValueError("Invalid model selection")

    monkeypatch.setattr(
        loop_runner,
        "resolve_agent",
        reject_invalid_fallback,
    )
    resolve_scope = patch.object(loop_runner, "_resolve_org_and_repos")
    run_pipeline = patch("hephaestus.automation.pipeline.coordinator.run_pipeline")
    with resolve_scope as mock_resolve_scope, run_pipeline as mock_run_pipeline:
        with pytest.raises(SystemExit) as error:
            main(["--agent", "codex", "--fallback-model", "unknown"])

    assert error.value.code == 2
    mock_resolve_scope.assert_not_called()
    mock_run_pipeline.assert_not_called()


def test_main_passes_inline_role_effort_before_pi_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi admission validates each model reference with its inline effort."""
    resolve = patch.object(loop_runner, "resolve_agent", return_value="pi")
    with resolve as mock_resolve:
        _capture_config(
            [
                "--repos",
                "Repo",
                "--dry-run",
                "--loops",
                "1",
                "--agent",
                "pi",
                "--model",
                "private/custom-model",
                "--planner-model",
                "private/custom-model:default",
                "--reviewer-model",
                "private/custom-model:high",
            ],
            monkeypatch,
        )

    references = {call.kwargs["model_references"] for call in mock_resolve.call_args_list}
    assert references == {
        ("private/custom-model:default",),
        ("private/custom-model:high",),
        ("private/custom-model",),
    }


def test_main_errors_on_empty_repo_list() -> None:
    """An empty resolved repo list is a clean exit-1, not a pipeline dispatch."""
    # resolve_agent() probes PATH for a real backend; mock it so the test does
    # not depend on `claude`/`codex`/`pi` being installed (release runners have
    # no agent backend).
    with (
        patch.object(loop_runner, "resolve_agent", return_value="claude"),
        patch.object(loop_runner, "_resolve_org_and_repos", return_value=("Org", [], None)),
    ):
        rc = main([])
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


@pytest.mark.parametrize(
    ("phase", "unused_role", "expected_roles"),
    [
        ("planning,plan_review", "implementer", {"planner", "reviewer"}),
        ("implementation,pr_review,merge_wait", "planner", {"implementer", "reviewer"}),
        ("pr_review,merge_wait", "planner", {"reviewer"}),
        ("pr_review,merge_wait", "implementer", {"reviewer"}),
    ],
)
def test_scoped_loop_admits_only_tools_used_by_selected_phases(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    unused_role: str,
    expected_roles: set[str],
) -> None:
    """An excluded Pi role cannot block a scope that uses other tools."""
    seen: set[str] = set()

    def resolve(agent: str | None, **kwargs: object) -> str:
        if agent == "pi":
            raise ValueError("Pi admission is unavailable")
        reference = cast(tuple[str, ...], kwargs["model_references"])[0]
        seen.add(reference)
        return agent or "codex"

    monkeypatch.setattr(loop_runner, "resolve_agent", resolve)
    config = _capture_config(
        [
            "--stages",
            phase,
            "--agent",
            "codex",
            f"--{unused_role}-agent",
            "pi",
            "--planner-model",
            "planner",
            "--implementer-model",
            "implementer",
            "--reviewer-model",
            "reviewer",
        ],
        monkeypatch,
    )
    assert config is not None
    assert seen == expected_roles


def test_review_profile_does_not_resolve_unused_implementer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unused Pi implementer cannot block a Claude review command."""
    resolved: list[str | None] = []

    def resolve(agent: str | None, **kwargs: object) -> str:
        resolved.append(agent)
        if agent == "pi":
            raise ValueError("Pi admission is unavailable")
        return agent or "claude"

    monkeypatch.setattr(loop_runner, "resolve_agent", resolve)
    config = _capture_config(
        ["--agent", "claude", "--implementer-agent", "pi"],
        monkeypatch,
        profile="review",
    )

    assert config is not None
    assert resolved == ["claude"]


def test_review_scope_requires_reviewer_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR review must validate the selected reviewer before queue dispatch."""

    def resolve(agent: str | None, **kwargs: object) -> str:
        if agent == "pi":
            raise ValueError("Pi admission is unavailable")
        return agent or "codex"

    monkeypatch.setattr(loop_runner, "resolve_agent", resolve)
    with pytest.raises(SystemExit) as error:
        _capture_config(
            ["--stages", "pr_review,merge_wait", "--agent", "codex", "--reviewer-agent", "pi"],
            monkeypatch,
        )
    assert error.value.code == 2
