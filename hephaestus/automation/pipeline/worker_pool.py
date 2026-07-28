"""Worker pool: the only place agent, build/test, git, and session work runs.

The coordinator submits frozen jobs and drains ``(handle, result)`` tuples from
the completion queue. Workers never touch WorkItems or stage queues and never
perform GitHub API mutations (enforced by test_pipeline_architecture.py).
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeGuard

from hephaestus.agents.runtime import resolve_agent, resume_agent_session, run_agent_session
from hephaestus.automation import claude_invoke, git_utils, subprocess_registry
from hephaestus.automation.learn import compact_agent_session
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.jobs import (
    WORKTREE_MATERIALIZED_KEY,
    AgentJob,
    BuildTestJob,
    CompactJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.tool_scopes import (
    DEFAULT_TOOL_SCOPE,
    ToolScope,
    tool_scope_for,
)
from hephaestus.automation.worktree_manager import (
    BRANCH_WORKTREE_OWNED,
    BranchWorktreeOwnedError,
    WorktreeManager,
)
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
_FETCH_ENV_BLOCKLIST = frozenset(
    {
        "GIT_ASKPASS",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_NO_VERIFY",
        "GIT_WORK_TREE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SSH_ASKPASS",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)

# ``gh`` must not be discovered through a caller-controlled ``PATH``: the
# checkout synchronizer executes it as the GitHub credential helper.  These
# are the system and package-manager locations we support for the automation
# host.  Resolving candidates also rejects a symlink that escapes its trusted
# installation root.
_TRUSTED_GH_CANDIDATES = (
    Path("/opt/homebrew/bin/gh"),
    Path("/usr/local/bin/gh"),
    Path("/usr/bin/gh"),
)
_TRUSTED_GH_ROOTS = (Path("/opt/homebrew"), Path("/usr/local"), Path("/usr"))


def _is_full_commit_sha(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is a full SHA-1 or SHA-256 commit id."""
    return bool(
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def _controlled_git_env() -> dict[str, str]:
    """Return an environment that cannot redirect or extend Git execution."""
    env = os.environ.copy()
    for key in _FETCH_ENV_BLOCKLIST:
        env.pop(key, None)
    for key in tuple(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["PATH"] = os.defpath
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _trusted_executable(name: str, *, path: str | None = None) -> str | None:
    """Resolve a command to an absolute path before entering a controlled env."""
    executable = shutil.which(name, path=path)
    return str(Path(executable).resolve()) if executable is not None else None


def _trusted_gh_executable() -> str | None:
    """Return a supported absolute ``gh`` binary without consulting ``PATH``."""
    for candidate in _TRUSTED_GH_CANDIDATES:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        if any(resolved.is_relative_to(root) for root in _TRUSTED_GH_ROOTS):
            return str(resolved)
    return None


def _unsafe_local_git_config_key(config: str) -> str | None:
    """Return an unsafe repository/worktree config key, if *config* contains one."""
    for entry in config.split("\0"):
        if not entry:
            continue
        key, _separator, _value = entry.partition("\n")
        normalized = key.lower()
        if normalized in {
            "core.askpass",
            "core.attributesfile",
            "core.fsmonitor",
            "core.gitproxy",
            "core.sshcommand",
            "core.worktree",
        }:
            return key
        if normalized == "credential.helper" or (
            normalized.startswith("credential.") and normalized.endswith(".helper")
        ):
            return key
        if normalized.startswith("remote.") and normalized.rsplit(".", 1)[-1] in {
            "proxy",
            "proxyauthmethod",
            "uploadpack",
        }:
            return key
        if normalized in {"fetch.recursesubmodules", "submodule.recurse"}:
            return key
        if normalized.startswith(("include.", "includeif.")):
            return key
        if normalized.startswith("filter.") and normalized.rsplit(".", 1)[-1] in {
            "clean",
            "process",
            "smudge",
        }:
            return key
        if normalized.startswith("merge.") and normalized.endswith(".driver"):
            return key
        # A checkout-specific URL rewrite can transform the validated literal
        # GitHub origin when it is later passed to ``git fetch``.  Any local
        # HTTP configuration can similarly proxy traffic or override TLS
        # verification/CA trust, including URL-scoped variants.
        if normalized.startswith(("http.", "url.")):
            return key
    return None


def _checkout_preflight_error(checkout: Path, timeout_s: int) -> str | None:
    """Return a reusable-checkout metadata safety failure before synchronization."""
    if not (checkout / ".git").exists():
        return None
    unsafe_config = _unsafe_local_git_config_key(
        git_utils.run(
            ["git", "config", "--null", "--list"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout
    )
    if unsafe_config is not None:
        return "checkout has unsafe local Git configuration"
    graft_value = git_utils.run(
        ["git", "rev-parse", "--git-path", "info/grafts"],
        cwd=checkout,
        timeout=timeout_s,
        env=_controlled_git_env(),
    ).stdout.strip()
    if not graft_value:
        return None
    graft_path = Path(graft_value)
    if not graft_path.is_absolute():
        graft_path = checkout / graft_path
    if graft_path.is_file():
        return "checkout has unsafe legacy Git grafts"
    return None


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
        self._completion_wakeup: threading.Event | None = None
        self._completion_saturation: threading.Event | None = None
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

    def set_completion_notifiers(
        self,
        *,
        wakeup: threading.Event,
        saturation: threading.Event,
    ) -> None:
        """Bind coordinator-owned completion wake and saturation latches.

        The coordinator creates these latches before it submits work.  A
        successful non-blocking completion write wakes its event loop; an
        impossible full completion queue instead latches a fatal coordinator
        fault.  The callback deliberately has no overflow buffer: retaining
        the owning item in the coordinator's in-flight registry makes it
        resumable during that fatal teardown.
        """
        self._completion_wakeup = wakeup
        self._completion_saturation = saturation

    def submit(
        self,
        job: AgentJob | BuildTestJob | GitJob | CompactJob,
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
        # Capture the caller's ContextVar snapshot so worker-thread prompt
        # builders see the same CLI-selected prompt catalog as the coordinator.
        context = copy_context()
        future = self._executor.submit(context.run, self._run, job, claim_key, claim_stage)
        future.add_done_callback(lambda f: self._on_future_done(handle, f))
        return handle

    def shutdown(self, *, mark_interrupted: bool = True) -> None:
        """Shut down the pool.

        When ``mark_interrupted`` is true, sets the shutdown event before
        cancelling pending futures and SIGTERMing every in-flight agent process
        group. Coordinators pass false for ordinary ``finally`` cleanup so
        releasing pool resources cannot reclassify a completed run as a signal
        interruption. ``executor.shutdown(cancel_futures=True)`` only cancels
        UN-STARTED futures; a job already blocked in a ``claude`` subprocess
        would keep running and pin its non-daemon worker thread (holding the
        interpreter open at exit — the #2059 leak). Terminating tracked process
        groups frees those workers promptly.
        """
        if mark_interrupted:
            self._shutdown.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        subprocess_registry.terminate_all()

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
        try:
            self._completion_q.put_nowait((handle, result))
        except queue_mod.Full:
            # With the coordinator's global C-in-flight invariant, a C-sized
            # completion queue cannot fill before a worker has a slot to
            # publish.  Treat a violation as an internal fault rather than
            # blocking this callback forever.  There is intentionally no
            # unbounded spill structure: finalization retains the in-flight
            # WorkItem as RESUMABLE for the next run.
            logger.error("completion queue saturated; refusing to block worker callback")
            if self._completion_saturation is not None:
                self._completion_saturation.set()
            if self._completion_wakeup is not None:
                self._completion_wakeup.set()
            return

        if self._completion_wakeup is not None:
            self._completion_wakeup.set()

    def _run(
        self,
        job: AgentJob | BuildTestJob | GitJob | CompactJob,
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
                elif isinstance(job, CompactJob):
                    result = self._run_compact(job)
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

            def _invoke() -> tuple[str, str | None]:
                if is_claude:
                    # Scope priority: an explicit per-job grant (a stage that
                    # knows its exact needs, e.g. pr_review) wins; a read-only
                    # sandbox without one clamps to the fail-closed default;
                    # everything else resolves by session-agent role.
                    if job.allowed_tools:
                        scope = ToolScope(job.allowed_tools)
                    elif job.sandbox == "read-only":
                        scope = DEFAULT_TOOL_SCOPE
                    else:
                        scope = tool_scope_for(session_agent)
                    stdout, _ = claude_invoke.invoke_claude_with_session(
                        repo=job.repo,
                        issue=job.issue,
                        agent=session_agent,
                        prompt=prompt,
                        model=job.model,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        output_format=job.output_format,
                        allowed_tools=scope.allowed_tools,
                        permission_mode=scope.permission_mode,
                    )
                    return stdout, None
                if job.resume_session_id:
                    agent_result = resume_agent_session(
                        agent=agent,
                        session_id=job.resume_session_id,
                        prompt=prompt,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                    )
                else:
                    agent_result = run_agent_session(
                        agent=agent,
                        prompt=prompt,
                        cwd=job.cwd,
                        timeout=job.timeout_s,
                        model=job.model,
                        sandbox=job.sandbox,
                        approval="never",
                        process_tracker=subprocess_registry.track_process_group,
                    )
                # A resumed command may not repeat the session-start event;
                # retain the known id in that case.
                return agent_result.stdout or "", agent_result.session_id or job.resume_session_id

            stdout, session_id = resilient_call(
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
                session_id=session_id,
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

    @staticmethod
    def _run_compact(job: CompactJob) -> JobResult:
        """Compact an agent session without making compaction a hard gate."""
        compacted = compact_agent_session(
            repo=job.repo,
            issue=job.issue,
            provider=job.agent,
            session_agent=job.session_agent,
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            session_id=job.session_id,
            sandbox=job.sandbox,
        )
        # ``compact_agent_session`` intentionally swallows expected failures; a
        # missing or uncompactable transcript must not stall a review cycle.
        return JobResult(ok=True, value=compacted)

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
        except BranchWorktreeOwnedError as exc:
            return JobResult(
                ok=False,
                error=BRANCH_WORKTREE_OWNED,
                value={"branch": exc.branch, "owner_path": str(exc.owner_path)},
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

        elif job.op == "verify_pr_review_checkout":
            return self._git_verify_pr_review_checkout(job)

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

        elif job.op == "release_branch_reservation":
            branch_name = str(job.kwargs.get("branch") or "")
            base_sha = job.kwargs.get("base_sha")
            repo_root_value = job.kwargs.get("repo_root")
            repo_root = Path(str(repo_root_value)) if repo_root_value else None
            if (
                not branch_name
                or not _is_full_commit_sha(base_sha)
                or repo_root is None
                or not repo_root.is_dir()
            ):
                return JobResult(
                    ok=False,
                    error="release_branch_reservation requires branch, base_sha, and repo_root",
                )
            released = git_utils.delete_reserved_branch_if_unchanged(
                branch_name,
                base_sha,
                repo_root,
                timeout=job.timeout_s,
            )
            return JobResult(ok=True, value=released)

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

        elif job.op == "sync_checkout":
            return self._git_sync_checkout(job)

        else:
            # Should be impossible due to GitJob.__post_init__ validation
            return JobResult(ok=False, error=f"unknown op {job.op!r}")

    def _git_sync_checkout(self, job: GitJob) -> JobResult:
        """Validate and fast-forward a reusable checkout without discarding local work."""
        expected_repo = str(job.kwargs.get("repo") or "")
        dest = str(job.kwargs.get("dest") or "")
        if not expected_repo or not dest:
            return JobResult(
                ok=False,
                error="sync_checkout requires non-empty 'repo' and 'dest' kwargs",
            )

        checkout = Path(dest)
        if not checkout.is_dir():
            return JobResult(ok=False, error=f"checkout does not exist: {checkout}")
        # This read-only security preflight must run before acquiring a lock
        # below: creating a lock file can otherwise create ``.git`` in a
        # malformed directory and change how the preflight probes it.
        if preflight_error := _checkout_preflight_error(checkout, job.timeout_s):
            return JobResult(ok=False, error=preflight_error)

        metadata_lock = WorktreeManager.git_metadata_lock_path(checkout)
        with _interruptible_file_lock(
            metadata_lock,
            shutdown=self._shutdown,
            timeout_s=job.timeout_s,
        ):
            return self._sync_checkout_locked(
                checkout=checkout,
                expected_repo=expected_repo,
                timeout_s=job.timeout_s,
            )

    def _sync_checkout_locked(
        self,
        *,
        checkout: Path,
        expected_repo: str,
        timeout_s: int,
    ) -> JobResult:
        """Validate and synchronize one checkout while its metadata lock is held."""
        origin = git_utils.run(
            ["git", "remote", "get-url", "origin"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.strip()
        normalized_origin = origin.rstrip("/").removesuffix(".git")
        expected_origins = {
            f"https://github.com/{expected_repo}",
            f"ssh://git@github.com/{expected_repo}",
            f"git@github.com:{expected_repo}",
        }
        if normalized_origin not in expected_origins:
            return JobResult(
                ok=False,
                error=f"checkout has unexpected origin; expected origin {expected_repo}",
            )

        status = git_utils.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if status.stdout.strip():
            return JobResult(ok=False, error=f"checkout is dirty: {checkout}")
        branch_result = git_utils.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            return JobResult(ok=False, error=f"checkout is detached: {checkout}")
        gh_command = _trusted_gh_executable()
        if gh_command is None:
            return JobResult(ok=False, error="required GitHub executable is unavailable")
        default_branch = git_utils.run(
            [gh_command, "api", f"repos/{expected_repo}", "--jq", ".default_branch"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.strip()
        if not default_branch:
            return JobResult(ok=False, error=f"repository has no default branch: {expected_repo}")
        if branch != default_branch:
            return JobResult(
                ok=False,
                error=(
                    f"checkout is not on its default branch {default_branch}: currently on {branch}"
                ),
            )
        return self._fast_forward_checkout(
            checkout=checkout,
            default_branch=default_branch,
            gh_command=gh_command,
            timeout_s=timeout_s,
        )

    @staticmethod
    def _checkout_state_error(*, checkout: Path, default_branch: str, timeout_s: int) -> str | None:
        """Return the clean-default-branch validation error, if any."""
        status = git_utils.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if status.stdout.strip():
            return f"checkout is dirty: {checkout}"
        branch_result = git_utils.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            return f"checkout is detached: {checkout}"
        if branch != default_branch:
            return f"checkout is not on its default branch {default_branch}: currently on {branch}"
        return None

    @staticmethod
    def _fast_forward_checkout(
        *,
        checkout: Path,
        default_branch: str,
        gh_command: str,
        timeout_s: int,
    ) -> JobResult:
        """Fetch and fast-forward a validated checkout while its metadata is locked."""
        hooks_disabled = f"core.hooksPath={os.devnull}"
        ssh_command = _trusted_executable("ssh", path=os.defpath)
        if ssh_command is None:
            return JobResult(ok=False, error="required fetch executable is unavailable")
        ssh_config = " ".join(
            (
                shlex.quote(ssh_command),
                "-F",
                shlex.quote(os.devnull),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            )
        )
        fetch_config = [
            "-c",
            hooks_disabled,
            "-c",
            f"core.sshCommand={ssh_config}",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=!{shlex.quote(gh_command)} auth git-credential",
            "-c",
            "core.askPass=",
            "-c",
            "http.sslVerify=true",
        ]
        git_utils.run(
            [
                "git",
                *fetch_config,
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                # The preflight validates origin before this point.  Fetch it
                # by name so a remote URL never reaches command debug logs.
                "origin",
                f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if validation_error := WorkerPool._checkout_state_error(
            checkout=checkout,
            default_branch=default_branch,
            timeout_s=timeout_s,
        ):
            return JobResult(ok=False, error=validation_error)
        relation = git_utils.run(
            [
                "git",
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...origin/{default_branch}",
            ],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.split()
        if len(relation) != 2:
            return JobResult(ok=False, error=f"could not compare checkout history: {checkout}")
        try:
            ahead, _behind = (int(count) for count in relation)
        except ValueError:
            return JobResult(ok=False, error=f"could not compare checkout history: {checkout}")
        if ahead:
            return JobResult(
                ok=False,
                error=f"checkout has local commits beyond origin/{default_branch}: {checkout}",
            )

        merge = git_utils.run(
            [
                "git",
                "-c",
                hooks_disabled,
                "-c",
                "core.fsmonitor=false",
                "merge",
                "--ff-only",
                f"origin/{default_branch}",
            ],
            cwd=checkout,
            check=False,
            log_errors=False,
            timeout=timeout_s,
            env=_controlled_git_env(),
        )
        if merge.returncode != 0:
            return JobResult(
                ok=False,
                error=(f"checkout cannot fast-forward {default_branch} to origin/{default_branch}"),
            )
        if validation_error := WorkerPool._checkout_state_error(
            checkout=checkout,
            default_branch=default_branch,
            timeout_s=timeout_s,
        ):
            return JobResult(ok=False, error=validation_error)
        synced_heads = git_utils.run(
            ["git", "rev-parse", "HEAD", f"origin/{default_branch}"],
            cwd=checkout,
            timeout=timeout_s,
            env=_controlled_git_env(),
        ).stdout.split()
        if len(synced_heads) != 2 or synced_heads[0] != synced_heads[1]:
            return JobResult(
                ok=False,
                error=f"checkout did not reach origin/{default_branch}: {checkout}",
            )
        synced_head = synced_heads[0]
        if not _is_full_commit_sha(synced_head):
            return JobResult(
                ok=False,
                error=f"checkout returned malformed default-branch SHA: {checkout}",
            )
        return JobResult(ok=True, value=synced_head)

    def _git_create_worktree(self, job: GitJob) -> JobResult:
        """Create a worktree and optionally sync an adopted PR branch."""
        kwargs = dict(job.kwargs)
        sync_to_remote = bool(kwargs.pop("sync_to_remote", False))
        pr_number = kwargs.pop("pr_number", None)
        repo_root_kwarg = kwargs.pop("repo_root", None)
        repo_root = Path(repo_root_kwarg) if repo_root_kwarg else get_repo_root()
        direct_setup = self._prepare_direct_scope_worktree(
            kwargs=kwargs,
            sync_to_remote=sync_to_remote,
            repo_root=repo_root,
            timeout_s=job.timeout_s,
        )
        if isinstance(direct_setup, JobResult):
            return direct_setup
        base_sha, branch_name = direct_setup
        base_dir = repo_root / "build" / ".worktrees"
        if isinstance(base_sha, str):
            manager = WorktreeManager(
                base_dir=base_dir,
                base_branch=base_sha,
                repo_root=repo_root,
            )
        else:
            manager = WorktreeManager(base_dir=base_dir, repo_root=repo_root)
        if base_sha is not None:
            kwargs["base_sha"] = base_sha
            kwargs["remote_branch_reserved"] = True
        try:
            created = manager.create_worktree(**kwargs, timeout=job.timeout_s)
        except Exception as exc:
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    timeout_s=job.timeout_s,
                    error=f"worktree creation failed: {exc}",
                )
            raise
        return self._finalize_created_worktree(
            created=created,
            base_sha=base_sha,
            branch_name=branch_name,
            repo_root=repo_root,
            repo=job.repo,
            sync_to_remote=sync_to_remote,
            pr_number=pr_number,
            timeout_s=job.timeout_s,
        )

    @staticmethod
    def _release_direct_scope_reservation(
        branch_name: str,
        base_sha: str | None,
        repo_root: Path,
        *,
        timeout_s: int,
    ) -> bool:
        """Conditionally release a direct reservation, or no-op for normal worktrees."""
        return base_sha is None or git_utils.delete_reserved_branch_if_unchanged(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
        )

    def _rollback_direct_scope_reservation(
        self,
        *,
        branch_name: str,
        base_sha: str,
        repo_root: Path,
        timeout_s: int,
        error: str,
    ) -> JobResult:
        """Release an early reservation or preserve its receipt for Finished."""
        try:
            released = self._release_direct_scope_reservation(
                branch_name, base_sha, repo_root, timeout_s=timeout_s
            )
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return JobResult(
                ok=False,
                value={"direct_scope_reservation": {"branch": branch_name, "base_sha": base_sha}},
                error=f"{error}; reservation rollback failed: {exc}",
            )
        if not released:
            return JobResult(
                ok=False,
                error=f"{error}; direct scope reservation changed before it could be released",
            )
        return JobResult(ok=False, error=error)

    def _finalize_created_worktree(
        self,
        *,
        created: Path | None,
        base_sha: str | None,
        branch_name: str,
        repo_root: Path,
        repo: str,
        sync_to_remote: bool,
        pr_number: object,
        timeout_s: int,
    ) -> JobResult:
        """Validate a created worktree and attach a direct reservation receipt."""
        if created is None:
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    timeout_s=timeout_s,
                    error="worktree manager returned no worktree",
                )
            # Non-direct callers retain the legacy no-op success contract.
            return JobResult(ok=True)
        worktree_path = Path(created)
        if repo_root not in worktree_path.parents and worktree_path != repo_root:
            error = (
                f"worktree {worktree_path} escaped resolved repo root {repo_root} "
                f"for job.repo={repo!r}"
            )
            if base_sha is not None:
                return self._rollback_direct_scope_reservation(
                    branch_name=branch_name,
                    base_sha=base_sha,
                    repo_root=repo_root,
                    timeout_s=timeout_s,
                    error=error,
                )
            return JobResult(
                ok=False,
                error=error,
            )
        if not sync_to_remote:
            if base_sha is not None:
                return JobResult(
                    ok=True,
                    value={
                        "path": str(worktree_path),
                        "direct_scope_reservation": {
                            "branch": branch_name,
                            "base_sha": base_sha,
                        },
                    },
                )
            return JobResult(ok=True, value=str(worktree_path))

        if pr_number is not None and not isinstance(pr_number, (int, str)):
            return JobResult(ok=False, error="worktree sync received an invalid PR number")

        try:
            dirty = not git_utils.is_clean_working_tree(worktree_path, timeout=timeout_s)
            status = ""
            diff = ""
            if dirty:
                status_result = git_utils.run(
                    ["git", "status", "--short"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=timeout_s,
                )
                diff_result = git_utils.run(
                    ["git", "diff"],
                    cwd=worktree_path,
                    capture_output=True,
                    check=False,
                    timeout=timeout_s,
                )
                status = status_result.stdout or ""
                diff = diff_result.stdout or ""
            elif branch_name:
                git_utils.sync_worktree_to_remote_branch(
                    worktree_path,
                    branch_name,
                    pr_number=int(pr_number) if isinstance(pr_number, (int, str)) else None,
                    timeout=timeout_s,
                )
        except Exception as exc:
            return JobResult(
                ok=False,
                error=f"worktree post-create preparation failed: {exc}",
                value={"path": str(worktree_path), WORKTREE_MATERIALIZED_KEY: True},
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

    @staticmethod
    def _prepare_direct_scope_worktree(
        *,
        kwargs: dict[str, object],
        sync_to_remote: bool,
        repo_root: Path,
        timeout_s: int,
    ) -> tuple[str | None, str] | JobResult:
        """Validate and atomically reserve a direct-scope implementation branch."""
        base_sha = kwargs.pop("base_sha", None)
        branch_name = str(kwargs.get("branch_name") or "")
        if base_sha is None:
            return None, branch_name
        if sync_to_remote or bool(kwargs.get("refresh_base", False)):
            return JobResult(ok=False, error="direct scope base pin invalid")
        if not _is_full_commit_sha(base_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        checkout_head = git_utils.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, timeout=timeout_s
        ).stdout.strip()
        if checkout_head != base_sha:
            return JobResult(ok=False, error="direct scope checkout pin mismatch")
        if not branch_name:
            return JobResult(ok=False, error="direct scope branch name is missing")
        git_utils.reserve_remote_branch_if_absent(
            branch_name,
            base_sha,
            repo_root,
            timeout=timeout_s,
        )
        return base_sha, branch_name

    @staticmethod
    def _git_verify_pr_review_checkout(job: GitJob) -> JobResult:
        """Synchronize a clean review checkout and bind it to one PR head.

        The review snapshot comes from GitHub before this job.  A remote move
        while synchronizing is not an error to paper over: the stage refreshes
        a new snapshot and retries a bounded number of times without sending an
        agent job for the stale input.
        """
        worktree = Path(str(job.kwargs.get("worktree_path") or ""))
        branch = str(job.kwargs.get("branch") or "")
        expected_head = str(job.kwargs.get("expected_head_sha") or "")
        base_branch = str(job.kwargs.get("base_branch") or "main")
        pr_number = job.kwargs.get("pr_number")
        if not worktree.is_dir() or not branch or not expected_head or not base_branch:
            return JobResult(
                ok=False,
                error="review checkout requires worktree, branch, base branch, and head",
            )
        if not git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s):
            return JobResult(ok=True, value={"ready": False, "reason": "dirty"})
        git_utils.sync_worktree_to_remote_branch(
            worktree,
            branch,
            pr_number=int(pr_number) if pr_number is not None else None,
            timeout=job.timeout_s,
        )
        head = git_utils.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, timeout=job.timeout_s
        ).stdout.strip()
        if head != expected_head:
            return JobResult(ok=True, value={"ready": False, "reason": "head_drift"})
        if not git_utils.is_clean_working_tree(worktree, timeout=job.timeout_s):
            return JobResult(ok=True, value={"ready": False, "reason": "dirty"})
        # Build the prompt diff from the checkout only after it is proven to
        # be the head captured above.  ``gh pr diff`` is mutable and cannot
        # distinguish an A -> B -> A head race from a stable A snapshot.
        git_utils.run(
            ["git", "fetch", "origin", "--", base_branch],
            cwd=worktree,
            timeout=job.timeout_s,
        )
        base = git_utils.run(
            ["git", "rev-parse", f"origin/{base_branch}"],
            cwd=worktree,
            timeout=job.timeout_s,
        ).stdout.strip()
        if not base:
            return JobResult(ok=False, error="review checkout base ref unavailable")
        diff = git_utils.run(
            ["git", "diff", "--no-ext-diff", "--binary", f"{base}...{head}"],
            cwd=worktree,
            timeout=job.timeout_s,
        ).stdout
        if not isinstance(diff, str):
            return JobResult(ok=False, error="review checkout diff unavailable")
        return JobResult(ok=True, value={"ready": True, "head": head, "base": base, "diff": diff})

    def _git_remove_worktree(self, job: GitJob) -> JobResult:
        """Remove a worktree by known path, or fall back to manager state."""
        if job.kwargs.get("worktree_path"):
            worktree_path = Path(str(job.kwargs["worktree_path"]))
            repo_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
            # This is the same lock create_worktree takes. Keep it across
            # removal, prune, and local cleanup so pipeline workers cannot
            # attach the branch between those operations. The final
            # ``git branch -d`` check also refuses an externally checked-out
            # branch.
            with file_lock(WorktreeManager.git_metadata_lock_path(repo_root)):
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
                local_cleanup = job.kwargs.get("local_branch_cleanup")
                if local_cleanup is not None:
                    if not isinstance(local_cleanup, dict):
                        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                    branch_name = local_cleanup.get("branch")
                    expected_sha = local_cleanup.get("base_sha")
                    if not isinstance(branch_name, str) or not _is_full_commit_sha(expected_sha):
                        return JobResult(ok=False, error="local branch cleanup receipt is invalid")
                    deleted = git_utils.delete_local_branch_if_unchanged(
                        branch_name,
                        expected_sha,
                        repo_root,
                        timeout=job.timeout_s,
                    )
                    return JobResult(ok=True, value={"local_branch_deleted": deleted})
            return JobResult(ok=True)
        fallback_root = Path(str(job.kwargs.get("repo_root") or get_repo_root()))
        manager = WorktreeManager(repo_root=fallback_root)
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
        # ``commit_if_changes`` returns False for a clean worktree.  An agent
        # is instructed to leave its edits uncommitted, but a defensive
        # recovery still recognizes a clean branch that is ahead of its
        # remote tracking ref: the coordinator, not the agent, publishes that
        # already-created commit so every subsequent review binds to the new
        # remote head.
        changed = git_utils.commit_if_changes(
            int(issue_number),
            Path(worktree_path),
            str(job.kwargs.get("agent", "claude")),
            allowed_paths=job.kwargs.get("allowed_paths"),
            timeout=job.timeout_s,
        )
        branch = str(job.kwargs.get("branch") or "")
        if not changed:
            publish_state = self._commit_push_requires_publish(
                job=job,
                branch=branch,
                worktree_path=Path(worktree_path),
            )
            if isinstance(publish_state, JobResult):
                return publish_state
            if not publish_state:
                return JobResult(ok=True, value=False)
            status = git_utils.run(
                ["git", "status", "--porcelain"],
                cwd=Path(worktree_path),
                capture_output=True,
                timeout=job.timeout_s,
            )
            if status.stdout.strip():
                return JobResult(ok=False, error="commit_push left uncommitted changes")
        branch = branch or "HEAD"
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is not None and not _is_full_commit_sha(expected_remote_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        if bool(job.kwargs.get("publish_detached_head", False)):
            if not isinstance(expected_remote_sha, str):
                return JobResult(
                    ok=False,
                    error="detached PR push requires the reviewed remote head",
                )
            git_utils.push_head_to_branch(
                branch,
                expected_remote_sha,
                Path(worktree_path),
                timeout=job.timeout_s,
            )
        elif isinstance(expected_remote_sha, str):
            git_utils.push_branch_if_remote_matches(
                branch,
                expected_remote_sha,
                Path(worktree_path),
                timeout=job.timeout_s,
            )
        else:
            git_utils.push_branch(branch, Path(worktree_path), timeout=job.timeout_s)
        return JobResult(ok=True, value=True)

    @staticmethod
    def _commit_push_requires_publish(
        *, job: GitJob, branch: str, worktree_path: Path
    ) -> bool | JobResult:
        """Return whether a clean worktree still needs coordinator-owned publication."""
        expected_remote_sha = job.kwargs.get("expected_remote_sha")
        if expected_remote_sha is None:
            return bool(
                branch
                and git_utils.has_unpushed_commits(
                    branch,
                    worktree_path,
                    timeout=job.timeout_s,
                )
            )
        if not _is_full_commit_sha(expected_remote_sha):
            return JobResult(ok=False, error="direct scope base pin invalid")
        if not branch:
            return JobResult(ok=False, error="direct scope branch name is missing")
        ahead = git_utils.run(
            ["git", "rev-list", "--count", f"{expected_remote_sha}..HEAD"],
            cwd=worktree_path,
            capture_output=True,
            check=False,
            timeout=job.timeout_s,
        )
        if ahead.returncode != 0:
            return JobResult(ok=False, error="cannot verify direct scope branch ancestry")
        if ahead.stdout.strip() != "0":
            return True
        released = git_utils.delete_reserved_branch_if_unchanged(
            branch,
            expected_remote_sha,
            worktree_path,
            timeout=job.timeout_s,
        )
        if not released:
            return JobResult(
                ok=False,
                error="direct scope reservation changed before it could be released",
            )
        return False
