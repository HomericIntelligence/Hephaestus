"""Dependency-neutral Git execution and repository identity helpers."""

import logging
import math
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from hephaestus.config.child_environments import read_approved_parent_env
from hephaestus.utils.cache import ThreadSafeCache
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.git import run_git as _shared_run_git
from hephaestus.utils.helpers import get_repo_root as get_repo_root, run_subprocess

logger = logging.getLogger(__name__)

_operation_deadline_s: ContextVar[float | None] = ContextVar(
    "git_operation_deadline_s",
    default=None,
)
_operation_shutdown: ContextVar[threading.Event | None] = ContextVar(
    "git_operation_shutdown", default=None
)


@contextmanager
def operation_deadline(
    deadline_s: float | None, *, shutdown: threading.Event | None = None
) -> Iterator[None]:
    """Apply one absolute monotonic deadline to all Git children in this context."""
    if deadline_s is not None and (
        isinstance(deadline_s, bool)
        or not isinstance(deadline_s, (int, float))
        or not math.isfinite(deadline_s)
        or deadline_s <= 0
    ):
        raise ValueError("deadline_s must be a finite positive monotonic time")
    token = _operation_deadline_s.set(float(deadline_s) if deadline_s is not None else None)
    shutdown_token = _operation_shutdown.set(shutdown or _operation_shutdown.get())
    try:
        yield
    finally:
        _operation_shutdown.reset(shutdown_token)
        _operation_deadline_s.reset(token)


def current_operation_shutdown() -> threading.Event | None:
    """Return the active operation's cancellation event."""
    return _operation_shutdown.get()


def remaining_operation_timeout(timeout: int | float | None) -> int | float | None:
    """Return the smaller per-child timeout or operation time that remains."""
    shutdown = current_operation_shutdown()
    if shutdown is not None and shutdown.is_set():
        raise InterruptedError("Git operation cancelled")
    deadline_s = _operation_deadline_s.get()
    if deadline_s is None:
        return timeout
    remaining_s = deadline_s - time.monotonic()
    if remaining_s <= 0:
        raise subprocess.TimeoutExpired("operation deadline", 0)
    return remaining_s if timeout is None else min(float(timeout), remaining_s)


@contextmanager
def operation_file_lock(path: Path) -> Iterator[None]:
    """Hold a file lock within the active Git operation's time and stop limits."""
    shutdown = current_operation_shutdown()
    bounded = _operation_deadline_s.get() is not None or shutdown is not None
    with ExitStack() as stack:
        while True:
            remaining_operation_timeout(None)
            try:
                stack.enter_context(file_lock(path, blocking=not bounded))
            except LockUnavailableError:
                if not bounded:
                    raise
                wait_s = cast(float, remaining_operation_timeout(0.1))
                if shutdown is None:
                    time.sleep(wait_s)
                else:
                    shutdown.wait(wait_s)
                continue
            break
        remaining_operation_timeout(None)
        yield


def run(
    cmd: list[str],
    cwd: Path | None = None,
    capture_output: bool = True,
    check: bool = True,
    timeout: int | float | None = None,
    log_errors: bool = True,
    env: dict[str, str] | None = None,
    shutdown: threading.Event | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with consistent, redacted error handling.

    Args:
        input_text: Text to send through standard input. If this value is None, no text is sent.

    """
    logger.debug("Running subprocess")
    timeout = remaining_operation_timeout(timeout)
    shutdown = shutdown or current_operation_shutdown()
    cancellation: dict[str, Any] = {"shutdown": shutdown} if shutdown is not None else {}
    try:
        if cmd and cmd[0] == "git":
            return _shared_run_git(
                cmd,
                cwd=cwd,
                timeout=cast(int | None, timeout),
                check=check,
                log_on_error=False,
                env=env,
                input_text=input_text,
                retries=0,
                **cancellation,
            )
        return run_subprocess(
            cmd,
            env=env if env is not None else read_approved_parent_env(),
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            check=check,
            log_on_error=False,
            input_text=input_text,
            **cancellation,
        )
    except subprocess.TimeoutExpired:
        if log_errors:
            logger.error("Subprocess timed out")
        raise
    except subprocess.CalledProcessError as error:
        if log_errors:
            logger.error("Subprocess failed with exit code %s", error.returncode)
        raise


_repo_info_cache: ThreadSafeCache[Path | None, tuple[str, str]] = ThreadSafeCache()
_repo_slug_cache: ThreadSafeCache[Path | None, str] = ThreadSafeCache()


def get_repo_info(repo_root: Path | None = None) -> tuple[str, str]:
    """Get repository owner and name from the Git ``origin`` remote."""
    if repo_root is None:
        repo_root = get_repo_root()

    key = repo_root.resolve() if repo_root is not None else None

    def _compute() -> tuple[str, str]:
        try:
            result = run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_root,
                capture_output=True,
                check=True,
            )
            remote_url = result.stdout.strip()

            # Parse SSH (git@github.com:owner/repo.git) and HTTPS remotes.
            if "@" in remote_url and ":" in remote_url:
                parts = remote_url.split(":")[-1].replace(".git", "").split("/")
                owner, repo = parts[-2], parts[-1]
            elif remote_url.startswith("https://"):
                parts = remote_url.replace(".git", "").split("/")
                owner, repo = parts[-2], parts[-1]
            else:
                raise RuntimeError(f"Unable to parse git remote URL: {remote_url}")

            logger.debug("Detected repo: %s/%s", owner, repo)
            return owner, repo
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Failed to get git remote URL: {error}") from error

    return _repo_info_cache.get_or_compute(key, _compute)


def get_repo_slug(repo_root: Path | None = None) -> str:
    """Return the cached short repository name, falling back to ``repo``."""
    key = repo_root.resolve() if repo_root is not None else None

    def _compute() -> str:
        try:
            _, repo = get_repo_info(repo_root)
        except (RuntimeError, subprocess.CalledProcessError):
            return "repo"
        return repo

    return _repo_slug_cache.get_or_compute(key, _compute)


def clear_repo_caches() -> None:
    """Clear repository identity caches."""
    _repo_info_cache.clear()
    _repo_slug_cache.clear()


def issue_ref(issue_number: int | str) -> str:
    """Return a repository-qualified issue reference."""
    return f"{get_repo_slug()}#{issue_number}"


def pr_ref(pr_number: int | str) -> str:
    """Return a repository-qualified pull-request reference."""
    return f"{get_repo_slug()}#{pr_number}"
