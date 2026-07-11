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
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from hephaestus.agents.runtime import resolve_agent, run_agent_session
from hephaestus.automation import claude_invoke, git_utils
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
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.resilience import (
    CircuitBreakerOpenError,
    resilient_call,
)
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.helpers import get_repo_root

logger = logging.getLogger(__name__)

_TAIL = 4000  # chars of stdout/stderr retained in a JobResult
_ERR_MAX = 500  # chars of error detail retained in a JobResult
_GIT_LOCK_WAIT_POLL_S = 0.1


def _repo_lock_path(repo: str, lock_dir: Path | None = None) -> Path:
    """Cross-process advisory lock file for *repo*.

    Anchored at ``<repo_root>/<DEFAULT_STATE_DIR>/locks`` (the shared
    automation state dir) rather than the bare CWD, so every process that
    operates on this checkout resolves the SAME sentinel file regardless of
    which subdirectory it was launched from. ``file_lock`` creates the parent
    directory on first acquisition.

    Args:
        repo: Repository slug (``owner/name``); slashes are flattened.
        lock_dir: Override directory for the sentinel files (tests inject a
            temp dir here).

    Returns:
        Path of the sentinel lock file for *repo*.

    """
    if lock_dir is None:
        lock_dir = get_repo_root() / DEFAULT_STATE_DIR / "locks"
    return lock_dir / f"git-{repo.replace('/', '_')}.lock"


@dataclass
class _RepoLockEntry:
    """In-process git lock plus active/waiting user count."""

    lock: threading.Lock
    users: int = 0


class _GitLockTimeoutError(TimeoutError):
    """Raised when a Git job cannot acquire its cross-process repo lock in time."""


class _GitLockInterruptedError(RuntimeError):
    """Raised when shutdown interrupts a Git job while it waits for the repo lock."""


@contextmanager
def _interruptible_file_lock(
    path: Path,
    *,
    shutdown: threading.Event,
    timeout_s: float,
) -> Iterator[None]:
    """Acquire ``path`` without an unbounded blocking flock wait."""
    deadline = time.monotonic() + max(timeout_s, 0.0)

    while True:
        if shutdown.is_set():
            raise _GitLockInterruptedError

        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(path, blocking=False))
            except LockUnavailableError as exc:
                now = time.monotonic()
                if now >= deadline:
                    raise _GitLockTimeoutError from exc

                wait_s = min(_GIT_LOCK_WAIT_POLL_S, deadline - now)
                if shutdown.wait(timeout=wait_s):
                    raise _GitLockInterruptedError from exc
                continue

            if shutdown.is_set():
                raise _GitLockInterruptedError
            yield
            return


class WorkerPool:
    """Thread pool executor for submitting and tracking frozen jobs.

    Jobs are executed via :meth:`submit`; a future callback drains results to
    the completion queue. Workers never mutate ``WorkItem`` objects or stage
    queues. Agent jobs do build prompts in the worker; prompt builders may do
    read-only GitHub fetches, while durable GitHub mutations remain coordinator
    responsibilities.

    Completion contract: every non-cancelled :meth:`submit` produces EXACTLY
    ONE ``(handle, result)`` tuple on the completion queue — normal job
    failures are converted to error results in :meth:`_run`, and any exception
    that still escapes the future is converted to a ``worker_crash`` result in
    :meth:`_on_future_done`. Only futures cancelled before starting (via
    :meth:`shutdown`'s ``cancel_futures=True``) emit no completion; the
    coordinator synthesizes those.
    """

    def __init__(
        self,
        size: int,
        shutdown: threading.Event,
        completion_q: CompletionQueue,
        lock_dir: Path | None = None,
    ) -> None:
        """Initialize the pool.

        Args:
            size: Number of worker threads.
            shutdown: Event that signals pool shutdown; workers check it before
                starting and after completing each job.
            completion_q: Queue to which ``(JobHandle, JobResult)`` tuples are
                sent when jobs complete.
            lock_dir: Optional override for the cross-process git lock
                directory (tests inject a temp dir; defaults to the shared
                automation state dir — see :func:`_repo_lock_path`).

        """
        self._executor = ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix="hephaestus-pipeline-worker",
        )
        self._shutdown = shutdown
        self._completion_q = completion_q
        self._repo_locks: dict[str, _RepoLockEntry] = {}
        self._repo_locks_guard = threading.Lock()
        self._lock_dir = lock_dir

    @contextmanager
    def _repo_lock(self, repo: str) -> Iterator[None]:
        """Acquire a per-repo git lock and evict its cache entry when idle."""
        with self._repo_locks_guard:
            entry = self._repo_locks.get(repo)
            if entry is None:
                entry = _RepoLockEntry(threading.Lock())
                self._repo_locks[repo] = entry
            entry.users += 1

        try:
            with entry.lock:
                yield
        finally:
            with self._repo_locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._repo_locks.get(repo) is entry:
                    self._repo_locks.pop(repo, None)

    def submit(
        self,
        job: AgentJob | BuildTestJob | GitJob,
        on_done_state: str | StageName,
        *,
        claim_key: str = "",
        claim_stage: str = "",
    ) -> JobHandle:
        """Submit a job for execution.

        Args:
            job: Immutable frozen job spec.
            on_done_state: Pipeline stage the item should transition to when
                this job completes.
            claim_key: Optional coordinator item key for worker-claim logging.
            claim_stage: Optional stage queue name for worker-claim logging.

        Returns:
            JobHandle carrying the submitted job and target state; the
            coordinator uses the handle to route the completion back to the
            work item.

        """
        handle = JobHandle(job=job, on_done_state=on_done_state)
        future = self._executor.submit(self._run, job, claim_key, claim_stage)
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
        synthesizes one later). For every OTHER outcome a completion MUST be
        queued: ``_run`` already converts normal job failures into error
        results, and anything that still escapes ``future.result()`` -- any
        ``Exception`` plus the process-control escapes ``KeyboardInterrupt``,
        ``SystemExit``, and ``GeneratorExit`` -- is converted here to a
        ``worker_crash`` result so a non-cancelled submit never silently loses
        its completion. Process-control escapes are logged without traceback at
        warning/info severity; genuine ``Exception`` crashes keep
        ``logger.exception``. ``KeyboardInterrupt`` is intentionally NOT
        re-raised after queuing: this callback runs on an executor worker
        thread where a re-raise would only print a traceback, not stop the
        process.
        """
        if future.cancelled():
            return  # cancel_futures synthesizes NO completion
        worker_id = threading.current_thread().name
        try:
            result = future.result()
        except KeyboardInterrupt as exc:
            logger.warning("Worker future interrupted; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        except (SystemExit, GeneratorExit) as exc:
            logger.info("Worker future exited during shutdown; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        except Exception as exc:
            logger.exception("Worker future raised; converting to worker_crash result")
            result = JobResult(
                ok=False,
                error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                worker_id=worker_id,
            )
        self._completion_q.put((handle, result))

    def _run(
        self,
        job: AgentJob | BuildTestJob | GitJob,
        claim_key: str = "",
        claim_stage: str = "",
    ) -> JobResult:
        """Execute a job and return its result.

        Converts normal job exceptions and process-control escapes into
        ``JobResult`` values so a single job failure does not crash the worker
        thread. After every job, post-checks the shutdown event and marks
        interrupted=True if it was set (SIGINT to the process group makes
        children return normally; the interrupt flag prevents misreading a
        killed job as success).
        """
        start = time.monotonic()
        worker_id = threading.current_thread().name
        logger.info(
            "worker_claim: worker_id=%s item=%s stage=%s job=%s repo=%s descr=%s",
            worker_id,
            claim_key or "-",
            claim_stage or "-",
            type(job).__name__,
            getattr(job, "repo", ""),
            getattr(job, "descr", ""),
        )

        # Pre-check: do not start a queued job if shutdown is set.
        if self._shutdown.is_set():
            result = JobResult(
                ok=False,
                interrupted=True,
                error="interrupted_before_start",
            )
        else:
            try:
                if isinstance(job, AgentJob):
                    result = self._run_agent(job)
                elif isinstance(job, BuildTestJob):
                    result = self._run_build_test(job)
                elif isinstance(job, GitJob):
                    result = self._run_git(job)
                else:
                    raise TypeError(f"unknown job type {type(job)}")
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                # Preserve the executing worker identity for process-control
                # escapes. The future callback may run outside the worker
                # thread if the future completed before callback registration.
                logger.info(
                    "Job %s exited via %s, returning worker_crash result",
                    job,
                    type(exc).__name__,
                )
                result = JobResult(
                    ok=False,
                    error=f"worker_crash: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
                )
            except Exception as exc:
                # Convert job execution failures into a JobResult so the callback
                # never re-raises into its thread.
                logger.exception("Job %s raised, returning error result", job)
                result = JobResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc!s}"[:_ERR_MAX],
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
            worker_id=worker_id,
        )

    def _run_agent(self, job: AgentJob) -> JobResult:
        """Run an agent job (Claude or other runtime).

        Retry tradeoff: the whole agent invocation is wrapped in
        :func:`resilient_call`, so a *transient* failure (network reset, gh
        flake) re-runs the ENTIRE agent session — expensive, and the retried
        session may redo work the failed one partially completed. We accept
        that because agent invocations are idempotent-by-design at the
        workflow level (plan/review comments upsert; implementation re-runs
        converge on the same branch), and the alternative — no retry — turns
        every blip into a failed pipeline stage. Non-transient errors (rc!=0
        with non-transient stderr, timeouts) are NOT retried; they surface
        immediately as error results.

        Unexpected Exception subclasses from agent resolution, prompt
        construction, and the resilience wrapper are classified in this method
        for symmetry with the specific agent failures below. Process-control
        escapes are converted by :meth:`_run` so the returned result preserves
        the executing worker identity.
        """
        try:
            agent = resolve_agent(job.agent)
            is_claude = agent == "claude"
            session_agent = job.session_agent or job.agent
            prompt = job.prompt_builder(**job.prompt_kwargs)

            def _invoke() -> str:
                if is_claude:
                    stdout, _ = claude_invoke.invoke_claude_with_session(
                        repo=job.repo,
                        issue=job.issue,
                        agent=session_agent,
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
                circuit_breaker_name=f"agent:{agent}",
                retry_predicate=lambda _exc: not self._shutdown.is_set(),
            )

            value = None
            if job.parse is not None:
                try:
                    value = job.parse(stdout)
                except Exception as exc:
                    logger.exception("Parse callable raised for agent job")
                    return JobResult(
                        ok=False,
                        error=f"parse failed: {type(exc).__name__}: {exc!s}"[:_ERR_MAX],
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
        except Exception as exc:
            logger.exception("Agent job raised, returning error result")
            return JobResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc!s}"[:_ERR_MAX],
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
        """Run a git job (serialized per-repo, in-process AND cross-process).

        Lock layering (documented invariant): the in-process
        ``threading.Lock`` is OUTER and the cross-process
        :func:`~hephaestus.utils.file_lock.file_lock` is INNER. The thread
        lock elects a single thread per process first, so at most one thread
        per process ever opens/holds the flock descriptor — sidestepping
        flock's confusing same-process semantics (multiple fds on one file
        within one process can still exclude each other) and keeping the
        blocking flock wait to one thread. Both locks are held for the entire
        operation because worktrees share ``.git``.
        """
        lock_path = _repo_lock_path(job.repo, self._lock_dir)
        try:
            with (
                self._repo_lock(job.repo),
                _interruptible_file_lock(
                    lock_path,
                    shutdown=self._shutdown,
                    timeout_s=job.timeout_s,
                ),
            ):
                return self._dispatch_git_op(job)
        except _GitLockTimeoutError:
            return JobResult(ok=False, error="lock_timeout")
        except _GitLockInterruptedError:
            return JobResult(
                ok=False,
                interrupted=True,
                error="interrupted_waiting_for_git_lock",
            )
        except subprocess.TimeoutExpired as exc:
            return JobResult(
                ok=False,
                error="timeout",
                stdout_tail=str(exc.stdout or "")[-_TAIL:],
                stderr_tail=str(exc.stderr or "")[-_TAIL:],
            )
        except subprocess.CalledProcessError as exc:
            return JobResult(
                ok=False,
                error=f"rc={exc.returncode}",
                stdout_tail=(exc.stdout or "")[-_TAIL:],
                stderr_tail=(exc.stderr or "")[-_TAIL:],
            )

    def _dispatch_git_op(self, job: GitJob) -> JobResult:
        """Dispatch a git operation to its handler.

        ``job.timeout_s`` is threaded into every git helper call so network
        operations cannot outlive the job budget while holding repo locks.
        """
        if job.op == "create_worktree":
            return self._git_create_worktree(job)

        elif job.op == "remove_worktree":
            return self._git_remove_worktree(job)

        elif job.op == "rebase":
            result = git_utils.rebase_worktree_onto(**job.kwargs, timeout=job.timeout_s)
            return JobResult(ok=result, value=result)

        elif job.op == "push":
            git_utils.push_current_branch_with_lease_on_divergence(
                **job.kwargs,
                timeout=job.timeout_s,
            )
            return JobResult(ok=True)

        elif job.op == "commit_push":
            return self._git_commit_push(job)

        elif job.op == "clone":
            # gh repo clone <repo> <dest>
            repo = str(job.kwargs.get("repo") or "")
            dest = str(job.kwargs.get("dest") or "")
            if not repo or not dest:
                return JobResult(
                    ok=False,
                    error="clone requires non-empty 'repo' and 'dest' kwargs",
                )
            git_utils.run(["gh", "repo", "clone", repo, dest], cwd=None, timeout=job.timeout_s)
            return JobResult(ok=True)

        else:
            # Should be impossible due to GitJob.__post_init__ validation
            return JobResult(ok=False, error=f"unknown op {job.op!r}")

    def _git_create_worktree(self, job: GitJob) -> JobResult:
        """Create a worktree and optionally sync an adopted PR branch."""
        manager = WorktreeManager()
        kwargs = dict(job.kwargs)
        sync_to_remote = bool(kwargs.pop("sync_to_remote", False))
        pr_number = kwargs.pop("pr_number", None)
        created = manager.create_worktree(**kwargs, timeout=job.timeout_s)
        if created is None:
            return JobResult(ok=True)
        worktree_path = Path(created)
        branch_name = str(kwargs.get("branch_name") or "")
        if not sync_to_remote:
            return JobResult(ok=True, value=str(worktree_path))

        dirty = not git_utils.is_clean_working_tree(worktree_path, timeout=job.timeout_s)
        status = ""
        diff = ""
        if dirty:
            status_result = git_utils.run(
                ["git", "status", "--short"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=job.timeout_s,
            )
            diff_result = git_utils.run(
                ["git", "diff"],
                cwd=worktree_path,
                capture_output=True,
                check=False,
                timeout=job.timeout_s,
            )
            status = status_result.stdout or ""
            diff = diff_result.stdout or ""
        elif branch_name:
            git_utils.sync_worktree_to_remote_branch(
                worktree_path,
                branch_name,
                pr_number=int(pr_number) if pr_number is not None else None,
                timeout=job.timeout_s,
            )
        return JobResult(
            ok=True,
            value={
                "path": str(worktree_path),
                "dirty": dirty,
                "status": status,
                "diff": diff,
            },
        )

    def _git_remove_worktree(self, job: GitJob) -> JobResult:
        """Remove a worktree by known path, or fall back to manager state."""
        if job.kwargs.get("worktree_path"):
            worktree_path = Path(str(job.kwargs["worktree_path"]))
            repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
            cmd = ["git", "worktree", "remove", str(worktree_path)]
            if job.kwargs.get("force"):
                cmd.append("--force")
            git_utils.run(cmd, cwd=repo_root, timeout=job.timeout_s)
            git_utils.run(
                ["git", "worktree", "prune"],
                cwd=repo_root,
                check=False,
                timeout=job.timeout_s,
            )
            return JobResult(ok=True)
        manager = WorktreeManager()
        manager.remove_worktree(**job.kwargs, timeout=job.timeout_s)
        return JobResult(ok=True)

    def _git_commit_push(self, job: GitJob) -> JobResult:
        """Commit pending changes in a worktree, then push its branch.

        Only the keys ``commit_if_changes`` actually accepts are forwarded —
        passing ``job.kwargs`` wholesale would crash on routing-only keys such
        as ``branch``. A missing ``worktree_path`` (or ``issue_number``) is a
        hard error result, never a silent skip: the coordinator submitted this
        op expecting a push to happen.
        """
        worktree_path = job.kwargs.get("worktree_path")
        issue_number = job.kwargs.get("issue_number")
        if not worktree_path or issue_number is None:
            return JobResult(
                ok=False,
                error="commit_push requires non-empty 'worktree_path' and 'issue_number' kwargs",
            )
        # NOTE: commit_if_changes returns False BOTH for "worktree clean,
        # nothing to commit" AND for "commit attempted but failed" (it logs
        # and swallows the RuntimeError). Do not push in either case; stages
        # consume value=False as the no-real-commit path.
        changed = git_utils.commit_if_changes(
            int(issue_number),
            Path(worktree_path),
            str(job.kwargs.get("agent", "claude")),
            allowed_paths=job.kwargs.get("allowed_paths"),
            timeout=job.timeout_s,
        )
        if not changed:
            return JobResult(ok=True, value=False)
        git_utils.push_branch(
            str(job.kwargs.get("branch", "HEAD")),
            Path(worktree_path),
            timeout=job.timeout_s,
        )
        return JobResult(ok=True, value=changed)
