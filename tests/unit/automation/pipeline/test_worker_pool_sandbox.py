"""Tests for AgentJob.sandbox threading through WorkerPool._invoke (issue #2055).

Pins that a ``sandbox="read-only"`` job reaches the actual provider kwargs
(``allowed_tools``/``permission_mode`` for Claude, ``sandbox`` for
codex/pi) rather than merely being claimed by prompt text, and that the
default ``sandbox="workspace-write"`` path is byte-identical to pre-#2055
behavior for every other stage.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool

_WP = "hephaestus.automation.pipeline.worker_pool"


@pytest.fixture
def shutdown_event() -> threading.Event:
    """Fresh shutdown event for each test."""
    return threading.Event()


@pytest.fixture
def completion_q() -> CompletionQueue:
    """Fresh completion queue for each test."""
    import queue

    return queue.Queue()


@pytest.fixture
def pool(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> Iterator[WorkerPool]:
    """Worker pool with a single thread and a temp cross-process lock dir."""
    p = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    yield p
    p.shutdown()


def _agent_job(**overrides: object) -> AgentJob:
    defaults: dict[str, object] = {
        "repo": "test/repo",
        "issue": 123,
        "agent": "claude",
        "model": "opus-4-8",
        "prompt_builder": lambda: "test prompt",
        "cwd": Path("/tmp"),
        "timeout_s": 60,
        "descr": "test job",
    }
    defaults.update(overrides)
    return AgentJob(**defaults)  # type: ignore[arg-type]


class TestClaudeSandbox:
    """sandbox threading through the Claude (invoke_claude_with_session) path."""

    def test_read_only_reaches_allowed_tools_and_permission_mode(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        """sandbox='read-only' adds allowed_tools/permission_mode kwargs."""
        job = _agent_job(sandbox="read-only")

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("verdict output", "session-id")
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        mock_invoke.assert_called_once_with(
            repo=job.repo,
            issue=job.issue,
            agent=job.agent,
            prompt="test prompt",
            model=job.model,
            cwd=job.cwd,
            timeout=job.timeout_s,
            output_format=job.output_format,
            allowed_tools="Read,Glob,Grep",
            permission_mode="dontAsk",
        )

    def test_default_workspace_write_omits_read_only_kwargs(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        """Default sandbox (workspace-write) passes no allowed_tools/permission_mode."""
        job = _agent_job()
        assert job.sandbox == "workspace-write"

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("output", "session-id")
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        mock_invoke.assert_called_once_with(
            repo=job.repo,
            issue=job.issue,
            agent=job.agent,
            prompt="test prompt",
            model=job.model,
            cwd=job.cwd,
            timeout=job.timeout_s,
            output_format=job.output_format,
        )


class TestNonClaudeSandbox:
    """sandbox threading through the codex/pi (run_agent_session) path."""

    def test_read_only_reaches_run_agent_session(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        """sandbox='read-only' is forwarded verbatim to run_agent_session."""
        job = _agent_job(agent="codex", sandbox="read-only")

        session_result = MagicMock()
        session_result.stdout = "codex verdict"
        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        mock_session.assert_called_once_with(
            agent="codex",
            prompt="test prompt",
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            sandbox="read-only",
            approval="never",
        )

    def test_default_workspace_write_unchanged(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        """Default sandbox stays 'workspace-write' for every pre-existing caller."""
        job = _agent_job(agent="codex")
        assert job.sandbox == "workspace-write"

        session_result = MagicMock()
        session_result.stdout = "codex output"
        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        mock_session.assert_called_once_with(
            agent="codex",
            prompt="test prompt",
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            sandbox="workspace-write",
            approval="never",
        )
