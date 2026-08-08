"""Hermetic WorkerPool agent-error boundary regressions.

The primary WorkerPool suite also exercises the host verifier's containment
primitives, so immutable host validation excludes that module. Keep focused
provider-boundary checks here so they can produce reviewed-head receipts.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from hephaestus.agents.runtime import AgentExecutionError
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool

_WORKER_POOL_MODULE = "hephaestus.automation.pipeline.worker_pool"


def test_codex_agent_execution_error_is_explicit_at_worker_boundary(
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """Provider-declared Codex failures remain explicit agent errors."""
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
        cwd=tmp_path,
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
            ),
        ):
            result = pool._run_agent(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("agent_error: codex_nested_sandbox_unsupported")
    assert "outside the enclosing API sandbox" in result.error
