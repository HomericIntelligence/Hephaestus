"""Subprocess rehearsal of immutable-release withdrawal against loopback APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast
from urllib.parse import urlparse

import pytest

from hephaestus.ci.release_rollback import ReleaseApiClient, ReleaseRollbackError

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_PATH = "/repos/HomericIntelligence/Hephaestus/releases/tags/v1.2.3"
TAG_PATH = "/repos/HomericIntelligence/Hephaestus/git/ref/tags/v1.2.3"
PYPI_PATH = "/pypi/HomericIntelligence-Hephaestus/1.2.3/json"


def _release() -> dict[str, Any]:
    """Return a representative immutable GitHub release API response."""
    return {
        "id": 42,
        "immutable": True,
        "body": "Original release notes.",
        "assets": [
            {
                "id": 100,
                "name": "hephaestus-1.2.3-py3-none-any.whl",
                "size": 1024,
                "digest": "sha256:asset",
            }
        ],
    }


@dataclass
class ReleaseFixture:
    """Mutable HTTP state used to rehearse one withdrawal transaction."""

    release: dict[str, Any] = field(default_factory=_release)
    pypi_yanked: bool = True
    server_error: bool = False
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    patch_payloads: list[dict[str, Any]] = field(default_factory=list)


class ReleaseFixtureServer(ThreadingHTTPServer):
    """Threading server carrying the mutable rollback fixture state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        fixture: ReleaseFixture,
    ) -> None:
        super().__init__(server_address, request_handler)
        self.fixture = fixture


class ReleaseFixtureHandler(BaseHTTPRequestHandler):
    """Serve the minimal GitHub and PyPI endpoints used by the checked-in CLI."""

    protocol_version = "HTTP/1.1"

    @property
    def fixture(self) -> ReleaseFixture:
        """Return state attached to the typed loopback server."""
        return cast(ReleaseFixtureServer, self.server).fixture

    def do_GET(self) -> None:
        """Serve release tag, release state, and PyPI yank metadata."""
        path = urlparse(self.path).path
        self._record("GET", path)
        if self.fixture.server_error:
            self._write_json(500, {"message": "fixture server failure"})
        elif path == TAG_PATH:
            self._write_json(200, {"ref": "refs/tags/v1.2.3", "object": {"sha": "abc"}})
        elif path == RELEASE_PATH:
            self._write_json(200, self.fixture.release)
        elif path == PYPI_PATH:
            self._write_json(
                200,
                {
                    "urls": [
                        {"filename": "hephaestus-1.2.3.tar.gz", "yanked": self.fixture.pypi_yanked},
                        {
                            "filename": "hephaestus-1.2.3-py3-none-any.whl",
                            "yanked": self.fixture.pypi_yanked,
                        },
                    ]
                },
            )
        else:
            self._write_json(404, {"message": "not found"})

    def do_PATCH(self) -> None:
        """Accept the release-note-only update used by the withdrawal command."""
        path = urlparse(self.path).path
        self._record("PATCH", path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            self._write_json(400, {"message": "body must be an object"})
            return
        self.fixture.patch_payloads.append(payload)
        if path != "/repos/HomericIntelligence/Hephaestus/releases/42":
            self._write_json(404, {"message": "not found"})
            return
        if set(payload) != {"body"} or not isinstance(payload["body"], str):
            self._write_json(400, {"message": "only release body may change"})
            return
        self.fixture.release["body"] = payload["body"]
        self._write_json(200, self.fixture.release)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep subprocess-test output focused on command failures."""

    def _record(self, method: str, path: str) -> None:
        """Record request metadata needed by the rehearsal assertions."""
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.fixture.requests.append((method, path, headers))

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        """Write a JSON response with explicit length for persistent connections."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RedirectFixtureServer(ThreadingHTTPServer):
    """Loopback server that redirects one authenticated GitHub API request."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        redirect_target: str,
    ) -> None:
        super().__init__(server_address, request_handler)
        self.redirect_target = redirect_target


class RedirectFixtureHandler(BaseHTTPRequestHandler):
    """Redirect every request to a separately observed loopback origin."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Return a cross-origin redirect without a response body."""
        target = cast(RedirectFixtureServer, self.server).redirect_target
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Keep redirect-rehearsal output focused on assertion failures."""


@pytest.fixture
def rollback_fixture() -> Iterator[tuple[ReleaseFixture, str]]:
    """Start a loopback GitHub/PyPI fixture and yield its API base URL."""
    fixture = ReleaseFixture()
    try:
        server = ReleaseFixtureServer(("127.0.0.1", 0), ReleaseFixtureHandler, fixture)
    except PermissionError as error:
        pytest.skip(f"loopback sockets are unavailable in this environment: {error}")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    try:
        yield fixture, f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_rollback(api_base: str) -> subprocess.CompletedProcess[str]:
    """Run the checked-in rollback wrapper through its public CLI contract."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "release_rollback.py"),
            "rollback",
            "--tag",
            "v1.2.3",
            "--reason",
            "fixture regression",
            "--apply",
            "--confirm-tag",
            "v1.2.3",
            "--github-api-base",
            api_base,
            "--pypi-api-base",
            api_base,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "GH_TOKEN": "fixture-token"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_rollback_rehearsal_patches_only_notes_and_preserves_assets(
    rollback_fixture: tuple[ReleaseFixture, str],
) -> None:
    """The real CLI performs a post-write read and leaves immutable assets intact."""
    fixture, api_base = rollback_fixture
    original_assets = deepcopy(fixture.release["assets"])

    result = _run_rollback(api_base)

    assert result.returncode == 0, result.stderr
    assert len(fixture.patch_payloads) == 1
    assert set(fixture.patch_payloads[0]) == {"body"}
    assert "<!-- hephaestus-release-withdrawn -->" in fixture.release["body"]
    assert fixture.release["body"].endswith("Original release notes.")
    assert fixture.release["assets"] == original_assets

    methods_and_paths = [(method, path) for method, path, _ in fixture.requests]
    assert methods_and_paths == [
        ("GET", TAG_PATH),
        ("GET", RELEASE_PATH),
        ("GET", PYPI_PATH),
        ("PATCH", "/repos/HomericIntelligence/Hephaestus/releases/42"),
        ("GET", RELEASE_PATH),
    ]
    github_requests = [request for request in fixture.requests if request[1] != PYPI_PATH]
    for _, _, headers in github_requests:
        assert headers["authorization"] == "Bearer fixture-token"


def test_rollback_rehearsal_refuses_non_yanked_pypi_release(
    rollback_fixture: tuple[ReleaseFixture, str],
) -> None:
    """A partially yanked PyPI release prevents the GitHub mutation."""
    fixture, api_base = rollback_fixture
    fixture.pypi_yanked = False

    result = _run_rollback(api_base)

    assert result.returncode != 0
    assert "PyPI release must be yanked" in result.stderr
    assert fixture.patch_payloads == []


def test_rollback_rehearsal_fails_closed_on_server_error(
    rollback_fixture: tuple[ReleaseFixture, str],
) -> None:
    """A 5xx response leaves the immutable release unmodified."""
    fixture, api_base = rollback_fixture
    fixture.server_error = True

    result = _run_rollback(api_base)

    assert result.returncode != 0
    assert "HTTP 500" in result.stderr
    assert fixture.patch_payloads == []


def test_github_redirect_fails_closed_without_forwarding_token() -> None:
    """Authenticated GitHub reads must not follow a redirect to another origin."""
    target_fixture = ReleaseFixture()
    servers: list[ThreadingHTTPServer] = []
    try:
        target_server = ReleaseFixtureServer(
            ("127.0.0.1", 0), ReleaseFixtureHandler, target_fixture
        )
        servers.append(target_server)
        target_host = str(target_server.server_address[0])
        target_port = int(target_server.server_address[1])
        redirect_server = RedirectFixtureServer(
            ("127.0.0.1", 0),
            RedirectFixtureHandler,
            f"http://{target_host}:{target_port}{RELEASE_PATH}",
        )
        servers.append(redirect_server)
    except PermissionError as error:
        for server in servers:
            server.server_close()
        pytest.skip(f"loopback sockets are unavailable in this environment: {error}")

    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    redirect_host = str(redirect_server.server_address[0])
    redirect_port = int(redirect_server.server_address[1])
    client = ReleaseApiClient(
        repository="HomericIntelligence/Hephaestus",
        package="HomericIntelligence-Hephaestus",
        github_api_base=f"http://{redirect_host}:{redirect_port}",
        pypi_api_base="https://pypi.fixture",
        token="fixture-token",  # noqa: S106 - non-secret local fixture value
    )
    try:
        with pytest.raises(ReleaseRollbackError, match="HTTP 302"):
            client.get_release_by_tag("v1.2.3")
        assert target_fixture.requests == []
    finally:
        for server in servers:
            server.shutdown()
        for thread in threads:
            thread.join(timeout=5)
        for server in servers:
            server.server_close()
