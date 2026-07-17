"""Pipeline dispatch tests for loop_runner.main.

The queue-based pipeline is the only automation-loop path (epic #1809, cutover
#1818, legacy-path removal #1819). ``loop_runner.main`` parses the CLI, builds a
``PipelineConfig``, runs a repo-token preflight, and hands off to
``run_pipeline``. The repo stage owns cloning, so ``main`` does not clone
(C3: no double-clone).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.loop_runner as loop_runner
import hephaestus.automation.pipeline.coordinator as coordinator_mod
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch the pipeline dispatch target and the pre-dispatch collaborators."""
    mocks = {
        "run_pipeline": MagicMock(return_value=0),
        "preflight": MagicMock(),
        "clone": MagicMock(),
    }
    monkeypatch.setattr(coordinator_mod, "run_pipeline", mocks["run_pipeline"])
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", mocks["preflight"])
    monkeypatch.setattr(loop_runner, "_clone_missing_repos", mocks["clone"])
    monkeypatch.setattr(
        loop_runner, "_resolve_org_and_repos", lambda args: ("org", ["repo-a"], None)
    )
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent: "claude")
    return mocks


def test_main_dispatches_to_pipeline(dispatch: dict[str, MagicMock]) -> None:
    """main() always runs the queue-based pipeline."""
    exit_code = loop_runner.main([])

    assert exit_code == 0
    dispatch["run_pipeline"].assert_called_once()


def test_pipeline_path_preflights_but_skips_clone(dispatch: dict[str, MagicMock]) -> None:
    """C3: pipeline keeps token preflight, while the repo stage owns cloning."""
    loop_runner.main([])

    dispatch["run_pipeline"].assert_called_once()
    dispatch["preflight"].assert_called_once_with("org", "repo-a")
    dispatch["clone"].assert_not_called()


def test_pipeline_exit_code_propagates(dispatch: dict[str, MagicMock]) -> None:
    """run_pipeline's exit code IS main's exit code."""
    dispatch["run_pipeline"].return_value = 130

    assert loop_runner.main([]) == 130


def test_dry_run_skips_preflight(dispatch: dict[str, MagicMock]) -> None:
    """A dry run must not hit the live gh token preflight."""
    loop_runner.main(["--dry-run"])

    dispatch["run_pipeline"].assert_called_once()
    dispatch["preflight"].assert_not_called()


def test_build_pipeline_config_maps_cli_fields(dispatch: dict[str, MagicMock]) -> None:
    """_build_pipeline_config carries the CLI scope into PipelineConfig."""
    loop_runner.main(
        [
            "--loops",
            "3",
            "--max-workers",
            "4",
            "--parallel-repos",
            "2",
            "--dry-run",
            "--issues",
            "11,12",
            "--prs",
            "21,22",
            "--no-advise",
            "--no-serialize-file-overlap",
            "--nitpick",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.org == "org"
    assert config.repos == ["repo-a"]
    assert config.issues == [11, 12]
    assert config.prs == [21, 22]
    assert config.loops == 3
    assert config.max_workers == 4
    assert config.parallel_repos == 2
    assert config.dry_run is True
    assert config.no_advise is True
    assert config.serialize_file_overlap is False
    assert config.nitpick is True
    assert config.scope is None
    assert config.event_log_path is not None
    assert config.event_log_path.name.startswith("pipeline-events-")
    assert config.event_log_path.parent == Path(DEFAULT_STATE_DIR)


def test_drive_green_all_maps_all_authors_and_bots(dispatch: dict[str, MagicMock]) -> None:
    """The legacy drive-green-all flag keeps its broad discovery scope."""
    loop_runner.main(["--drive-green-all", "--dry-run"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.drive_green_all is True
    assert config.include_all_authors is True
    assert config.include_bot_prs is True


def test_default_pipeline_event_log_path_does_not_create_repo_checkout() -> None:
    """The default event log path must not live under a repo clone directory."""
    path = loop_runner._pipeline_event_log_path(DEFAULT_PROJECTS_DIR, ["repo-a"])

    assert path is not None
    assert path.parent == Path(DEFAULT_STATE_DIR)
    assert DEFAULT_PROJECTS_DIR / "repo-a" not in path.parents


def test_build_pipeline_config_maps_plan_phase_to_planning_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """A planning-only top-level run must stop after plan_review."""
    loop_runner.main(["--issues", "11", "--phases", "plan"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})


def test_build_pipeline_config_maps_implement_phase_to_review_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """The implement phase owns implementation plus PR review."""
    loop_runner.main(["--issues", "11", "--phases", "implement"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset(
        {StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.STRICT_REVIEW}
    )


def test_build_pipeline_config_maps_drive_green_phase_to_ci_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """The drive-green phase owns CI classification plus merge wait."""
    loop_runner.main(["--issues", "11", "--phases", "drive-green"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset(
        {StageName.STRICT_REVIEW, StageName.CI, StageName.MERGE_WAIT}
    )


def test_build_pipeline_config_maps_drive_green_loops_to_budget(
    dispatch: dict[str, MagicMock],
) -> None:
    """The loop CLI's drive-green loop cap must tune the merge_wait budget."""
    loop_runner.main(["--drive-green-loops", "3"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.budget_overrides["merge"] == 3


def test_build_pipeline_config_maps_agent_and_models(
    dispatch: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline path preserves provider and model selections."""
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent: "codex")

    loop_runner.main(
        [
            "--agent",
            "codex",
            "--model",
            "gpt-default",
            "--planner-model",
            "gpt-plan",
            "--reviewer-model",
            "gpt-review",
            "--implementer-model",
            "gpt-impl",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.agent == "codex"
    assert config.model == "gpt-default"
    assert config.planner_model == "gpt-plan"
    assert config.reviewer_model == "gpt-review"
    assert config.implementer_model == "gpt-impl"


def test_phase_timeout_help_documents_agent_job_scope() -> None:
    """The --phase-timeout help names the per-agent-job semantic."""
    parser = loop_runner._build_parser()
    action = next(a for a in parser._actions if "--phase-timeout" in a.option_strings)

    assert "AGENT JOB" in (action.help or "")
