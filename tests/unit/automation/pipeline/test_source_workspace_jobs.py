"""Worker-boundary tests for typed source workspaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    JobWorkspaceError,
    validate_job_workspace,
)


def _prompt() -> str:
    return "prompt"


def test_source_reading_agent_rejects_legacy_reusable_root(tmp_path: Path) -> None:
    """Legacy raw-cwd jobs fail before reaching a provider at the repo root."""
    (tmp_path / ".git").mkdir()
    job = AgentJob(
        repo="example/project",
        issue=1,
        agent="codex",
        model="model",
        prompt_builder=_prompt,
        cwd=tmp_path,
        timeout_s=1,
        sandbox="read-only",
        allowed_tools="Read,Glob,Grep",
    )

    with pytest.raises(JobWorkspaceError, match="reusable repository root"):
        validate_job_workspace(job)


def test_non_source_external_job_can_use_explicit_directory(tmp_path: Path) -> None:
    """An explicit non-source external directory remains supported."""
    job = AgentJob(
        repo="example/project",
        issue=1,
        agent="codex",
        model="model",
        prompt_builder=_prompt,
        cwd=tmp_path,
        timeout_s=1,
        allowed_tools="",
    )

    assert validate_job_workspace(job) == tmp_path.resolve()
