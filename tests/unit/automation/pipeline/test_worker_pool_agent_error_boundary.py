"""Hermetic WorkerPool agent-error boundary regressions.

The primary WorkerPool suite also exercises the host verifier's containment
primitives, so immutable host validation excludes that module. Keep focused
provider-boundary checks here so they can produce reviewed-head receipts.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.agents.runtime import AgentExecutionError
from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool, _agent_exception_result
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)
from tests.unit.automation.test_source_worktree import _repository

_WORKER_POOL_MODULE = "hephaestus.automation.pipeline.worker_pool"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            SourceWorkspacePreparationError(
                SourceWorkspacePreparationCause.GIT_METADATA_LOCK_UNAVAILABLE
            ),
            "source_workspace_preparation",
        ),
        (
            SourceWorkspaceError("workspace registration changed"),
            "source_workspace_ownership",
        ),
    ],
)
def test_pre_agent_source_workspace_failure_is_classified_at_the_job_boundary(
    error: Exception,
    expected: str,
) -> None:
    """A job that never reached a provider carries a stable pre-agent class."""
    result = _agent_exception_result(error)

    assert result.ok is False
    assert result.pre_agent_failure == expected
    assert type(error).__name__ in (result.error or "")


def test_generic_agent_error_has_no_pre_agent_classification() -> None:
    """An actual provider failure stays on the provider recovery path."""
    result = _agent_exception_result(AgentExecutionError("provider transport failed"))

    assert result.ok is False
    assert result.pre_agent_failure is None


def test_codex_agent_execution_error_is_explicit_at_worker_boundary(
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """Provider-declared Codex failures remain explicit agent errors."""
    root, _, revision = _repository(tmp_path)
    manager = SourceWorkspaceManager(root, repository="test/repo")
    binding = manager.prepare(2634, SourceLane.IMPLEMENTATION, revision, branch="writer")
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = AgentJob(
        repo="test/repo",
        issue=2634,
        agent="codex",
        model="sol",
        prompt_builder=lambda: "test prompt",
        cwd=binding.cwd,
        workspace=binding,
        timeout_s=60,
        descr="codex agent execution failure",
    )

    try:
        with (
            patch(f"{_WORKER_POOL_MODULE}.resolve_agent", return_value="codex"),
            patch(
                f"{_WORKER_POOL_MODULE}.run_agent_session",
                side_effect=AgentExecutionError(
                    "codex_nested_sandbox_unsupported: run the outer loop "
                    "outside the enclosing API sandbox"
                ),
            ) as invoke,
        ):
            result = pool._run_agent(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("agent_error: codex_nested_sandbox_unsupported")
    assert "outside the enclosing API sandbox" in result.error
    invoke.assert_called_once()
    assert invoke.call_args.kwargs["cwd"] == binding.cwd
