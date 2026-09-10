"""Keep bounded GitHub error classification free of extra requests."""

import json
import subprocess
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.github import client, rate_limit

_LIMIT_MESSAGE = "GraphQL: API rate limit exceeded for user ID 1"
_RESET_EPOCH = 1_700_000_000


@pytest.fixture(autouse=True)
def reset_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the breaker and rate-limit cache from other tests."""
    client._GH_BREAKER.reset()
    monkeypatch.setattr(rate_limit, "_rate_limit_probe_cache", {})
    yield
    client._GH_BREAKER.reset()


def reset_probe(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Replace the external reset request with a recorded response."""
    probe = Mock(
        return_value=subprocess.CompletedProcess(
            ["gh", "api", "rate_limit"],
            0,
            json.dumps({"resources": {"graphql": {"reset": _RESET_EPOCH}}}),
            "",
        )
    )
    monkeypatch.setattr(rate_limit, "run_subprocess", probe)
    return probe


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("stop", ["active", "expired", "cancelled", "cancelled_without_deadline"])
@pytest.mark.parametrize("stale_cache", [False, True])
def test_bounded_rate_limit_error_does_not_start_another_request(
    monkeypatch: pytest.MonkeyPatch, stream: str, stop: str, stale_cache: bool
) -> None:
    """Return unknown reset facts after one attempt, even when the budget ends."""
    now = [100.0]
    shutdown = threading.Event()
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    if stale_cache:
        rate_limit._rate_limit_probe_cache["graphql"] = (_RESET_EPOCH, 1.0)
    probe = reset_probe(monkeypatch)

    def fail_request(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if stop == "expired":
            now[0] = 111.0
        if stop.startswith("cancelled"):
            shutdown.set()
        raise subprocess.CalledProcessError(
            1,
            argv,
            output=_LIMIT_MESSAGE if stream == "stdout" else "",
            stderr=_LIMIT_MESSAGE if stream == "stderr" else "",
        )

    request = Mock(side_effect=fail_request)
    monkeypatch.setattr(client, "run_subprocess", request)
    with pytest.raises(client.GitHubRateLimitError) as caught:
        client.gh_call(
            ["issue", "view", "1"],
            deadline_s=None if stop == "cancelled_without_deadline" else 110.0,
            shutdown=shutdown,
            max_retries=1,
            retry_on_rate_limit=False,
            throttle=False,
        )

    request.assert_called_once()
    probe.assert_not_called()
    assert caught.value.reset_epoch == 0


@pytest.mark.parametrize("evidence", ["cached", "embedded"])
def test_bounded_rate_limit_error_retains_known_reset_evidence(
    monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    """Use a recent cache or an embedded reset time without another request."""
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    probe = reset_probe(monkeypatch)
    message = _LIMIT_MESSAGE
    if evidence == "cached":
        rate_limit._rate_limit_probe_cache["graphql"] = (_RESET_EPOCH, 99.0)
    else:
        message = "Limit reached for resource core, resets 2:30pm (UTC)"
    request = Mock(
        side_effect=subprocess.CalledProcessError(
            1,
            ["gh"],
            output=message if evidence == "embedded" else "",
            stderr=message if evidence == "cached" else "",
        )
    )
    monkeypatch.setattr(client, "run_subprocess", request)

    with pytest.raises(client.GitHubRateLimitError) as caught:
        client.gh_call(["issue", "view", "1"], deadline_s=110.0, throttle=False)

    request.assert_called_once()
    probe.assert_not_called()
    assert caught.value.reset_epoch > 0
    if evidence == "cached":
        assert caught.value.reset_epoch == _RESET_EPOCH


def test_manual_rate_limit_error_can_still_request_reset_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the independent utility default for a manual command."""
    probe = reset_probe(monkeypatch)
    request = Mock(side_effect=subprocess.CalledProcessError(1, ["gh"], stderr=_LIMIT_MESSAGE))
    monkeypatch.setattr(client, "run_subprocess", request)

    with pytest.raises(client.GitHubRateLimitError) as caught:
        client.gh_call(
            ["issue", "view", "1"],
            retry_on_rate_limit=False,
            max_retries=1,
            throttle=False,
        )

    request.assert_called_once()
    probe.assert_called_once()
    assert caught.value.reset_epoch == _RESET_EPOCH
