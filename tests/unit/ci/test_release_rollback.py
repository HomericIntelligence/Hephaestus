"""Unit tests for fail-closed immutable-release withdrawal operations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from hephaestus.ci.release_rollback import (
    WITHDRAWN_MARKER,
    ReleaseApiClient,
    ReleaseRollbackError,
    ReleaseState,
    _pypi_release_is_yanked,
    apply_withdrawal_advisory,
    assert_preflight_clear,
    asset_fingerprints,
    prepend_withdrawal_advisory,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _release(*, body: str = "Existing release notes.", immutable: bool = True) -> dict[str, Any]:
    """Return a complete immutable GitHub release fixture."""
    return {
        "id": 42,
        "immutable": immutable,
        "body": body,
        "assets": [
            {
                "id": 100,
                "name": "hephaestus-1.2.3-py3-none-any.whl",
                "size": 1024,
                "digest": "sha256:asset",
            }
        ],
    }


def _state(**overrides: Any) -> ReleaseState:
    """Return a rollback-ready release state with selected fields replaced."""
    defaults: dict[str, Any] = {
        "tag_exists": True,
        "testpypi_exists": False,
        "pypi_exists": True,
        "pypi_yanked": True,
        "github_release": _release(),
    }
    defaults.update(overrides)
    return ReleaseState(**defaults)


class _ReleaseClient:
    """Minimal stateful client used to assert release-note mutation behavior."""

    def __init__(self, release: dict[str, Any]) -> None:
        self.release = release
        self.update_calls: list[tuple[int, str]] = []

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        """Return the fixture release for the expected tag."""
        assert tag == "v1.2.3"
        return self.release

    def update_release_notes(self, release_id: int, body: str) -> None:
        """Record the PATCH-equivalent update and replace only release notes."""
        self.update_calls.append((release_id, body))
        self.release["body"] = body


class _JsonResponse:
    """Context-managed JSON response for standard-library HTTP client tests."""

    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.payload = payload

    def __enter__(self) -> _JsonResponse:
        """Return the fake response as a context-manager result."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Do not suppress exceptions from the request context."""

    def read(self) -> bytes:
        """Return the encoded JSON response body."""
        return json.dumps(self.payload).encode("utf-8")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (_state(tag_exists=False), "tag v1.2.3 does not exist"),
        (_state(testpypi_exists=True), "version already exists on TestPyPI"),
        (_state(pypi_exists=True), "version already exists on PyPI"),
        (_state(github_release=_release()), "tag already has a GitHub release or draft"),
    ],
)
def test_preflight_rejects_each_occupied_publication_target(
    state: ReleaseState, expected: str
) -> None:
    """Preflight rejects every occupied target instead of relying on idempotency."""
    if expected == "version already exists on PyPI":
        state = _state(pypi_exists=True, testpypi_exists=False, github_release=None)
    elif expected == "tag already has a GitHub release or draft":
        state = _state(pypi_exists=False, github_release=_release())
    elif expected == "version already exists on TestPyPI":
        state = _state(pypi_exists=False, testpypi_exists=True, github_release=None)

    with pytest.raises(ReleaseRollbackError, match=expected):
        assert_preflight_clear(state, "v1.2.3")


def test_preflight_requires_existing_remote_tag() -> None:
    """Publication cannot begin from a ref that is not a remote release tag."""
    with pytest.raises(ReleaseRollbackError, match=r"tag v1\.2\.3 does not exist"):
        assert_preflight_clear(
            _state(
                tag_exists=False,
                pypi_exists=False,
                github_release=None,
            ),
            "v1.2.3",
        )


def test_preflight_rejects_existing_github_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-tag GitHub draft is an occupied publication target."""
    draft = {**_release(), "draft": True, "tag_name": "v1.2.3"}

    def serve_request(request: Any, timeout: float) -> _JsonResponse:
        url = request.full_url
        if "/git/ref/tags/" in url:
            return _JsonResponse({"ref": "refs/tags/v1.2.3"})
        if "/releases/tags/" in url:
            raise HTTPError(url, 404, "not published", hdrs=Message(), fp=None)
        if "/releases?" in url:
            assert request.get_header("Authorization") == "Bearer fixture-token"
            return _JsonResponse([draft])
        if "/pypi/" in url:
            raise HTTPError(url, 404, "not published", hdrs=Message(), fp=None)
        raise AssertionError(f"unexpected URL: {url}")

    import hephaestus.ci.release_rollback as rollback

    monkeypatch.setattr(rollback, "_open_without_redirects", serve_request)
    client = ReleaseApiClient(
        repository="HomericIntelligence/Hephaestus",
        package="HomericIntelligence-Hephaestus",
        github_api_base="https://github.fixture",
        pypi_api_base="https://pypi.fixture",
        token="fixture-token",  # noqa: S106 - non-secret local fixture value
    )

    state = client.get_state("v1.2.3")

    assert state.github_release == draft
    with pytest.raises(ReleaseRollbackError, match="GitHub release or draft"):
        assert_preflight_clear(state, "v1.2.3")


def test_preflight_requires_token_to_check_for_github_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight fails rather than treating an unauthenticated draft list as complete."""

    def serve_request(request: Any, timeout: float) -> _JsonResponse:
        url = request.full_url
        if "/git/ref/tags/" in url:
            return _JsonResponse({"ref": "refs/tags/v1.2.3"})
        if "/releases/tags/" in url:
            raise HTTPError(url, 404, "not published", hdrs=Message(), fp=None)
        raise AssertionError(f"unexpected URL: {url}")

    import hephaestus.ci.release_rollback as rollback

    monkeypatch.setattr(rollback, "_open_without_redirects", serve_request)
    client = ReleaseApiClient(
        repository="HomericIntelligence/Hephaestus",
        package="HomericIntelligence-Hephaestus",
        github_api_base="https://github.fixture",
        pypi_api_base="https://pypi.fixture",
    )

    with pytest.raises(ReleaseRollbackError, match="GH_TOKEN is required"):
        client.get_state("v1.2.3")


def test_wrapper_runs_from_an_uninstalled_source_checkout() -> None:
    """The workflow wrapper must import this checkout without a dev-install."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "release_rollback.py"), "--help"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "preflight" in result.stdout


def test_rollback_requires_every_pypi_file_to_be_yanked() -> None:
    """The advisory is withheld until the PyPI withdrawal is complete."""
    assert (
        _pypi_release_is_yanked(
            {
                "urls": [
                    {"filename": "release.tar.gz", "yanked": True},
                    {"filename": "release.whl", "yanked": False},
                ]
            }
        )
        is False
    )
    client = _ReleaseClient(_release())

    with pytest.raises(ReleaseRollbackError, match="PyPI release must be yanked"):
        apply_withdrawal_advisory(
            client, _state(pypi_yanked=False), tag="v1.2.3", reason="Broken wheel"
        )

    assert client.update_calls == []


def test_rollback_requires_immutable_github_release() -> None:
    """Rollback refuses mutable or incomplete GitHub release records."""
    client = _ReleaseClient(_release(immutable=False))

    with pytest.raises(ReleaseRollbackError, match="immutable"):
        apply_withdrawal_advisory(
            client,
            _state(github_release=client.release),
            tag="v1.2.3",
            reason="Broken wheel",
        )

    assert client.update_calls == []


def test_advisory_preserves_existing_release_notes() -> None:
    """Withdrawal PATCHes only release notes and preserves asset fingerprints."""
    release = _release()
    client = _ReleaseClient(release)
    before = asset_fingerprints(release)

    apply_withdrawal_advisory(
        client, _state(github_release=release), tag="v1.2.3", reason="Broken wheel"
    )

    assert client.update_calls == [(42, release["body"])]
    assert release["body"].endswith("Existing release notes.")
    assert WITHDRAWN_MARKER in release["body"]
    assert "Broken wheel" in release["body"]
    assert asset_fingerprints(release) == before


def test_existing_advisory_is_idempotent() -> None:
    """A repeated rollback does not duplicate the immutable-release warning."""
    body = prepend_withdrawal_advisory("Existing release notes.", "v1.2.3", "Broken wheel")
    release = _release(body=body)
    client = _ReleaseClient(release)

    apply_withdrawal_advisory(
        client, _state(github_release=release), tag="v1.2.3", reason="Broken wheel"
    )

    assert client.update_calls == []
    assert release["body"].count(WITHDRAWN_MARKER) == 1


@pytest.mark.parametrize("status", [401, 500])
def test_http_auth_and_server_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Only expected 404s are absence; authentication and 5xx failures abort."""

    def fail_request(request: Any, timeout: float) -> Any:
        raise HTTPError(request.full_url, status, "fixture failure", hdrs=Message(), fp=None)

    import hephaestus.ci.release_rollback as rollback

    monkeypatch.setattr(rollback, "_open_without_redirects", fail_request)
    client = ReleaseApiClient(
        repository="HomericIntelligence/Hephaestus",
        package="HomericIntelligence-Hephaestus",
        github_api_base="https://github.fixture",
        pypi_api_base="https://pypi.fixture",
    )

    with pytest.raises(ReleaseRollbackError, match=f"HTTP {status}"):
        client.get_release_by_tag("v1.2.3")
