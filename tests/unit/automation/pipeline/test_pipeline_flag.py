"""--pipeline / HEPH_PIPELINE dispatch tests for loop_runner.main (#1817).

The pipeline is DEFAULT OFF; the legacy path stays byte-identical when off.
The pipeline branch dispatches BEFORE ``_preflight_token_scopes`` and
``_clone_missing_repos`` (C3: the repo stage owns cloning — no double-clone).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import hephaestus.automation.loop_runner as loop_runner
import hephaestus.automation.pipeline.coordinator as coordinator_mod


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch both dispatch targets and the legacy pre-clone collaborators."""
    mocks = {
        "run_pipeline": MagicMock(return_value=0),
        "run_loop": MagicMock(return_value=[]),
        "preflight": MagicMock(),
        "clone": MagicMock(),
    }
    monkeypatch.setattr(coordinator_mod, "run_pipeline", mocks["run_pipeline"])
    monkeypatch.setattr(loop_runner, "run_loop", mocks["run_loop"])
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", mocks["preflight"])
    monkeypatch.setattr(loop_runner, "_clone_missing_repos", mocks["clone"])
    monkeypatch.setattr(
        loop_runner, "_resolve_org_and_repos", lambda args: ("org", ["repo-a"], None)
    )
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent: "claude")
    monkeypatch.delenv("HEPH_PIPELINE", raising=False)
    return mocks


@pytest.mark.parametrize(
    ("argv_flag", "env", "expect_pipeline"),
    [
        (False, None, False),  # default OFF -> legacy untouched
        (False, "0", False),
        (False, "1", True),  # env enables
        (True, None, True),  # CLI enables
        (True, "0", True),  # CLI wins over env
    ],
)
def test_flag_env_matrix(
    dispatch: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    argv_flag: bool,
    env: str | None,
    expect_pipeline: bool,
) -> None:
    """--pipeline x HEPH_PIPELINE precedence matrix (CLI flag wins)."""
    if env is not None:
        monkeypatch.setenv("HEPH_PIPELINE", env)
    argv = ["--pipeline"] if argv_flag else []

    exit_code = loop_runner.main(argv)

    assert exit_code == 0
    assert dispatch["run_pipeline"].called is expect_pipeline
    assert dispatch["run_loop"].called is not expect_pipeline


def test_pipeline_path_skips_preflight_and_clone(dispatch: dict[str, MagicMock]) -> None:
    """C3: the pipeline branch runs BEFORE preflight/clone (repo stage owns both)."""
    loop_runner.main(["--pipeline"])

    dispatch["run_pipeline"].assert_called_once()
    dispatch["preflight"].assert_not_called()
    dispatch["clone"].assert_not_called()


def test_legacy_path_still_preflights_and_clones(dispatch: dict[str, MagicMock]) -> None:
    """Flag off: the legacy pre-loop sequence is untouched."""
    loop_runner.main([])

    dispatch["preflight"].assert_called_once()
    dispatch["clone"].assert_called_once()
    dispatch["run_loop"].assert_called_once()
    dispatch["run_pipeline"].assert_not_called()


def test_pipeline_exit_code_propagates(dispatch: dict[str, MagicMock]) -> None:
    """run_pipeline's exit code IS main's exit code."""
    dispatch["run_pipeline"].return_value = 130

    assert loop_runner.main(["--pipeline"]) == 130


def test_build_pipeline_config_maps_cli_fields(dispatch: dict[str, MagicMock]) -> None:
    """_build_pipeline_config carries the CLI scope into PipelineConfig."""
    loop_runner.main(
        [
            "--pipeline",
            "--loops",
            "3",
            "--max-workers",
            "4",
            "--parallel-repos",
            "2",
            "--dry-run",
            "--issues",
            "11,12",
            "--no-advise",
            "--nitpick",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.org == "org"
    assert config.repos == ["repo-a"]
    assert config.issues == [11, 12]
    assert config.loops == 3
    assert config.max_workers == 4
    assert config.parallel_repos == 2
    assert config.dry_run is True
    assert config.no_advise is True
    assert config.nitpick is True
    assert config.prs == []


def test_build_pipeline_config_maps_agent_and_models(
    dispatch: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline path preserves provider and model selections."""
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent: "codex")

    loop_runner.main(
        [
            "--pipeline",
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


def test_phase_timeout_help_documents_pipeline_shift() -> None:
    """M4: the --phase-timeout help names the agent-job semantic shift."""
    parser = loop_runner._build_parser()
    action = next(a for a in parser._actions if "--phase-timeout" in a.option_strings)

    assert "AGENT JOB" in (action.help or "")
