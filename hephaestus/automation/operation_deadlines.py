"""Monotonic operation deadline helpers for automation boundaries."""

from __future__ import annotations

import math
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from hephaestus.utils.file_lock import LockUnavailableError


def _transport_seams(instance: object) -> Any:
    """Return the composed adapter's patchable module seams."""
    for base in type(instance).__mro__:
        if base.__module__.endswith(".pipeline_github_transport"):
            return sys.modules[base.__module__]
    raise RuntimeError("GitHub deadline host has no transport seam")


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

    def _deadline_gh_call(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]: ...

    def _operation_file_lock(self, path: Path, *, require_exclusive: bool = False) -> Any: ...


class PipelineGitHubDeadlineMixin:
    """Apply one absolute deadline to all GitHub work in one request."""

    _gh_timeout: int
    _operation_deadline_s: float | None = None
    _viewer_login_cache: str | None

    @contextmanager
    def operation_deadline(self, deadline_s: float) -> Iterator[None]:
        """Apply one absolute monotonic deadline to this operation."""
        if (
            isinstance(deadline_s, bool)
            or not isinstance(deadline_s, (int, float))
            or not math.isfinite(deadline_s)
            or deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic time")
        prior = self._operation_deadline_s
        self._operation_deadline_s = float(deadline_s)
        try:
            yield
        finally:
            self._operation_deadline_s = prior

    def _operation_timeout(self, requested_s: int | float | None = None) -> float | None:
        """Return the bounded time that remains before the operation deadline."""
        if self._operation_deadline_s is None:
            return float(requested_s) if requested_s is not None else None
        now = cast(float, _transport_seams(self).time.monotonic())
        remaining_s = self._operation_deadline_s - now
        if remaining_s <= 0:
            raise subprocess.TimeoutExpired("GitHub operation deadline", 0)
        return remaining_s if requested_s is None else min(float(requested_s), remaining_s)

    def _deadline_gh_call(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Run one GitHub CLI child within the active operation deadline."""
        kwargs["timeout"] = self._operation_timeout(kwargs.get("timeout", self._gh_timeout))
        return cast(
            subprocess.CompletedProcess[str],
            _transport_seams(self).gh_call(argv, **kwargs),
        )

    def _deadline_viewer_login(self, fallback: Callable[[], str]) -> str:
        """Return the cached actor login within the active operation deadline."""
        if self._viewer_login_cache is None:
            if self._operation_deadline_s is None:
                self._viewer_login_cache = fallback()
            else:
                result = self._deadline_gh_call(
                    ["api", "user", "--jq", ".login"],
                    check=False,
                )
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
        if self._operation_deadline_s is None:
            with _transport_seams(self).file_lock(path, require_exclusive=require_exclusive):
                yield
            return
        while True:
            remaining_s = cast(float, self._operation_timeout())
            stack = ExitStack()
            try:
                stack.enter_context(
                    _transport_seams(self).file_lock(
                        path, blocking=False, require_exclusive=require_exclusive
                    )
                )
            except LockUnavailableError:
                stack.close()
                _transport_seams(self).time.sleep(min(0.05, remaining_s))
                continue
            with stack:
                yield
                return
