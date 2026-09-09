"""Keep GitHub cancellation separate from service failure accounting."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import CancelledError
from threading import Event
from typing import Any

import pytest

import hephaestus.github.client as client


@pytest.fixture(autouse=True)
def reset_github_breaker() -> Iterator[None]:
    """Keep each test independent of the shared GitHub breaker."""
    client._GH_BREAKER.reset()
    yield
    client._GH_BREAKER.reset()


@pytest.mark.parametrize("cancel_after_start", [False, True])
def test_cancellation_preserves_prior_failures_and_timeouts_still_count(
    monkeypatch: pytest.MonkeyPatch, cancel_after_start: bool
) -> None:
    """Cancellation must neither clear nor increase an existing outage count."""
    shutdown = Event()
    cancel_in_runner = False
    starts = 0

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal starts
        starts += 1
        assert kwargs["shutdown"] is shutdown
        if cancel_in_runner:
            shutdown.set()
            raise InterruptedError("subprocess cancelled")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(client, "run_subprocess", run)

    def call() -> subprocess.CompletedProcess[str]:
        return client.gh_call(
            ["api", "rate_limit"],
            deadline_s=time.monotonic() + 60,
            shutdown=shutdown,
            throttle=False,
            log_on_error=False,
        )

    for _ in range(4):
        with pytest.raises(subprocess.TimeoutExpired):
            call()
    assert client._GH_BREAKER.snapshot()["failure_count"] == 4
    assert client._GH_BREAKER.state.value == "closed"

    starts_before_cancel = starts
    cancel_in_runner = cancel_after_start
    if not cancel_after_start:
        shutdown.set()
    with pytest.raises((CancelledError, InterruptedError)):
        call()

    assert starts == starts_before_cancel + int(cancel_after_start)
    assert client._GH_BREAKER.snapshot()["failure_count"] == 4
    assert client._GH_BREAKER.state.value == "closed"

    shutdown.clear()
    cancel_in_runner = False
    with pytest.raises(subprocess.TimeoutExpired):
        call()
    assert client._GH_BREAKER.snapshot()["failure_count"] == 5
    assert client._GH_BREAKER.state.value == "open"
