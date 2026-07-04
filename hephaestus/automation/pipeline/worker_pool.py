"""Worker pool: the only place agent, build/test, and git/network work runs.

The coordinator submits frozen jobs and drains ``(handle, result)`` tuples from
the completion queue. Workers never touch WorkItems or stage queues and never
perform GitHub API mutations (enforced by test_pipeline_architecture.py).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from hephaestus.agents.runtime import resolve_agent, run_agent_session
from hephaestus.automation import claude_invoke, git_utils
from hephaestus.automation._review_utils import DEFAULT_STATE_DIR, ensure_state_dir
from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.resilience import (
    CircuitBreakerOpenError,
    resilient_call,
)

logger = logging.getLogger(__name__)

_TAIL = 4000  # chars of stdout/stderr retained in a JobResult


def _repo_lock_path(repo: str) -> Path:
    """Cross-process advisory lock file for *repo*, under the automation state dir."""
    state_dir = ensure_state_dir(Path.cwd(), DEFAULT_STATE_DIR)
    locks_dir = state_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"git-{repo.replace('/', '_')}.lock"


class WorkerPool:
    """Thread pool executor for submitting and tracking frozen jobs.

    Jobs are executed via :meth:`submit`; a future callback drains results to
    the completion queue. Workers never look up PR numbers, build prompts, or
    call the GitHub API — those are coordinator responsibilities.
    """

    def __init__(self, size: int, shutdown: threading.Event, completion_q: CompletionQueue) -> None:
        """Initialize the pool.

        Args:
            size: Number of worker threads.
            shutdown: Event that signals pool shutdown; workers check it before
                starting and after completing each job.
            completion_q: Queue to which ``(JobHandle, JobResult)`` tuples are
                sent when jobs complete.

        """
        self._executor = ThreadPoolExecutor(max_workers=size)
        self._shutdown = shutdown
        self._completion_q = completion_q
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()

    def _repo_lock(self, repo: str) -> threading.Lock:
        """Get or create a per-repo lock (held during git operations)."""
        with self._repo_locks_guard:
            return self._repo_locks.setdefault(repo, threading.Lock())

    def submit(self, job: AgentJob | BuildTestJob | GitJob, on_done_state: StageName) -> JobHandle:
        """Submit a job for execution.

        Args:
            job: Immutable frozen job spec.
            on_done_state: Pipeline stage the item should transition to when
                this job completes.

        Returns:
            JobHandle carrying the submitted job and target state; the
            coordinator uses the handle to route the completion back to the
            work item.

        """
        handle = JobHandle(job=job, on_done_state=on_done_state)
        future = self._executor.submit(self._run, job)
        future.add_done_callback(lambda f: self._on_future_done(handle, f))
        return handle

    def shutdown(self) -> None:
        """Shut down the pool.

        Sets the shutdown event and cancels pending futures. Running tasks
        finish but post-check them for interruption.
        """
        self._shutdown.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _on_future_done(self, handle: JobHandle, future: Future[JobResult]) -> None:
        """Drain result to completion queue when a job future completes.

        If the future was cancelled, do not emit a completion (the coordinator
        synthesizes one later).
        """
        if future.cancelled():
            return  # cancel_futures synthesizes NO completion
        try:
            result = future.result()
            self._completion_q.put((handle, result))
        except Exception:
            # _run catches all job failures and returns them in a JobResult,
            # so future.result() should rarely raise. This handles only
            # unexpected runtime errors from the callback machinery.
            logger.exception("Future result() raised, discarding")

    def _run(self, job: AgentJob | BuildTestJob | GitJob) -> JobResult:
        """Execute a job and return its result.

        Catches all exceptions so a single job failure does not crash the
        worker thread. After every job, post-checks the shutdown event and
        marks interrupted=True if it was set (SIGINT to the process group
        makes children return normally; the interrupt flag prevents misreading
        a killed job as success).
        """
        start = time.monotonic()

        # Pre-check: do not start a queued job if shutdown is set.
        if self._shutdown.is_set():
            return JobResult(ok=False, interrupted=True, error="interrupted_before_start")

        try:
            if isinstance(job, AgentJob):
                result = self._run_agent(job)
            elif isinstance(job, BuildTestJob):
                result = self._run_build_test(job)
            elif isinstance(job, GitJob):
                result = self._run_git(job)
            else:
                raise TypeError(f"unknown job type {type(job)}")
        except Exception as exc:
            # Convert job execution failures into a JobResult so the callback
            # never re-raises into its thread. This catches normal runtime errors
            # but allows process-control exceptions to propagate normally.
            logger.exception("Job %s raised, returning error result", job)
            result = JobResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc!s}"[:500],
            )

        # Mandatory post-check: SIGINT to the process group makes subprocess
        # children return "normally" (rc=0 or some other code), so an
        # interrupted job must never read as success.
        if self._shutdown.is_set():
            result = replace(result, interrupted=True, ok=False)

        return replace(
            result,
            duration_s=time.monotonic() - start,
            stdout_tail=result.stdout_tail[-_TAIL:] if result.stdout_tail else "",
            stderr_tail=result.stderr_tail[-_TAIL:] if result.stderr_tail else "",
        )

    def _run_agent(self, job: AgentJob) -> JobResult:
        """Run an agent job (Claude or other runtime)."""
        try:
            agent = resolve_agent(job.agent)
            is_claude = agent == "claude"
            prompt = job.prompt_builder(**job.prompt_kwargs)

            def _invoke() -> str:
                if is_claude:
                    stdout, _ = claude_invoke.invoke_claude_with_session(
                        repo=job.repo,
                        issue=job.issue,
                        agent=job.agent,
                        prompt=prompt,
                        model=job.model,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        output_format=job.output_format,
                    )
                    return stdout
                else:
                    agent_result = run_agent_session(
                        agent=agent,
                        prompt=prompt,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        model=job.model,
                        sandbox="workspace-write",
                        approval="never",
                    )
                    return agent_result.stdout or ""

            stdout = resilient_call(
                _invoke,
                circuit_breaker_name=f"agent:{agent}:{job.model}",
            )

            value = None
            if job.parse is not None:
                try:
                    value = job.parse(stdout)
                except Exception as exc:
                    logger.exception("Parse callable raised for agent job")
                    return JobResult(
                        ok=False,
                        error=f"parse failed: {type(exc).__name__}: {exc!s}"[:500],
                        stdout_tail=stdout[-_TAIL:],
                    )

            return JobResult(
                ok=True,
                value=value if value is not None else stdout,
                stdout_tail=stdout[-_TAIL:],
            )

        except CircuitBreakerOpenError:
            return JobResult(ok=False, error="circuit_open")
        except subprocess.TimeoutExpired:
            return JobResult(ok=False, error="timeout")
        except subprocess.CalledProcessError as exc:
            return JobResult(
                ok=False,
                error=f"rc={exc.returncode}",
                stdout_tail=(exc.stdout or "")[-_TAIL:],
                stderr_tail=(exc.stderr or "")[-_TAIL:],
            )

    def _run_build_test(self, job: BuildTestJob) -> JobResult:
        """Run a build/test job (subprocess with argv)."""
        try:
            result = subprocess.run(
                job.argv,
                cwd=str(job.cwd),
                capture_output=True,
                text=True,
                timeout=job.timeout_s,
                check=False,  # we inspect rc below
            )
            return JobResult(
                ok=result.returncode == 0,
                value=None,
                stdout_tail=result.stdout[-_TAIL:],
                stderr_tail=result.stderr[-_TAIL:],
                error=None if result.returncode == 0 else f"rc={result.returncode}",
            )
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )

    def _run_git(self, job: GitJob) -> JobResult:
        """Run a git job (serialized per-repo via a shared lock).

        The per-repo lock is held for the entire operation to ensure
        worktree-manager calls (which share .git) do not race.
        """
        lock = self._repo_lock(job.repo)
        try:
            with lock:
                return self._dispatch_git_op(job)
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )

    def _dispatch_git_op(self, job: GitJob) -> JobResult:
        """Dispatch a git operation to its handler."""
        if job.op == "create_worktree":
            manager = WorktreeManager()
            manager.create_worktree(**job.kwargs)
            return JobResult(ok=True)

        elif job.op == "remove_worktree":
            manager = WorktreeManager()
            manager.remove_worktree(**job.kwargs)
            return JobResult(ok=True)

        elif job.op == "rebase":
            result = git_utils.rebase_worktree_onto(**job.kwargs)
            return JobResult(ok=result, value=result)

        elif job.op == "push":
            git_utils.push_current_branch_with_lease_on_divergence(**job.kwargs)
            return JobResult(ok=True)

        elif job.op == "commit_push":
            # commit_if_changes returns True if changes were committed
            git_utils.commit_if_changes(**job.kwargs)
            # extract worktree_path for push
            worktree_path = job.kwargs.get("worktree_path")
            branch = job.kwargs.get("branch", "HEAD")
            if worktree_path:
                git_utils.push_branch(branch, worktree_path)
            return JobResult(ok=True)

        elif job.op == "clone":
            # gh repo clone <repo> <dest>
            repo = job.kwargs.get("repo") or ""
            dest = job.kwargs.get("dest") or ""
            git_utils.run(["gh", "repo", "clone", repo, dest], cwd=None)
            return JobResult(ok=True)

        else:
            # Should be impossible due to GitJob.__post_init__ validation
            return JobResult(ok=False, error=f"unknown op {job.op!r}")
