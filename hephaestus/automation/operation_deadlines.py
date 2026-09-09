"""Monotonic operation deadline helpers for automation boundaries."""

from __future__ import annotations

import math
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from hephaestus.utils.file_lock import LockUnavailableError, file_lock

type GitHubCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def operation_deadline_after(timeout_s: int | float) -> float:
    """Return an absolute deadline after a positive timeout."""
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("operation timeout must be finite and positive")
    return time.monotonic() + float(timeout_s)


class OperationDeadlineHost(Protocol):
    """Declare deadline operations used by composed GitHub collaborators."""

    _operation_deadline_s: float | None
    _operation_shutdown: Event | None

    def operation_deadline(
        self, deadline_s: float, *, shutdown: Event | None = None
    ) -> AbstractContextManager[None]:
        """Keep an existing deadline and cancellation signal for this request."""
        ...

    def _operation_timeout(self, requested_s: int | float | None = None) -> float | None: ...

    def _deadline_gh_call(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Run the command with the supplied process controls."""

    def _operation_file_lock(self, path: Path, *, require_exclusive: bool = False) -> Any: ...


class PipelineGitHubDeadlineMixin:
    """Apply one absolute deadline to all GitHub work in one request."""

    _gh_timeout: int
    _operation_deadline_s: float | None = None
    _operation_shutdown: Event | None = None
    _viewer_login_cache: str | None
    _command_runner: GitHubCommandRunner

    @contextmanager
    def operation_deadline(
        self, deadline_s: float, *, shutdown: Event | None = None
    ) -> Iterator[None]:
        """Apply one absolute monotonic deadline to this operation."""
        if (
            isinstance(deadline_s, bool)
            or not isinstance(deadline_s, (int, float))
            or not math.isfinite(deadline_s)
            or deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic time")
        prior = self._operation_deadline_s
        prior_shutdown = self._operation_shutdown
        self._operation_deadline_s = min(prior, deadline_s) if prior is not None else deadline_s
        self._operation_shutdown = shutdown if shutdown is not None else prior_shutdown
        try:
            self._operation_timeout()
            yield
        finally:
            self._operation_deadline_s = prior
            self._operation_shutdown = prior_shutdown

    def _operation_timeout(self, requested_s: int | float | None = None) -> float | None:
        """Return the bounded time that remains before the operation deadline."""
        if self._operation_shutdown is not None and self._operation_shutdown.is_set():
            raise CancelledError("GitHub operation was cancelled")
        if self._operation_deadline_s is None:
            return float(requested_s) if requested_s is not None else None
        now = time.monotonic()
        remaining_s = self._operation_deadline_s - now
        if remaining_s <= 0:
            raise subprocess.TimeoutExpired("GitHub operation deadline", 0)
        return remaining_s if requested_s is None else min(float(requested_s), remaining_s)

    def _deadline_gh_call(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Run one GitHub CLI child within the active operation deadline."""
        kwargs["timeout"] = self._operation_timeout(kwargs.get("timeout", self._gh_timeout))
        deadline = self._operation_deadline_s
        if deadline is None:
            deadline = operation_deadline_after(kwargs["timeout"] or self._gh_timeout)
        requested_deadline = kwargs.get("deadline_s")
        kwargs["deadline_s"] = (
            min(deadline, requested_deadline) if requested_deadline is not None else deadline
        )
        kwargs["max_retries"] = 1
        kwargs["retry_on_rate_limit"] = False
        if self._operation_shutdown is not None:
            kwargs["shutdown"] = self._operation_shutdown
        return self._command_runner(argv, **kwargs)

    def _deadline_viewer_login(self) -> str:
        """Return the cached actor login within the active operation deadline."""
        if self._viewer_login_cache is None:
            result = self._deadline_gh_call(["api", "user", "--jq", ".login"], check=False)
            self._viewer_login_cache = (
                (result.stdout or "").strip() if result.returncode == 0 else ""
            )
        if not self._viewer_login_cache:
            raise RuntimeError("cannot verify GitHub comment ownership: viewer login unavailable")
        return self._viewer_login_cache

    @contextmanager
    def _operation_file_lock(
        self, path: Path, *, require_exclusive: bool = False
    ) -> Iterator[None]:
        """Acquire one file lock without waiting past the operation deadline."""
        deadline = self._operation_deadline_s or operation_deadline_after(self._gh_timeout)
        while True:
            self._operation_timeout()
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                raise subprocess.TimeoutExpired("GitHub file lock deadline", 0)
            stack = ExitStack()
            try:
                stack.enter_context(
                    file_lock(path, blocking=False, require_exclusive=require_exclusive)
                )
            except LockUnavailableError:
                stack.close()
                delay = min(0.05, remaining_s)
                if self._operation_shutdown is None:
                    time.sleep(delay)
                else:
                    self._operation_shutdown.wait(delay)
                continue
            with stack:
                self._operation_timeout()
                yield
                return
