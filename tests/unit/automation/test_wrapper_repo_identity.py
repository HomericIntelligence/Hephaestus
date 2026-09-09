"""Test repository identity at the shared queue command boundary."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from hephaestus.automation import implementer, loop_runner, pipeline_cli, planner, pr_reviewer
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig


@pytest.mark.parametrize(
    ("wrapper", "profile"),
    [
        pytest.param(loop_runner, "full", id="loop_runner"),
        pytest.param(planner, "planning", id="planner"),
        pytest.param(implementer, "implementation", id="implementer"),
        pytest.param(pr_reviewer, "review", id="pr_reviewer"),
    ],
)
def test_pipeline_wrappers_use_full_repository_identity(
    wrapper: ModuleType, profile: str, tmp_path: Path
) -> None:
    """All four commands preserve the owner and repository in queue configuration."""
    with (
        patch.object(
            pipeline_cli,
            "_detect_cwd_repo",
            return_value=("HomericIntelligence", "Hephaestus"),
        ) as detect_repo,
        patch.object(
            pipeline_cli, "_resolve_org_and_repos", wraps=pipeline_cli._resolve_org_and_repos
        ) as resolve_repo,
        patch.object(pipeline_cli, "resolve_agent", return_value="claude"),
        patch.object(pipeline_cli, "_source_revision", return_value=None),
        patch.object(pipeline_cli, "_setup_logging"),
        patch.object(pipeline_cli, "configure_github_throttle_from_args"),
        patch("hephaestus.utils.terminal.install_sigtstp_only"),
        patch("hephaestus.automation.pipeline.coordinator.run_pipeline", return_value=7) as run,
    ):
        result = wrapper.main(["--dry-run", "--agent", "claude", "--projects-dir", str(tmp_path)])

    assert result == 7
    resolve_repo.assert_called_once()
    assert resolve_repo.call_args is not None
    args = resolve_repo.call_args.args[0]
    assert args.profile == profile
    detect_repo.assert_called_once_with(metadata_timeout=args.metadata_timeout)
    run.assert_called_once()
    assert run.call_args is not None
    config = run.call_args.args[0]
    assert isinstance(config, PipelineConfig)
    assert config.org == "HomericIntelligence"
    assert config.repos == ["Hephaestus"]
