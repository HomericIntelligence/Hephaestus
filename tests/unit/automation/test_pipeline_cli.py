"""Test the shared command boundary for queue execution."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType

import pytest

from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.cli.localization import using_localizer


def _cli() -> ModuleType:
    """Load the current queue command module."""
    return importlib.import_module("hephaestus.automation.pipeline_cli")


def test_current_pipeline_help_uses_active_catalog_without_syntax_changes() -> None:
    """Translate authored help and keep parser tokens unchanged."""
    cli = _cli()
    catalog = {
        "Run the queue-owned automation pipeline.": "Exécuter la file automatisée.",
        "Comma-separated repository names.": "Noms de dépôts séparés par des virgules.",
    }

    with using_localizer(catalog):
        parser = cli.build_parser()

    help_text = parser.format_help()
    assert "Exécuter la file automatisée." in help_text
    assert "Noms de dépôts séparés par des virgules." in help_text
    assert "--repos REPOS" in help_text


def test_pipeline_scope_error_uses_stable_human_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate a static scope error without changing its stored English form."""
    cli = _cli()
    args = cli.parse_args(["--repos", "one,two", "--issues", "1", "--dry-run"])
    source = "--issues/--prs require exactly one repository via --repos REPO."
    monkeypatch.setattr(cli, "_detect_cwd_repo", lambda **_kwargs: ("acme", "widget"))

    with using_localizer({source: "--issues/--prs exige un seul dépôt via --repos REPO."}):
        _org, _repos, error = cli._resolve_org_and_repos(args)
        rendered = cli._localize_cli_error(error)

    assert error == source
    assert rendered == "--issues/--prs exige un seul dépôt via --repos REPO."


def test_podman_name_error_uses_stable_human_template() -> None:
    """Translate the authored Podman-name error at the parser boundary."""
    cli = _cli()
    source = "Invalid Podman machine name."

    with using_localizer({source: "Nom de machine Podman non valide."}):
        with pytest.raises(argparse.ArgumentTypeError, match="Nom de machine"):
            cli._parse_podman_machine_name("bad name")


@pytest.mark.parametrize(
    ("profile", "stages"),
    [
        ("full", None),
        ("planning", {StageName.PLANNING, StageName.PLAN_REVIEW}),
        (
            "implementation",
            {StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.MERGE_WAIT},
        ),
        ("review", {StageName.PR_REVIEW}),
    ],
)
def test_command_profiles_use_one_queue_builder(
    tmp_path: Path, profile: str, stages: set[StageName] | None
) -> None:
    """Each command keeps its declared queue scope and merge budget."""
    cli = _cli()
    args = cli.parse_args(
        ["--agent", "claude", "--issues", "7,8", "--projects-dir", str(tmp_path)],
        profile=profile,
    )

    config = cli.build_config(args, "acme", ["widget"])

    assert config.issues == [7, 8]
    assert config.budget_overrides["merge"] == 5
    assert (config.scope.stages if config.scope is not None else None) == stages
    assert config.explicit_pr_review is (profile == "review")


def test_actual_stage_names_set_a_contiguous_scope(tmp_path: Path) -> None:
    """A current stage selection reaches the existing route table."""
    cli = _cli()
    args = cli.parse_args(
        [
            "--agent",
            "claude",
            "--stages",
            "plan_review,implementation,pr_review",
            "--merge-attempts",
            "9",
            "--max-workers",
            "4",
            "--projects-dir",
            str(tmp_path),
        ]
    )

    config = cli.build_config(args, "acme", ["widget"])

    assert config.scope is not None
    assert config.scope.stages == {
        StageName.PLAN_REVIEW,
        StageName.IMPLEMENTATION,
        StageName.PR_REVIEW,
    }
    assert config.budget_overrides["merge"] == 9
    assert config.max_workers == 4


@pytest.mark.parametrize(
    "selection",
    ["", "plan", "planning,pr_review", "pr_review,planning", "planning,planning", "learning"],
)
def test_invalid_stage_scope_is_rejected(selection: str) -> None:
    """Reject empty, retired, duplicate, reversed, and discontinuous stage lists."""
    with pytest.raises(SystemExit) as error:
        _cli().parse_args(["--stages", selection])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--phases", "plan"],
        ["--drive-green-loops", "3"],
        ["--drive-green-all"],
        ["--parallel", "3"],
        ["--epic", "7"],
        ["--analyze"],
        ["--resume"],
        ["--no-auto-merge"],
        ["--no-skip-closed"],
        ["--no-follow-up"],
        ["--no-ui"],
        ["--system-prompt", "old.md"],
        ["--health-check"],
    ],
)
def test_retired_flags_have_no_alias(argv: list[str]) -> None:
    """Removed flags cannot select an old execution path."""
    with pytest.raises(SystemExit) as error:
        _cli().parse_args(argv)
    assert error.value.code == 2


def test_role_and_host_controls_reach_the_queue(tmp_path: Path) -> None:
    """Current role, isolation, learning, and timeout controls remain available."""
    cli = _cli()
    args = cli.parse_args(
        [
            "--agent",
            "codex",
            "--planner-model",
            "sol:high",
            "--reviewer-model",
            "terra:xhigh",
            "--implementer-model",
            "luna:medium",
            "--planner-timeout",
            "11",
            "--reviewer-timeout",
            "12",
            "--implementer-timeout",
            "13",
            "--learning-workers",
            "2",
            "--learning-queue-capacity",
            "3",
            "--codex-isolation-adapter",
            "adapter",
            "--evidence-receipt-dir",
            str(tmp_path / "receipts"),
            "--projects-dir",
            str(tmp_path),
        ],
        profile="implementation",
    )

    config = cli.build_config(args, "acme", ["widget"])

    assert (config.planner_model, config.reviewer_model, config.implementer_model) == (
        "sol:high",
        "terra:xhigh",
        "luna:medium",
    )
    assert (config.planner_timeout, config.reviewer_timeout, config.implementer_timeout) == (
        11,
        12,
        13,
    )
    assert (config.learning_workers, config.learning_queue_capacity) == (2, 3)
    assert config.codex_isolation_adapter == "adapter"
    assert config.evidence_receipt_dir == tmp_path / "receipts"


@pytest.mark.parametrize(
    ("module", "profile"),
    [
        ("loop_runner", "full"),
        ("planner", "planning"),
        ("implementer", "implementation"),
        ("pr_reviewer", "review"),
    ],
)
def test_retained_commands_delegate_without_another_config(
    monkeypatch: pytest.MonkeyPatch, module: str, profile: str
) -> None:
    """Each retained entry point delegates once and preserves the exit code."""
    calls: list[tuple[list[str] | None, str]] = []

    def run(argv: list[str] | None = None, *, profile: str = "full") -> int:
        calls.append((argv, profile))
        return 75

    monkeypatch.setattr(_cli(), "main", run)
    entrypoint = importlib.import_module(f"hephaestus.automation.{module}")

    assert entrypoint.main(["--dry-run"]) == 75
    assert calls == [(["--dry-run"], profile)]


@pytest.mark.parametrize(
    "module",
    [
        "plan_reviewer",
        "audit_reviewer",
        "ci_driver",
        "agent_stage",
        "_implement_phase",
        "_plan_phase",
        "_pr_create_phase",
        "_stage_context",
        "_interfaces",
        "pr_manager",
        "pr_discovery",
        "ci_check_inspector",
        "post_merge_processor",
        "advise_runner",
        "follow_up",
        "issue_dedup",
        "comment_difficulty",
        "pr_review_core",
        "curses_ui",
        "status_tracker",
        "state",
        "planner_state",
        "implementer_state",
        "review_state",
    ],
)
def test_retired_automation_modules_are_absent(module: str) -> None:
    """A retired module cannot expose another execution owner."""
    assert importlib.util.find_spec(f"hephaestus.automation.{module}") is None


def test_retired_console_commands_are_absent() -> None:
    """Only retained commands remain in the installed console surface."""
    root = Path(__file__).resolve().parents[3]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert {
        "hephaestus-audit-prs",
        "hephaestus-drive-prs-green",
        "hephaestus-agent-stage",
        "hephaestus-merge-prs",
    }.isdisjoint(project["project"]["scripts"])


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
@pytest.mark.parametrize("provider", ["claude", "codex", "opencode"])
def test_provider_owned_model_defaults_stay_empty(
    tmp_path: Path, profile: str, provider: str
) -> None:
    """The queue passes empty model choices to the selected provider."""
    cli = _cli()
    args = cli.parse_args(["--agent", provider, "--projects-dir", str(tmp_path)], profile=profile)
    config = cli.build_config(args, "acme", ["widget"])
    assert (
        config.model,
        config.planner_model,
        config.implementer_model,
        config.reviewer_model,
    ) == ("", "", "", "")


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
def test_command_dispatch_preserves_terminal_json(
    profile: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The coordinator emits the only terminal JSON record."""
    from hephaestus.automation.pipeline import coordinator
    from hephaestus.cli.utils import emit_json_status

    cli = _cli()

    def run(config: PipelineConfig) -> int:
        assert config.json_out is True
        emit_json_status(0, review_run_reasons={"explicit-review": 1})
        return 0

    monkeypatch.setattr(cli, "_resolve_org_and_repos", lambda args: ("acme", ["widget"], None))
    monkeypatch.setattr(cli, "_current_checkout_repo_roots", lambda *args: {})
    monkeypatch.setattr(cli, "resolve_agent", lambda agent, **kwargs: agent or "claude")
    monkeypatch.setattr(cli, "event_log_lifecycle", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(coordinator, "run_pipeline", run)

    assert cli.main(["--agent", "claude", "--dry-run", "--json"], profile=profile) == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"status": "ok", "exit_code": 0, "review_run_reasons": {"explicit-review": 1}}
    ]


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
def test_command_dispatch_reports_interrupt(
    profile: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An interrupted queue exits with one JSON error record."""
    from hephaestus.automation.pipeline import coordinator

    cli = _cli()

    def run(config: PipelineConfig) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_resolve_org_and_repos", lambda args: ("acme", ["widget"], None))
    monkeypatch.setattr(cli, "_current_checkout_repo_roots", lambda *args: {})
    monkeypatch.setattr(cli, "resolve_agent", lambda agent, **kwargs: agent or "claude")
    monkeypatch.setattr(cli, "event_log_lifecycle", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(coordinator, "run_pipeline", run)

    assert cli.main(["--agent", "claude", "--dry-run", "--json"], profile=profile) == 130
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1
    assert records[0]["exit_code"] == 130
    assert records[0]["message"] == "interrupted"


def _patch_pipeline_main_prerequisites(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate queue command startup from repository and agent discovery."""
    monkeypatch.setattr(cli, "_resolve_org_and_repos", lambda args: ("acme", ["widget"], None))
    monkeypatch.setattr(cli, "_current_checkout_repo_roots", lambda *args: {})
    monkeypatch.setattr(cli, "resolve_agent", lambda agent, **kwargs: agent or "claude")
    monkeypatch.setattr(cli, "event_log_lifecycle", lambda *args, **kwargs: nullcontext())


def test_json_reports_logging_setup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Emit one JSON error when command logging cannot start."""
    cli = _cli()
    monkeypatch.setattr(
        cli,
        "configure_cli_logging",
        lambda **kwargs: (_ for _ in ()).throw(OSError("read-only directory")),
    )

    source = "Cannot open log file %(path)r: %(error)s. Check the parent directory and permissions."
    with using_localizer({source: "Impossible d'ouvrir %(path)r : %(error)s."}):
        assert cli.main(["--json", "--dry-run"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["message"].startswith("Cannot open log file")


def test_json_reports_token_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Emit one JSON error when the GitHub token preflight fails."""
    cli = _cli()
    _patch_pipeline_main_prerequisites(cli, monkeypatch)
    monkeypatch.setattr(
        cli,
        "gh_call",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cli.subprocess.CalledProcessError(1, ["gh"], stderr="denied")
        ),
    )

    source = (
        "ERROR: `gh` cannot read %(org)s/%(repo)s with the current token.\n"
        "  %(detail)s\n"
        "  Required scopes: repo (classic) OR Issues+PRs+Contents Read & Write "
        "(fine-grained).\n"
        "  Check with: gh auth status"
    )
    with using_localizer({source: "Échec GitHub pour %(org)s/%(repo)s : %(detail)s"}):
        assert cli.main(["--json", "--agent", "claude"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert "current token" in payload["message"]


def test_json_reports_prompt_catalog_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Emit one JSON error when the packaged prompt catalog is unavailable."""
    from hephaestus.automation.pipeline import coordinator_types

    cli = _cli()
    _patch_pipeline_main_prerequisites(cli, monkeypatch)
    monkeypatch.setattr(
        coordinator_types.PromptCatalog,
        "current",
        lambda: (_ for _ in ()).throw(OSError("catalog unavailable")),
    )

    source = "ERROR: Prompt templates missing or unreadable — reinstall: `uv sync`."
    with using_localizer({source: "Modèles indisponibles."}):
        assert cli.main(["--json", "--agent", "claude", "--dry-run"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["message"].startswith("ERROR: Prompt templates missing or unreadable")
