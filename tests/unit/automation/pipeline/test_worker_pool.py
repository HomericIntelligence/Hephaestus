"""Tests for the WorkerPool job execution."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool, _repo_lock_path
from hephaestus.resilience import CircuitBreakerOpenError, get_circuit_breaker
from hephaestus.utils.file_lock import LockUnavailableError
from hephaestus.utils.helpers import get_repo_root

_WP = "hephaestus.automation.pipeline.worker_pool"


@pytest.fixture
def shutdown_event() -> threading.Event:
    """Fresh shutdown event for each test."""
    return threading.Event()


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


def _agent_job(model: str = "opus-4-8", **overrides: object) -> AgentJob:
    """Build an AgentJob with test defaults.

    Failing-path tests pass a unique ``model`` to keep their invocation
    details distinct while the runtime circuit breaker remains shared.
    """
    defaults: dict[str, object] = {
        "repo": "test/repo",
        "issue": 123,
        "agent": "claude",
        "model": model,
        "prompt_builder": lambda: "test prompt",
        "cwd": Path("/tmp"),
        "timeout_s": 60,
        "descr": "test job",
    }
    defaults.update(overrides)
    return AgentJob(**defaults)  # type: ignore[arg-type]


class TestWorkerPoolSubmitComplete:
    """Tests for basic submit/complete workflow."""

    def test_submit_and_complete_agent_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Submit a Claude agent job and drain completion."""
        job = _agent_job()

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("Test output", "session-id")
            pool.submit(job, StageName.IMPLEMENTATION)
            handle, result = completion_q.get(timeout=10)

        assert handle.job is job
        assert handle.on_done_state == StageName.IMPLEMENTATION
        assert result.ok is True
        assert "Test output" in str(result.value)

    def test_submit_and_complete_non_claude_agent_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Non-Claude agents dispatch through run_agent_session."""
        job = _agent_job(agent="codex")

        session_result = MagicMock()
        session_result.stdout = "codex output"
        with (
            patch(f"{_WP}.resolve_agent", return_value="codex") as mock_resolve,
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        mock_resolve.assert_called_once_with("codex")
        mock_session.assert_called_once_with(
            agent="codex",
            prompt="test prompt",
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            sandbox="workspace-write",
            approval="never",
        )
        assert result.ok is True
        assert result.value == "codex output"

    def test_read_only_agent_job_propagates_its_sandbox_to_codex(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Strict-review jobs must not be silently widened to workspace-write."""
        job = _agent_job(agent="codex", sandbox="read-only")
        session_result = MagicMock(stdout="strict review")

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert mock_session.call_args.kwargs["sandbox"] == "read-only"

    def test_read_only_agent_job_scopes_claude_to_read_tools(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Strict-review Claude jobs receive no write or shell capability."""
        job = _agent_job(sandbox="read-only")
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("GO", "s"),
            ) as invoke,
        ):
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert invoke.call_args.kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert invoke.call_args.kwargs["permission_mode"] == "dontAsk"

    def test_expected_head_mismatch_blocks_agent_before_invocation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A strict reviewer never sees a worktree at a different commit."""
        expected = "a" * 40
        job = _agent_job(expected_head_sha=expected)
        head_result = subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"], 0, stdout=("b" * 40) + "\n", stderr=""
        )
        with (
            patch(f"{_WP}.subprocess.run", return_value=head_result),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as invoke,
        ):
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None and "local_head_mismatch" in result.error
        invoke.assert_not_called()

    def test_dirty_expected_head_worktree_blocks_agent_before_invocation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Matching SHA alone is insufficient when local files differ from it."""
        expected = "a" * 40
        job = _agent_job(expected_head_sha=expected)
        head_result = subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"], 0, stdout=expected + "\n", stderr=""
        )
        dirty_result = subprocess.CompletedProcess(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            0,
            stdout=" M implementation.py\n?? untracked.txt\n",
            stderr="",
        )
        with (
            patch(f"{_WP}.subprocess.run", side_effect=[head_result, dirty_result]),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as invoke,
        ):
            pool.submit(job, StageName.STRICT_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None and "local_worktree_dirty" in result.error
        invoke.assert_not_called()

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
        handle, result = completion_q.get(timeout=10)

        assert handle.job is job
        assert result.ok is True
        assert "hello" in result.stdout_tail

    def test_worker_claim_logs_and_result_carries_worker_id(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A worker records its identity when it starts executing a submitted job."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=tmp_path,
            argv=("pytest", "-q"),
            timeout_s=60,
            descr="unit gate",
        )
        completed = subprocess.CompletedProcess(job.argv, 0, stdout="", stderr="")
        caplog.set_level(logging.INFO, logger=_WP)

        with patch(f"{_WP}.subprocess.run", return_value=completed):
            pool.submit(
                job,
                StageName.CI,
                claim_key="test/repo#123",
                claim_stage="ci",
            )
            _handle, result = completion_q.get(timeout=10)

        worker_id = getattr(result, "worker_id", "")
        assert worker_id
        assert any(
            "worker_claim" in record.message
            and worker_id in record.message
            and "item=test/repo#123" in record.message
            and "stage=ci" in record.message
            for record in caplog.records
        )

    def test_distinct_workers_claim_concurrent_queue_entries(
        self,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Concurrent entries expose distinct worker IDs in claim logs and results."""
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        jobs = [
            BuildTestJob(
                repo="test/repo",
                cwd=tmp_path,
                argv=("pytest", "-q", f"case-{idx}"),
                timeout_s=60,
                descr=f"unit gate {idx}",
            )
            for idx in range(2)
        ]
        barrier = threading.Barrier(2, timeout=5)

        def complete_after_both_workers_enter(
            argv: tuple[str, ...],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            barrier.wait()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        caplog.set_level(logging.INFO, logger=_WP)

        try:
            with patch(f"{_WP}.subprocess.run", side_effect=complete_after_both_workers_enter):
                pool.submit(
                    jobs[0],
                    StageName.CI,
                    claim_key="test/repo#123",
                    claim_stage="ci",
                )
                pool.submit(
                    jobs[1],
                    StageName.PR_REVIEW,
                    claim_key="test/repo!456",
                    claim_stage="pr_review",
                )
                results = [completion_q.get(timeout=10)[1] for _ in jobs]
        finally:
            pool.shutdown()

        worker_ids = {result.worker_id for result in results}
        assert len(worker_ids) == 2
        claim_messages = [
            record.message for record in caplog.records if "worker_claim" in record.message
        ]
        assert any(
            worker_id in message and "item=test/repo#123" in message and "stage=ci" in message
            for worker_id in worker_ids
            for message in claim_messages
        )
        assert any(
            worker_id in message
            and "item=test/repo!456" in message
            and "stage=pr_review" in message
            for worker_id in worker_ids
            for message in claim_messages
        )

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
        _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "rc=1" in result.error

    def test_build_test_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Build/test job hitting its timeout returns an error result."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("sleep", "60"),
            timeout_s=1,
        )

        with patch(
            f"{_WP}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["sleep", "60"], timeout=1),
        ):
            pool.submit(job, StageName.CI)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "timeout"


class TestAgentErrorHandling:
    """Tests for agent-job error handling paths."""

    def test_agent_breaker_is_shared_across_models(
        self,
        pool: WorkerPool,
    ) -> None:
        """Failures for one model open the runtime breaker for every model."""
        get_circuit_breaker("agent:claude", failure_threshold=2)
        jobs = [_agent_job(model=model) for model in ("opus", "sonnet")]

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=RuntimeError("runtime unavailable"),
            ) as invoke,
        ):
            first = pool._run_agent(jobs[0])
            second = pool._run_agent(jobs[0])
            blocked = pool._run_agent(jobs[1])

        assert first.error == "RuntimeError: runtime unavailable"
        assert second.error == "RuntimeError: runtime unavailable"
        assert blocked.error == "circuit_open"
        assert invoke.call_count == 2

    def test_circuit_breaker_open_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent job with circuit open returns error result."""
        job = _agent_job(model="model-cb-open", prompt_builder=lambda: "prompt")

        def failing_invoke(*args: object, **kwargs: object) -> object:
            raise CircuitBreakerOpenError(name="test_breaker", time_until_recovery=10.0)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=failing_invoke,
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "circuit_open"

    def test_agent_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent invocation timeout maps to error='timeout' (not retried)."""
        job = _agent_job(model="model-agent-timeout")

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=60),
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=30)

        assert result.ok is False
        assert result.error == "timeout"

    def test_agent_called_process_error_returns_rc_and_tails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent CalledProcessError maps to rc=<n> with stdout/stderr tails."""
        job = _agent_job(model="model-agent-cpe")
        exc = subprocess.CalledProcessError(
            returncode=2,
            cmd=["claude"],
            output="partial stdout",
            stderr="nonretryable failure detail",
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=exc,
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=30)

        assert result.ok is False
        assert result.error == "rc=2"
        assert "partial stdout" in result.stdout_tail
        assert "nonretryable failure detail" in result.stderr_tail

    def test_generic_exception_converted_to_error_result(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected exception inside the job is bounded by the shared cap."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)

        def exploding_builder() -> str:
            raise RuntimeError("prompt builder exploded " + ("x" * 200))

        job = _agent_job(model="model-generic-exc", prompt_builder=exploding_builder)

        with patch(f"{_WP}.resolve_agent", return_value="claude"):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("RuntimeError: ")
        assert len(result.error) == small_err_max

    def test_run_agent_classifies_resolve_agent_exception(
        self,
        pool: WorkerPool,
    ) -> None:
        """resolve_agent failures are classified inside _run_agent."""
        job = _agent_job(model="model-resolve-generic", agent="bad-agent")

        with patch(f"{_WP}.resolve_agent", side_effect=ValueError("bad agent")):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "ValueError: bad agent"

    def test_run_agent_classifies_prompt_builder_exception(self, pool: WorkerPool) -> None:
        """Prompt builder failures are classified inside _run_agent."""

        def missing_prompt() -> str:
            raise KeyError("prompt-template")

        job = _agent_job(model="model-prompt-generic", prompt_builder=missing_prompt)

        with patch(f"{_WP}.resolve_agent", return_value="claude"):
            result = pool._run_agent(job)

        assert result.ok is False
        assert "KeyError" in (result.error or "")
        assert "prompt-template" in (result.error or "")

    def test_run_converts_escaping_exception_to_bounded_error(
        self,
        pool: WorkerPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exceptions escaping _run_agent are still capped in _run."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)
        job = _agent_job(model="model-run-generic", prompt_builder=lambda: "prompt")

        with patch.object(pool, "_run_agent", side_effect=RuntimeError("z" * 200)):
            result = pool._run(job)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("RuntimeError: ")
        assert len(result.error) == small_err_max

    def test_run_agent_classifies_resilient_call_exception(self, pool: WorkerPool) -> None:
        """Unexpected resilience-wrapper failures are classified inside _run_agent."""
        job = _agent_job(model="model-resilient-generic", prompt_builder=lambda: "prompt")

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.resilient_call", side_effect=OSError("retry wrapper failed")),
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "OSError: retry wrapper failed"

    def test_run_agent_does_not_retry_transient_error_after_shutdown(
        self, pool: WorkerPool, shutdown_event: threading.Event
    ) -> None:
        """Shutdown suppresses retrying an interrupted agent session."""
        job = _agent_job(model="model-shutdown-no-retry", prompt_builder=lambda: "prompt")
        shutdown_event.set()

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=OSError("connection reset"),
            ) as invoke,
            patch("hephaestus.utils.retry.time.sleep") as sleep,
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "OSError: connection reset"
        assert invoke.call_count == 1
        sleep.assert_not_called()

    def test_unknown_job_type_returns_error_result(self, pool: WorkerPool) -> None:
        """A job of unknown type is converted to a TypeError error result."""
        result = pool._run(cast(AgentJob, object()))
        assert result.ok is False
        assert "TypeError" in (result.error or "")


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

        job = _agent_job(prompt_builder=lambda: "prompt", parse=my_parser)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("hello world", "sid")
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"parsed": "HELLO WORLD"}

    def test_parse_callable_exception_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parse callable failures are bounded by the shared cap."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)

        def bad_parser(text: str) -> object:
            raise ValueError("parse failed " + ("y" * 200))

        job = _agent_job(prompt_builder=lambda: "prompt", parse=bad_parser)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("output", "sid")
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("parse failed: ValueError: ")
        assert len(result.error) == small_err_max


class TestInterruptedPostCheck:
    """Tests for the mandatory post-check interrupt flag."""

    def test_interrupted_post_check_on_shutdown_event(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown set WHILE the job runs -> post-check forces interrupted.

        The prompt builder blocks until the test sets the shutdown event, so
        the job is deterministically mid-flight when the event fires — this
        proves the POST-check path ran, not the before-start pre-check.
        """
        started = threading.Event()

        def blocking_builder() -> str:
            started.set()
            assert shutdown_event.wait(timeout=10)
            return "prompt"

        job = _agent_job(prompt_builder=blocking_builder)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("done", "sid")
            pool.submit(job, StageName.CI)
            assert started.wait(timeout=10), "job never started"
            shutdown_event.set()
            _handle, result = completion_q.get(timeout=10)

        assert result.interrupted is True
        assert result.ok is False
        # Proves the POST-check ran: the pre-check path would have stamped
        # this sentinel error and never invoked the prompt builder.
        assert result.error != "interrupted_before_start"

    def test_interrupted_before_start(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown event set before job starts -> error and callable never invoked."""
        shutdown_event.set()

        job = _agent_job(prompt_builder=MagicMock())

        with patch(f"{_WP}.time.monotonic", side_effect=[10.0, 10.25]):
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.interrupted is True
        assert result.ok is False
        assert result.error == "interrupted_before_start"
        assert result.duration_s == pytest.approx(0.25)
        assert result.stdout_tail == ""
        assert result.stderr_tail == ""
        # Callable should never have been invoked (was MagicMock above)
        assert not job.prompt_builder.called  # type: ignore[attr-defined]


class TestGitOps:
    """Tests for every GitJob op dispatch (helpers mocked)."""

    def test_create_worktree_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """create_worktree forwards kwargs to WorktreeManager.create_worktree."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(
            base_dir=tmp_path / "build" / ".worktrees",
            repo_root=tmp_path,
        )
        instance.create_worktree.assert_called_once_with(
            issue_number=7,
            branch_name="7-auto",
            timeout=60,
        )
        assert result.ok is True
        assert result.value == str(tmp_path / "wt")

    def test_create_worktree_syncs_adopted_clean_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """sync_to_remote is a worker concern, not leaked into WorktreeManager."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-existing",
                "refresh_base": False,
                "repo_root": str(tmp_path),
                "sync_to_remote": True,
                "pr_number": 70,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True) as mock_clean,
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        instance.create_worktree.assert_called_once_with(
            issue_number=7,
            branch_name="7-existing",
            refresh_base=False,
            timeout=60,
        )
        mock_clean.assert_called_once_with(tmp_path / "wt", timeout=60)
        mock_sync.assert_called_once_with(tmp_path / "wt", "7-existing", pr_number=70, timeout=60)
        assert result.ok is True
        assert result.value == {
            "path": str(tmp_path / "wt"),
            "dirty": False,
            "status": "",
            "diff": "",
        }

    def test_create_worktree_defaults_repo_root_to_ambient_cwd(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """No repo_root kwarg falls back to get_repo_root() (single-repo callers)."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={"issue_number": 7, "branch_name": "7-auto"},
        )
        instance = MagicMock()
        ambient_root = get_repo_root()
        instance.create_worktree.return_value = ambient_root / "build" / ".worktrees" / "issue-7"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager,
            patch(f"{_WP}.get_repo_root", return_value=ambient_root),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(
            base_dir=ambient_root / "build" / ".worktrees",
            repo_root=ambient_root,
        )
        assert result.ok is True

    def test_create_worktree_escaped_repo_root_fails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A worktree path outside the resolved repo_root is a hard failure."""
        repo_root = tmp_path / "Argus"
        job = GitJob(
            repo="Argus",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 107,
                "branch_name": "107-auto-impl",
                "repo_root": str(repo_root),
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "Hephaestus" / "issue-107"
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert "escaped resolved repo root" in result.error
        assert str(repo_root) in result.error

    def test_remove_worktree_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """remove_worktree forwards kwargs to WorktreeManager.remove_worktree."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={"issue_number": 7, "force": True},
        )
        instance = MagicMock()
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        instance.remove_worktree.assert_called_once_with(issue_number=7, force=True, timeout=60)
        assert result.ok is True

    def test_remove_worktree_fallback_honors_repo_root_kwarg(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The manager-state fallback (no worktree_path) still scopes repo_root."""
        other_repo = tmp_path / "Argus"
        job = GitJob(
            repo="Argus",
            op="remove_worktree",
            timeout_s=60,
            kwargs={"issue_number": 107, "force": True, "repo_root": str(other_repo)},
        )
        instance = MagicMock()
        with patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(repo_root=other_repo)
        assert result.ok is True

    def test_remove_worktree_path_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Path cleanup removes the known worktree path even with a fresh manager."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-7"),
                "repo_root": str(tmp_path),
                "force": True,
            },
        )
        with patch(f"{_WP}.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_any_call(
            ["git", "worktree", "remove", str(tmp_path / "issue-7"), "--force"],
            cwd=tmp_path,
            timeout=60,
        )
        mock_run.assert_any_call(
            ["git", "worktree", "prune"],
            cwd=tmp_path,
            check=False,
            timeout=60,
        )
        assert result.ok is True

    @pytest.mark.parametrize("rebase_clean", [True, False])
    def test_rebase_dispatch_propagates_bool(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        rebase_clean: bool,
    ) -> None:
        """Rebase forwards to rebase_worktree_onto; its bool is ok AND value."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={"cwd": Path("/tmp/wt"), "base_branch": "main"},
        )
        with patch(
            "hephaestus.automation.git_utils.rebase_worktree_onto",
            return_value=rebase_clean,
        ) as mock_rebase:
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        mock_rebase.assert_called_once_with(
            cwd=Path("/tmp/wt"),
            base_branch="main",
            timeout=60,
        )
        assert result.ok is rebase_clean
        assert result.value is rebase_clean

    def test_push_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Push forwards to push_current_branch_with_lease_on_divergence."""
        job = GitJob(
            repo="test/repo",
            op="push",
            timeout_s=60,
            kwargs={"cwd": Path("/tmp/wt"), "branch": "7-auto"},
        )
        with patch(
            "hephaestus.automation.git_utils.push_current_branch_with_lease_on_divergence"
        ) as mock_push:
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        mock_push.assert_called_once_with(cwd=Path("/tmp/wt"), branch="7-auto", timeout=60)
        assert result.ok is True

    def test_commit_push_extracts_explicit_keys(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """commit_push passes only accepted keys ('branch' must not crash it)."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 5,
                "worktree_path": tmp_path,
                "branch": "5-auto",
                "agent": "claude",
            },
        )
        with (
            patch(
                "hephaestus.automation.git_utils.commit_if_changes", return_value=True
            ) as mock_commit,
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
        ):
            pool.submit(job, StageName.CI)
            _, result = completion_q.get(timeout=10)

        mock_commit.assert_called_once_with(
            5,
            tmp_path,
            "claude",
            allowed_paths=None,
            timeout=60,
        )
        mock_push.assert_called_once_with("5-auto", tmp_path, timeout=60)
        assert result.ok is True
        assert result.value is True  # value carries commit_if_changes' bool

    def test_commit_push_value_false_does_not_push_when_nothing_committed(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """commit_push reports value=False without pushing a clean tree."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5, "worktree_path": tmp_path},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=False),
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
        ):
            pool.submit(job, StageName.CI)
            _, result = completion_q.get(timeout=10)

        mock_push.assert_not_called()
        assert result.ok is True
        assert result.value is False

    def test_commit_push_missing_worktree_path_is_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Missing worktree_path is an explicit error, not a silent skip."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes") as mock_commit,
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
        ):
            pool.submit(job, StageName.CI)
            _, result = completion_q.get(timeout=10)

        mock_commit.assert_not_called()
        mock_push.assert_not_called()
        assert result.ok is False
        assert "worktree_path" in result.error

    def test_clone_dispatch_threads_timeout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Clone runs gh repo clone with the job's timeout budget."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once_with(
            ["gh", "repo", "clone", "owner/name", "/tmp/dest"],
            cwd=None,
            timeout=120,
        )
        assert result.ok is True

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"repo": "owner/name"}, {"dest": "/tmp/dest"}, {"repo": "", "dest": ""}],
    )
    def test_clone_missing_args_fast_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        kwargs: dict[str, str],
    ) -> None:
        """Clone with empty repo/dest fails fast without shelling out."""
        job = GitJob(repo="test/repo", op="clone", timeout_s=60, kwargs=kwargs)
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_not_called()
        assert result.ok is False
        assert "clone requires" in result.error

    def test_git_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A git helper hitting its timeout maps to error='timeout'."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=1,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        with patch(
            "hephaestus.automation.git_utils.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=1),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "timeout"

    def test_git_called_process_error_returns_rc_and_tails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Git CalledProcessError maps to rc=<n> with stdout/stderr tails."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=60,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        exc = subprocess.CalledProcessError(
            returncode=128,
            cmd=["gh", "repo", "clone", "owner/name", "/tmp/dest"],
            output="clone stdout tail",
            stderr="fatal: repository access denied",
        )

        with patch("hephaestus.automation.git_utils.run", side_effect=exc):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "rc=128"
        assert result.stdout_tail == "clone stdout tail"
        assert result.stderr_tail == "fatal: repository access denied"

    def test_unknown_op_fallback(self, pool: WorkerPool) -> None:
        """The defensive unknown-op branch returns an error result.

        Unreachable via GitJob.__post_init__ validation, so exercised by
        bypassing the constructor.
        """
        bogus = MagicMock(spec=GitJob)
        bogus.op = "bogus"
        bogus.repo = "test/repo"
        bogus.kwargs = {}
        result = pool._dispatch_git_op(cast(GitJob, bogus))
        assert result.ok is False
        assert "unknown op" in (result.error or "")


class TestGitLocking:
    """Tests for per-repo serialization and cross-process file locking."""

    def test_same_repo_jobs_serialize_with_mutex(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Two GitJobs for the same repo run serially (held by lock)."""
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )

        events: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)  # Both jobs reach here simultaneously

        def git_job_entrypoint(job_name: str) -> None:
            # NOTE: _run_git already holds the per-repo lock around this
            # call — re-acquiring pool._repo_lock here would self-deadlock
            # (threading.Lock is not reentrant). The barrier alone proves
            # serialization: if the pool serialized us, the two jobs never
            # overlap, so neither can satisfy the 2-party barrier.
            with lock:
                events.append(f"{job_name}:entered_lock")
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                # Expected under serialization: the peer never arrives
                # while we are inside the pool's critical section.
                with lock:
                    events.append(f"{job_name}:barrier_failed_expected")
            with lock:
                events.append(f"{job_name}:exited_lock")

        job1 = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})
        job2 = GitJob(repo="test/repo", op="remove_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.side_effect = lambda **kwargs: git_job_entrypoint("job1")
        instance.remove_worktree.side_effect = lambda **kwargs: git_job_entrypoint("job2")

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job1, StageName.REPO)
            pool.submit(job2, StageName.REPO)
            # Block on the completion channel instead of sleeping: robust
            # under the slow pure-Python coverage tracer and proves both
            # jobs actually complete.
            completions = [completion_q.get(timeout=10.0) for _ in range(2)]

        pool.shutdown()

        assert len(completions) == 2

        # Verify serialization: both jobs must have failed the barrier —
        # they never overlapped inside the pool's critical section.
        assert len([e for e in events if e.endswith(":barrier_failed_expected")]) == 2, events

    def test_different_repo_jobs_run_concurrently(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Two GitJobs for different repos overlap (different locks)."""
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        barrier = threading.Barrier(2)

        def wait_at_barrier(**kwargs: object) -> None:
            # Both jobs must be inside their critical sections at once to
            # satisfy the barrier; a 10 s timeout fails the test if the pool
            # wrongly serialized different repos.
            barrier.wait(timeout=10)

        job1 = GitJob(repo="test/repo1", op="create_worktree", timeout_s=60, kwargs={})
        job2 = GitJob(repo="test/repo2", op="create_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.side_effect = wait_at_barrier

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job1, StageName.REPO)
            pool.submit(job2, StageName.REPO)
            completions = [completion_q.get(timeout=10.0) for _ in range(2)]

        pool.shutdown()

        assert all(result.ok for _, result in completions)

    def test_different_repo_jobs_use_different_locks(
        self,
        pool: WorkerPool,
    ) -> None:
        """Two active GitJob repo contexts use different in-process locks."""
        with pool._repo_lock("test/repo1"), pool._repo_lock("test/repo2"):
            with pool._repo_locks_guard:
                lock1 = pool._repo_locks["test/repo1"].lock
                lock2 = pool._repo_locks["test/repo2"].lock

        assert lock1 is not lock2
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_repo_lock_evicted_after_git_job_completes(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A completed GitJob does not leave an idle repo lock cached forever."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.return_value = None
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_repo_lock_not_evicted_while_waiter_holds_it(
        self,
        pool: WorkerPool,
    ) -> None:
        """A waiting same-repo user keeps the shared lock entry until it exits."""
        waiter_acquired = threading.Event()
        release_waiter = threading.Event()

        def wait_for_users(expected: int) -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with pool._repo_locks_guard:
                    entry = pool._repo_locks.get("test/repo")
                    if entry is not None and entry.users == expected:
                        return
                time.sleep(0.01)
            pytest.fail(f"repo lock users never reached {expected}")

        def waiter() -> None:
            with pool._repo_lock("test/repo"):
                waiter_acquired.set()
                release_waiter.wait(timeout=5.0)

        with pool._repo_lock("test/repo"):
            with pool._repo_locks_guard:
                entry = pool._repo_locks["test/repo"]
            thread = threading.Thread(target=waiter)
            thread.start()
            wait_for_users(2)

        assert waiter_acquired.wait(timeout=5.0)
        with pool._repo_locks_guard:
            assert pool._repo_locks.get("test/repo") is entry

        release_waiter.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        with pool._repo_locks_guard:
            assert "test/repo" not in pool._repo_locks

    def test_repo_lock_path_anchors_at_state_dir(self) -> None:
        """Default lock path is anchored at repo_root/DEFAULT_STATE_DIR, not CWD."""
        expected = get_repo_root() / DEFAULT_STATE_DIR / "locks" / "git-a_b.lock"
        assert _repo_lock_path("a/b") == expected

    def test_repo_lock_path_honors_override(self, tmp_path: Path) -> None:
        """An explicit lock_dir overrides the state-dir anchor (test seam)."""
        assert _repo_lock_path("a/b", tmp_path) == tmp_path / "git-a_b.lock"

    def test_git_job_takes_cross_process_file_lock(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Running a GitJob creates the per-repo sentinel file in lock_dir."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})
        instance = MagicMock()
        instance.create_worktree.return_value = None
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert (tmp_path / "locks" / "git-test_repo.lock").exists()

    def test_git_file_lock_timeout_returns_lock_timeout_and_releases_repo_lock(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A held cross-process lock fails fast with lock_timeout."""
        fcntl = pytest.importorskip("fcntl")
        lock_path = _repo_lock_path("test/repo", tmp_path / "locks")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=0, kwargs={})

        try:
            fcntl.flock(held_fd, fcntl.LOCK_EX)
            with patch(f"{_WP}.WorktreeManager") as manager:
                result = pool._run_git(job)
        finally:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
            os.close(held_fd)

        manager.assert_not_called()
        assert result.ok is False
        assert result.error == "lock_timeout"
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_git_file_lock_wait_is_interrupted_by_shutdown(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
    ) -> None:
        """Shutdown while waiting for the file lock returns an interrupted result."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})

        def interrupting_wait(timeout: float | None = None) -> bool:
            shutdown_event.set()
            return True

        with (
            patch(f"{_WP}.file_lock", side_effect=LockUnavailableError("held")),
            patch.object(shutdown_event, "wait", side_effect=interrupting_wait),
            patch(f"{_WP}.WorktreeManager") as manager,
        ):
            result = pool._run_git(job)

        manager.assert_not_called()
        assert result.ok is False
        assert result.interrupted is True
        assert result.error == "interrupted_waiting_for_git_lock"
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_git_file_lock_wait_does_not_swallow_dispatch_lock_errors(
        self,
        pool: WorkerPool,
    ) -> None:
        """Only outer lock acquisition failures are mapped to lock_timeout."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=0, kwargs={})
        instance = MagicMock()
        instance.create_worktree.side_effect = LockUnavailableError("inner lock")

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            with pytest.raises(LockUnavailableError, match="inner lock"):
                pool._run_git(job)


class TestShutdownAndCancel:
    """Tests for shutdown behavior and future cancellation."""

    def test_shutdown_cancels_queued_job_and_emits_no_completion_for_it(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Cancelled queued jobs emit NO completion; the running one completes.

        A slow job occupies the single worker while a second job sits queued;
        shutdown(cancel_futures=True) cancels the queued one. Exactly one
        completion (the running job's, marked interrupted) must arrive.
        """
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=1,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        started = threading.Event()
        release = threading.Event()

        def slow_builder() -> str:
            started.set()
            release.wait(timeout=10)
            return "prompt"

        slow_job = _agent_job(prompt_builder=slow_builder)
        queued_job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("echo", "never-runs"),
            timeout_s=60,
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("done", "sid")
            pool.submit(slow_job, StageName.PLANNING)
            assert started.wait(timeout=10), "slow job never started"
            pool.submit(queued_job, StageName.CI)  # queued behind the busy worker
            pool.shutdown()  # sets shutdown event + cancel_futures=True
            release.set()

            handle, result = completion_q.get(timeout=10)

        # Exactly the running job's completion arrives ...
        assert handle.job is slow_job
        assert result.interrupted is True  # shutdown was set mid-flight
        # ... and NONE for the cancelled queued job.
        with pytest.raises(queue.Empty):
            completion_q.get(timeout=0.5)


class TestOnFutureDone:
    """Tests for the completion-loss guarantees of _on_future_done."""

    def test_cancelled_future_emits_no_completion(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A cancelled future synthesizes no completion tuple."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.CI,
        )
        future: Future[JobResult] = Future()
        future.cancel()
        pool._on_future_done(handle, future)
        assert completion_q.empty()

    @pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3), GeneratorExit()])
    def test_run_converts_process_control_escape_with_worker_id(
        self, pool: WorkerPool, exc: BaseException
    ) -> None:
        """Escapes inside the worker preserve the executing worker identity."""
        job = BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1)

        with patch.object(pool, "_run_build_test", side_effect=exc):
            result = pool._run(job, claim_key="r#1", claim_stage="ci")

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith(f"worker_crash: {type(exc).__name__}")
        assert result.worker_id == threading.current_thread().name

    def test_exception_future_emits_worker_crash_completion_and_logs_traceback(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RuntimeError from future.result() becomes worker_crash with traceback."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.CI,
        )
        future: Future[JobResult] = Future()
        future.set_exception(RuntimeError("boom"))

        with caplog.at_level(logging.INFO, logger=_WP):
            pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error.startswith("worker_crash: RuntimeError")
        assert any(
            record.levelno == logging.ERROR and record.exc_info is not None
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        ("exc", "expected_level"),
        [
            (KeyboardInterrupt(), logging.WARNING),
            (SystemExit(3), logging.INFO),
            (GeneratorExit(), logging.INFO),
        ],
    )
    def test_process_control_future_emits_worker_crash_completion_without_traceback(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        caplog: pytest.LogCaptureFixture,
        exc: BaseException,
        expected_level: int,
    ) -> None:
        """Process-control escapes stay at lower severity and do not log tracebacks."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.CI,
        )
        future: Future[JobResult] = Future()
        future.set_exception(exc)

        with caplog.at_level(logging.INFO, logger=_WP):
            pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error.startswith(f"worker_crash: {type(exc).__name__}")
        assert any(record.levelno == expected_level for record in caplog.records)
        assert not any(record.exc_info is not None for record in caplog.records)

    def test_raising_future_emits_truncated_worker_crash_completion(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker crash message longer than the cap is truncated once."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.CI,
        )
        future: Future[JobResult] = Future()
        future.set_exception(RuntimeError("w" * 200))

        pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("worker_crash: RuntimeError: ")
        assert len(result.error) == small_err_max


@pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="process-group termination unavailable on this platform"
)
class TestShutdownReapsSubprocess:
    """WorkerPool.shutdown() SIGTERMs in-flight agent process groups (#2059)."""

    def test_shutdown_terminates_running_agent_subprocess_fast(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A slow claude job is reaped by shutdown() instead of running to timeout.

        Regression for the #2059 leak: before the fix, ``pool.shutdown()`` only
        cancelled un-started futures — a job already blocked in a claude
        subprocess kept running. Now the child is spawned via the real
        ``_run_tracked`` (process group + registry) and SIGTERMed on shutdown.
        """
        from hephaestus.automation import claude_invoke

        started = threading.Event()
        real_run_tracked = claude_invoke._run_tracked
        # A 60s sleeper stands in for a wedged claude reviewer.
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]

        def fake_run_tracked(_cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            started.set()
            # Swap the "claude" binary for a real long-lived Python sleeper but
            # keep the REAL _run_tracked spawn (Popen + process-group tracking).
            return real_run_tracked(list(sleeper), **kwargs)

        job = _agent_job(model="reap-test", timeout_s=60, session_agent="implementer")
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke._run_tracked", side_effect=fake_run_tracked),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            assert started.wait(timeout=10), "agent subprocess never started"
            time.sleep(0.2)  # let the child settle inside communicate()

            t0 = time.monotonic()
            pool.shutdown()
            _handle, result = completion_q.get(timeout=10)
            elapsed = time.monotonic() - t0

        # Reaped well under the 60s sleep (SIGTERM, not timeout).
        assert elapsed < 15, f"shutdown did not reap the subprocess fast ({elapsed:.1f}s)"
        assert result.ok is False
        assert result.interrupted is True
