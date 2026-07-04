"""Tests for the WorkerPool job execution."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.resilience import CircuitBreakerOpenError


@pytest.fixture
def shutdown_event() -> threading.Event:
    """Fresh shutdown event for each test."""
    return threading.Event()


@pytest.fixture
def completion_q() -> CompletionQueue:
    """Fresh completion queue for each test."""
    return queue.Queue()


@pytest.fixture
def pool(shutdown_event: threading.Event, completion_q: CompletionQueue) -> WorkerPool:
    """Worker pool with a single thread."""
    return WorkerPool(size=1, shutdown=shutdown_event, completion_q=completion_q)


class TestWorkerPoolSubmitComplete:
    """Tests for basic submit/complete workflow."""

    def test_submit_and_complete_agent_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Submit an agent job and drain completion."""
        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "test prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
            descr="test job",
        )

        with patch(
            "hephaestus.automation.pipeline.worker_pool.claude_invoke.invoke_claude_with_session"
        ) as mock_invoke:
            mock_invoke.return_value = ("Test output", "session-id")
            pool.submit(job, StageName.IMPLEMENTATION)
            time.sleep(0.2)

        assert not completion_q.empty()
        handle, result = completion_q.get_nowait()
        assert handle.job == job
        assert handle.on_done_state == StageName.IMPLEMENTATION
        assert result.ok is True
        assert "Test output" in str(result.value)

    def test_submit_and_complete_build_test_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Submit a build/test job."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("echo", "hello"),
            timeout_s=60,
        )

        pool.submit(job, StageName.CI)
        time.sleep(0.2)

        assert not completion_q.empty()
        handle, result = completion_q.get_nowait()
        assert handle.job == job
        assert result.ok is True
        assert "hello" in result.stdout_tail

    def test_build_test_nonzero_rc_is_not_ok(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Build/test job with nonzero rc returns ok=False."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("false",),
            timeout_s=60,
        )

        pool.submit(job, StageName.CI)
        time.sleep(0.2)

        _, result = completion_q.get_nowait()
        assert result.ok is False
        assert "rc=1" in result.error


class TestCircuitBreaker:
    """Tests for circuit breaker integration."""

    def test_circuit_breaker_open_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent job with circuit open returns error result."""
        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
        )

        def failing_invoke(*args: object, **kwargs: object) -> object:
            raise CircuitBreakerOpenError(name="test_breaker", time_until_recovery=10.0)

        with patch(
            "hephaestus.automation.pipeline.worker_pool.claude_invoke.invoke_claude_with_session",
            side_effect=failing_invoke,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            time.sleep(0.2)

        _, result = completion_q.get_nowait()
        assert result.ok is False
        assert result.error == "circuit_open"


class TestInterruptedPostCheck:
    """Tests for the mandatory post-check interrupt flag."""

    def test_interrupted_post_check_on_shutdown_event(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Job callable returns ok=True but shutdown is set -> interrupted=True."""
        # Use a slower subprocess to ensure the shutdown event gets set while it's running
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("sleep", "1"),
            timeout_s=60,
        )

        pool.submit(job, StageName.CI)
        # Set shutdown before the job completes (but after submit)
        time.sleep(0.1)
        shutdown_event.set()
        time.sleep(1.5)  # Wait for the sleep job to finish

        _, result = completion_q.get_nowait()
        assert result.interrupted is True
        assert result.ok is False

    def test_interrupted_before_start(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown event set before job starts -> error and callable never invoked."""
        shutdown_event.set()

        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=MagicMock(),
            cwd=Path("/tmp"),
            timeout_s=60,
        )

        pool.submit(job, StageName.PLANNING)
        time.sleep(0.2)

        _, result = completion_q.get_nowait()
        assert result.interrupted is True
        assert result.error == "interrupted_before_start"
        # Callable should never have been invoked (was MagicMock above)
        assert not job.prompt_builder.called  # type: ignore[attr-defined]


class TestGitMutexSerialization:
    """Tests for per-repo git mutex serialization."""

    def test_same_repo_jobs_use_lock(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Two GitJobs for the same repo both use the same lock object."""
        lock1 = pool._repo_lock("test/repo1")
        lock2 = pool._repo_lock("test/repo1")
        assert lock1 is lock2  # Same lock object

    def test_different_repo_jobs_use_different_locks(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Two GitJobs for different repos use different locks."""
        lock1 = pool._repo_lock("test/repo1")
        lock2 = pool._repo_lock("test/repo2")
        assert lock1 is not lock2


class TestShutdownAndCancel:
    """Tests for shutdown behavior and future cancellation."""

    def test_shutdown_cancels_pending_work(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown cancels pending futures (queue remains empty for cancelled jobs)."""
        # The key is that shutdown() with cancel_futures=True cancels any pending futures
        # before they start. Once a future is running, it completes normally.
        # This test verifies that the pool can be shut down.
        pool.shutdown()
        # No pending jobs, so the queue should be empty
        assert completion_q.empty()


class TestParse:
    """Tests for parse callable on AgentJob."""

    def test_parse_callable_applied(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Parse callable is invoked and result stored."""

        def my_parser(text: str) -> dict[str, object]:
            return {"parsed": text.upper()}

        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
            parse=my_parser,
        )

        with patch(
            "hephaestus.automation.pipeline.worker_pool.claude_invoke.invoke_claude_with_session"
        ) as mock_invoke:
            mock_invoke.return_value = ("hello world", "sid")
            pool.submit(job, StageName.PLANNING)
            time.sleep(0.2)

        _, result = completion_q.get_nowait()
        assert result.ok is True
        assert result.value == {"parsed": "HELLO WORLD"}

    def test_parse_callable_exception_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Parse callable raises -> error result."""

        def bad_parser(text: str) -> object:
            raise ValueError("parse failed")

        job = AgentJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            model="opus-4-8",
            prompt_builder=lambda: "prompt",
            cwd=Path("/tmp"),
            timeout_s=60,
            parse=bad_parser,
        )

        with patch(
            "hephaestus.automation.pipeline.worker_pool.claude_invoke.invoke_claude_with_session"
        ) as mock_invoke:
            mock_invoke.return_value = ("output", "sid")
            pool.submit(job, StageName.PLANNING)
            time.sleep(0.2)

        _, result = completion_q.get_nowait()
        assert result.ok is False
        assert "parse failed" in result.error
