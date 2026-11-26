"""Behavior tests for the bounded CLI startup update check (issue #3112).

All release metadata is controlled in-process. No test reaches the network.
"""

from __future__ import annotations

import io
import urllib.request
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError
from typing import Any
from unittest.mock import MagicMock

import pytest
from packaging.version import Version

from hephaestus.version import update_check
from hephaestus.version.update_check import (
    check_for_updates,
    latest_stable_version,
    reset_check_state,
)


@pytest.fixture(autouse=True)
def _controlled_install_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use controlled install facts and isolate the once-per-invocation gate."""
    monkeypatch.setattr(update_check, "_is_editable_install", lambda: False)
    reset_check_state()
    yield
    reset_check_state()


def _release(version: str) -> dict[str, Any]:
    """Return controlled package-index metadata for one release."""
    return {"info": {"version": version}, "releases": {version: []}}


def test_older_installed_version_emits_stderr_notice() -> None:
    """An installed release older than the latest stable release gets one notice."""
    stream = io.StringIO()
    emitted = check_for_updates(
        stream=stream,
        current_version="0.9.9",
        interpreter="/opt/venv/bin/python",
        fetcher=lambda _url, _timeout: _release("0.10.0"),
    )

    assert emitted is True
    notice = stream.getvalue()
    assert "0.10.0" in notice
    assert "0.9.9" in notice
    assert "/opt/venv/bin/python" in notice
    assert "-m pip install --upgrade HomericIntelligence-Hephaestus" in notice


def test_equal_version_emits_no_notice() -> None:
    """An up-to-date installation is silent."""
    stream = io.StringIO()
    emitted = check_for_updates(
        stream=stream,
        current_version="1.2.3",
        fetcher=lambda _url, _timeout: _release("1.2.3"),
    )

    assert emitted is False
    assert stream.getvalue() == ""


def test_newer_installed_version_emits_no_notice() -> None:
    """A newer installation does not downgrade the operator."""
    stream = io.StringIO()
    emitted = check_for_updates(
        stream=stream,
        current_version="2.0.0",
        fetcher=lambda _url, _timeout: _release("1.9.0"),
    )

    assert emitted is False
    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    ("installed", "published", "expected"),
    [
        ("0.9.9", "0.10.0", True),
        ("0.10.0", "0.9.9", False),
        ("9.9.9", "10.0.0", True),
    ],
)
def test_numeric_version_boundary(installed: str, published: str, expected: bool) -> None:
    """Version segments compare numerically, not as strings."""
    stream = io.StringIO()
    emitted = check_for_updates(
        stream=stream,
        current_version=installed,
        fetcher=lambda _url, _timeout: _release(published),
    )

    assert emitted is expected
    assert bool(stream.getvalue()) is expected


@pytest.mark.parametrize("installed", ["1.2.3.dev4", "1.2.3rc1", "1.2.3+local.1", "unknown"])
def test_dev_and_unusable_current_versions_skip(installed: str) -> None:
    """Development, pre-release, local, and unknown versions are not compared."""
    stream = io.StringIO()
    calls: list[float] = []

    def fetcher(_url: str, timeout: float) -> dict[str, Any]:
        calls.append(timeout)
        return _release("9.9.9")

    emitted = check_for_updates(
        stream=stream,
        current_version=installed,
        fetcher=fetcher,
    )

    assert emitted is False
    assert stream.getvalue() == ""
    assert calls == []


def test_editable_install_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install does not report a release update."""
    monkeypatch.setattr(update_check, "_is_editable_install", lambda: True)
    stream = io.StringIO()

    emitted = check_for_updates(
        stream=stream,
        current_version="0.9.9",
        fetcher=lambda _url, _timeout: _release("0.10.0"),
    )

    assert emitted is False
    assert stream.getvalue() == ""


def test_missing_metadata_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source checkout without distribution metadata stays silent."""

    def missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(update_check, "distribution_version", missing)
    stream = io.StringIO()

    emitted = check_for_updates(
        stream=stream,
        fetcher=lambda _url, _timeout: _release("9.9.9"),
    )

    assert emitted is False
    assert stream.getvalue() == ""


def test_timeout_does_not_block_or_emit() -> None:
    """A bounded network timeout is advisory and cannot change an exit status."""

    def timeout_fetcher(_url: str, _timeout: float) -> dict[str, Any]:
        raise TimeoutError("network unavailable")

    stream = io.StringIO()

    assert (
        check_for_updates(
            stream=stream,
            current_version="0.9.9",
            fetcher=timeout_fetcher,
        )
        is False
    )
    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"info": "not an object"},
        {"info": {"version": "not-a-version"}},
        {"info": {"version": None}},
        {"releases": {"1.0.0rc1": [], "2.0.0.dev1": []}},
    ],
)
def test_malformed_metadata_skips(metadata: dict[str, Any]) -> None:
    """Malformed or prerelease-only metadata produces no notice."""
    stream = io.StringIO()

    emitted = check_for_updates(
        stream=stream,
        current_version="0.1.0",
        fetcher=lambda _url, _timeout: metadata,
    )

    assert emitted is False
    assert stream.getvalue() == ""


def test_latest_stable_version_ignores_prereleases() -> None:
    """Release-map selection picks the highest final release only."""
    metadata = {
        "info": {"version": "2.1.0rc1"},
        "releases": {
            "0.9.9": [],
            "0.10.0": [],
            "2.0.0.dev3": [],
            "not-a-version": [],
        },
    }

    assert latest_stable_version(metadata) == Version("0.10.0")


def test_notice_goes_to_stderr_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default stream is stderr; stdout stays empty for pipelines."""
    emitted = check_for_updates(
        current_version="0.9.9",
        interpreter="/usr/local/bin/python3",
        fetcher=lambda _url, _timeout: _release("0.10.0"),
    )

    captured = capsys.readouterr()
    assert emitted is True
    assert captured.out == ""
    assert "0.10.0" in captured.err


def test_notice_is_emitted_once_per_invocation() -> None:
    """Nested or repeated calls cannot send a second notice or a second fetch."""
    calls: list[str] = []
    stream = io.StringIO()

    def fetcher(_url: str, _timeout: float) -> dict[str, Any]:
        calls.append("fetch")
        return _release("9.9.9")

    assert check_for_updates(
        stream=stream,
        current_version="0.9.9",
        fetcher=fetcher,
    )
    first_notice = stream.getvalue()
    assert (
        check_for_updates(
            stream=stream,
            current_version="0.9.9",
            fetcher=fetcher,
        )
        is False
    )

    assert calls == ["fetch"]
    assert stream.getvalue() == first_notice
    assert first_notice.count("A newer release") == 1


def test_default_fetcher_passes_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production fetcher bounds the index read with the given timeout."""
    observed: dict[str, float] = {}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"info": {"version": "2.0.0"}}'

    def fake_urlopen(_request: object, *, timeout: float) -> MagicMock:
        observed["timeout"] = timeout
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    stream = io.StringIO()

    emitted = check_for_updates(
        stream=stream,
        current_version="1.0.0",
        timeout=1.25,
    )

    assert emitted is True
    assert observed["timeout"] == 1.25
    response.__enter__.return_value.read.assert_called_once_with(
        update_check._MAX_RESPONSE_BYTES + 1
    )
