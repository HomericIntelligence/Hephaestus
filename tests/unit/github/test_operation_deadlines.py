"""Check the total deadline at the shared GitHub process boundary."""

import json
import subprocess
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.github import client, rate_limit


@pytest.fixture(autouse=True)
def reset_github_state() -> None:
    """Reset process-local state for each operation check."""
    client._GH_BREAKER.reset()
    rate_limit.configure_gh_global_throttle(rate=10.0, burst=30.0)


def test_throttle_wait_cannot_outlive_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stop a token wait at the deadline without starting GitHub work."""
    now = [100.0]
    waits: list[float] = []
    state = tmp_path / "throttle.json"
    state.write_text(json.dumps({"tokens": 0.0, "updated": now[0]}))
    monkeypatch.setattr(rate_limit, "_global_throttle_state_path", lambda: state)
    monkeypatch.setattr("hephaestus.github.rate_limit.time.monotonic", lambda: now[0])

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("hephaestus.github.rate_limit.time.sleep", sleep)
    run = Mock()
    monkeypatch.setattr(client, "run_subprocess", run)
    with pytest.raises(subprocess.TimeoutExpired):
        client.gh_call(["api", "rate_limit"], deadline_s=100.02, max_retries=1)
    assert sum(waits) == pytest.approx(0.02)
    run.assert_not_called()
    assert client._GH_BREAKER.snapshot()["failure_count"] == 0


def test_child_timeout_uses_budget_left_after_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deduct throttle time from the child process timeout."""
    now = [100.0]
    monkeypatch.setattr("hephaestus.github.client.time.monotonic", lambda: now[0])

    def acquire(**_kwargs: object) -> None:
        now[0] += 4.0

    monkeypatch.setattr(client, "gh_global_throttle_acquire", acquire)
    run = Mock(return_value=subprocess.CompletedProcess(["gh"], 0, "{}", ""))
    monkeypatch.setattr(client, "run_subprocess", run)
    client.gh_call(["api", "rate_limit"], deadline_s=110.0, max_retries=1, timeout=30)
    assert run.call_args.kwargs["timeout"] == pytest.approx(6.0)


def test_throttle_lock_observes_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancel lock polling without starting an external operation."""
    import fcntl

    shutdown = threading.Event()
    calls = 0
    monkeypatch.setattr(rate_limit, "_global_throttle_state_path", lambda: tmp_path / "state")

    def flock(_fd: int, flags: int) -> None:
        nonlocal calls
        assert flags & fcntl.LOCK_NB
        calls += 1
        shutdown.set()
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(CancelledError):
        rate_limit.gh_global_throttle_acquire(shutdown=shutdown)
    assert calls == 1


def test_cancelled_dispatch_does_not_change_provider_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep cancellation local to the current operation."""
    shutdown = threading.Event()
    shutdown.set()
    run = Mock()
    monkeypatch.setattr(client, "run_subprocess", run)
    with pytest.raises(CancelledError):
        client.gh_call(["api", "rate_limit"], shutdown=shutdown, throttle=False)
    run.assert_not_called()
    assert client._GH_BREAKER.snapshot()["failure_count"] == 0
