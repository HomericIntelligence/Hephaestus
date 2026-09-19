"""Check the installed private artifact service with local test inputs."""

from __future__ import annotations

import http.client
import importlib.metadata
import ipaddress
import json
import os
import selectors
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hephaestus.automation import fleet_build_artifact_server
from hephaestus.automation.fleet_build_artifact_server import BuildArtifactServer
from tests.unit.automation.test_fleet_build_artifacts import private_file, retained_bundle, sha

pytestmark = [
    pytest.mark.integration,
    pytest.mark.precommit,
    pytest.mark.requires_posix,
    pytest.mark.skipif(
        os.name != "posix", reason="Private inputs require POSIX descriptor operations."
    ),
]

COMMAND = "hephaestus-fleet-build-artifacts"
FIXTURE_BEARER = "artifact-fixture-read-only"


def installed_command() -> Path:
    """Require the command from the installed package, without a PATH fallback."""
    entries = importlib.metadata.distribution("HomericIntelligence-Hephaestus").entry_points
    matches = [
        entry for entry in entries if entry.group == "console_scripts" and entry.name == COMMAND
    ]
    assert len(matches) == 1, "The installed package does not supply the artifact service command."
    command = Path(sys.executable).parent / COMMAND
    assert command.is_file(), "The installed artifact service executable is missing."
    return command


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_installed_command_does_not_require_private_inputs(flag: str, tmp_path: Path) -> None:
    """Supply help and version before configuration, credentials, or a listener."""
    result = subprocess.run(
        [str(installed_command()), flag],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout
    assert not result.stderr


def service_config(root: Path, host: str = "127.0.0.1") -> tuple[Path, dict[str, Any]]:
    """Create test-only trust and private inputs with no production authority."""
    root.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fleet test fixture")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2025, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2035, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    private_file(root / "certificate.pem", certificate.public_bytes(serialization.Encoding.PEM))
    private_file(
        root / "private-key.pem",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    private_file(root / "bearer", FIXTURE_BEARER.encode())
    registration = retained_bundle(root / "bundle")
    config = {
        "schema": "hi/hephaestus/build-artifact-service/v1",
        "host": host,
        "port": 0,
        "certificateFile": str(root / "certificate.pem"),
        "privateKeyFile": str(root / "private-key.pem"),
        "bearerFile": str(root / "bearer"),
        "registrations": [registration],
    }
    path = root / "service.json"
    private_file(path, json.dumps(config).encode())
    return path, registration


@contextmanager
def running_command(config: Path) -> Iterator[dict[str, Any]]:
    """Wait for explicit readiness and always stop the test-owned child."""
    process = subprocess.Popen(
        [str(installed_command()), "--config", str(config), "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=config.parent,
    )
    started = False
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5), "The service did not report readiness within five seconds."
            line = process.stdout.readline(4096)
        if not line:
            _, error = process.communicate(timeout=3)
            pytest.fail(f"The service did not start: exit {process.returncode}; {error.decode()}")
        ready = json.loads(line)
        assert ready["status"] == "ready"
        assert 1 <= ready["port"] <= 65535
        started = True
        yield ready
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            terminal, error = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
            pytest.fail("The artifact service did not stop within its shutdown deadline.")
        if started:
            assert process.returncode == 0, (
                f"The service shutdown failed: exit {process.returncode}; {error.decode()}"
            )
            assert [json.loads(line) for line in terminal.splitlines()] == [
                {"status": "ok", "exit_code": 0}
            ]


def read_page(
    ready: dict[str, Any],
    certificate: Path,
    registration: dict[str, Any],
    *,
    after: int = 0,
    limit: int = 65536,
    stream: str = "stdout",
    authorized: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Use verified local TLS and the exact Agamemnon backend request form."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(certificate))
    connection = http.client.HTTPSConnection(
        ready["host"], ready["port"], context=context, timeout=2
    )
    try:
        path = (
            f"/v1/fleet/build-jobs/{registration['buildId']}/logs?attempt={registration['attempt']}"
            f"&snapshotDigest={registration['snapshotDigest']}&stream={stream}&after={after}&limit={limit}"
        )
        headers = {"Authorization": f"Bearer {FIXTURE_BEARER}"} if authorized else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read(400001))
    finally:
        connection.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_installed_service_returns_authenticated_retained_log_pages(
    tmp_path: Path, host: str
) -> None:
    """Run actual TLS and return exact bytes for the registered terminal attempt."""
    config, registration = service_config(tmp_path, host)
    with running_command(config) as ready:
        status, denied = read_page(
            ready, tmp_path / "certificate.pem", registration, authorized=False
        )
        assert status == 401
        assert "Aλ" not in json.dumps(denied)
        status, page = read_page(ready, tmp_path / "certificate.pem", registration, limit=3)
        assert status == 200
        assert page == {
            "schema": "hi/fleet/build-logs/v1",
            "buildId": registration["buildId"],
            "attempt": 1,
            "snapshotDigest": registration["snapshotDigest"],
            "stream": "stdout",
            "after": 0,
            "next": 3,
            "data": "Aλ",
            "chunkDigest": sha("Aλ".encode()),
            "complete": False,
            "truncated": True,
            "manifest": registration["logs"],
        }


def trusted_socket(ready: dict[str, Any], certificate: Path) -> ssl.SSLSocket:
    """Connect to the test-owned listener with its explicit trust certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(certificate))
    raw = socket.create_connection((ready["host"], ready["port"]), timeout=2)
    try:
        return context.wrap_socket(raw, server_hostname=ready["host"])
    except BaseException:
        raw.close()
        raise


@contextmanager
def direct_service(
    config: Path,
    *,
    clock: Any = None,
) -> Iterator[tuple[BuildArtifactServer, dict[str, Any]]]:
    """Start an owned service and require its threads and listener to stop."""
    previous = set(threading.enumerate())
    service = BuildArtifactServer(config, **({"clock": clock} if clock is not None else {}))
    try:
        service.start()
        ready = {"host": service.host, "port": service.bound_port}
        yield service, ready
    finally:
        service.stop()
        assert service.bound_port == 0
        remaining = set(threading.enumerate()) - previous
        assert not [thread for thread in remaining if thread.name.startswith("FleetBuildArtifact")]


def request_target(registration: dict[str, Any]) -> str:
    """Supply the exact terminal query without implementation helpers."""
    return (
        f"/v1/fleet/build-jobs/{registration['buildId']}/logs?attempt=1"
        f"&snapshotDigest={registration['snapshotDigest']}&stream=stdout&after=0&limit=65536"
    )


def raw_status(connection: ssl.SSLSocket, request: bytes) -> int:
    """Read a bounded HTTP response from a test-owned TLS connection."""
    connection.sendall(request)
    response = http.client.HTTPResponse(connection)
    response.begin()
    assert response.getheader("Connection") == "close"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Server") is None
    body = response.read(400001)
    assert len(body) <= 400000
    if response.status != 200:
        assert json.loads(body) == {"error": "private_artifact_request_failed"}
    return response.status


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"target": "/unknown"}, 404, id="unknown-route"),
        pytest.param({"replace": [("build-fixture", "unknown-fixture")]}, 404, id="unknown-build"),
        pytest.param({"replace": [("&stream=stdout", "")]}, 400, id="missing-query"),
        pytest.param({"append": "&after=0"}, 400, id="duplicate-query"),
        pytest.param({"append": "&directory=unused"}, 400, id="extra-query"),
        pytest.param({"replace": [("attempt=1", "attempt=01")]}, 400, id="leading-zero"),
        pytest.param({"replace": [("after=0", "after=-1")]}, 400, id="negative-cursor"),
        pytest.param({"replace": [("after=0", "after=10")]}, 409, id="ahead-cursor"),
        pytest.param(
            {"replace": [("after=0", "after=3"), ("limit=65536", "limit=1")]}, 422, id="small-limit"
        ),
        pytest.param({"extra": "Content-Length: 1\r\n"}, 400, id="request-body"),
        pytest.param(
            {"auth": f"Authorization: Bearer {FIXTURE_BEARER}\r\n" * 2}, 401, id="duplicate-auth"
        ),
        pytest.param({"auth": "Authorization: Bearer different-fixture\r\n"}, 401, id="wrong-auth"),
        pytest.param({"method": "POST"}, 501, id="unsupported-method"),
        pytest.param(
            {"replace": [("build-fixture", "unknown-fixture")], "auth": ""},
            401,
            id="missing-auth-unknown-build",
        ),
    ],
)
def test_tls_request_contract(tmp_path: Path, change: dict[str, Any], expected: int) -> None:
    """Reject invalid requests with fixed public errors and no retained input."""
    config, registration = service_config(tmp_path)
    target = change.get("target", request_target(registration)) + change.get("append", "")
    for old, new in change.get("replace", []):
        target = target.replace(old, new)
    method = change.get("method", "GET")
    auth = change.get("auth", f"Authorization: Bearer {FIXTURE_BEARER}\r\n")
    extra = change.get("extra", "")
    with direct_service(config) as (_, ready):
        with trusted_socket(ready, tmp_path / "certificate.pem") as connection:
            request = f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}{extra}\r\n"
            assert raw_status(connection, request.encode()) == expected


@pytest.mark.parametrize("part", ["request-line", "headers"])
def test_total_request_limit_returns_fixed_error(tmp_path: Path, part: str) -> None:
    """Apply the 16 KiB bound to the complete request line and headers."""
    config, registration = service_config(tmp_path)
    request = (
        "GET /" + "x" * 16384 + " HTTP/1.1\r\n\r\n"
        if part == "request-line"
        else f"GET {request_target(registration)} HTTP/1.1\r\nX-Fixture: "
        + "x" * 16384
        + "\r\n\r\n"
    )
    with direct_service(config) as (_, ready):
        with trusted_socket(ready, tmp_path / "certificate.pem") as connection:
            assert raw_status(connection, request.encode()) == 431


def test_tls_requires_explicit_trust_and_ignores_ambient_key_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not use ambient TLS key logging or accept a client without trust."""
    config, _ = service_config(tmp_path)
    key_log = tmp_path / "not-created-key-log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    with direct_service(config) as (_, ready):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection((ready["host"], ready["port"]), timeout=2) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                context.wrap_socket(raw, server_hostname=ready["host"])
        with trusted_socket(ready, tmp_path / "certificate.pem"):
            pass
    assert not key_log.exists()


def test_literal_listener_does_not_resolve_a_hostname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start the selected literal-IP service without reverse hostname lookup."""
    config, registration = service_config(tmp_path)

    def unexpected_lookup(*_args: Any, **_kwargs: Any) -> str:
        pytest.fail("A literal-IP listener must not request a hostname lookup.")

    monkeypatch.setattr(socket, "getfqdn", unexpected_lookup)
    with direct_service(config) as (_, ready):
        status, _ = read_page(ready, tmp_path / "certificate.pem", registration)
        assert status == 200


def test_connection_capacity_deadline_and_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound admitted TLS peers and close them at one absolute deadline."""
    config, registration = service_config(tmp_path)
    current_time = [100.0]
    handshake_started = threading.Event()
    original_wrap = ssl.SSLContext.wrap_socket

    def observed_wrap(context: ssl.SSLContext, *args: Any, **kwargs: Any) -> ssl.SSLSocket:
        connection = original_wrap(context, *args, **kwargs)
        if kwargs.get("server_side"):
            handshake_started.set()
        return connection

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", observed_wrap)
    connections: list[ssl.SSLSocket] = []
    try:
        with direct_service(config, clock=lambda: current_time[0]) as (service, ready):
            for _ in range(8):
                connections.append(trusted_socket(ready, tmp_path / "certificate.pem"))
            handlers = [
                thread
                for thread in threading.enumerate()
                if thread.name == "FleetBuildArtifactRequest"
            ]
            with pytest.raises((OSError, ssl.SSLError)):
                trusted_socket(ready, tmp_path / "certificate.pem")
            connections[0].sendall(b"GET / HTTP/1.1\r\nHost: ")
            current_time[0] = 103.0
            for connection in connections:
                assert connection.recv(1) == b""
            deadline = time.monotonic() + 2
            for handler in handlers:
                handler.join(max(0, deadline - time.monotonic()))
                assert not handler.is_alive()
            status, _ = read_page(ready, tmp_path / "certificate.pem", registration)
            assert status == 200
            handshake_started.clear()
            with socket.create_connection((ready["host"], ready["port"]), timeout=2) as incomplete:
                assert handshake_started.wait(2)
                service.stop()
                # Darwin can report a reset when shutdown closes a pending connection.
                with suppress(ConnectionResetError):
                    assert incomplete.recv(1) == b""
            with pytest.raises(OSError):
                socket.create_connection((ready["host"], ready["port"]), timeout=1)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("field", ["service.json", "certificate.pem", "private-key.pem", "bearer"])
@pytest.mark.parametrize("use_json", [False, True])
def test_installed_command_rejects_nonprivate_input_without_readiness(
    tmp_path: Path, field: str, use_json: bool
) -> None:
    """Fail startup without exposing the rejected input or reporting readiness."""
    config, _ = service_config(tmp_path)
    (tmp_path / field).chmod(0o644)
    result = subprocess.run(
        [str(installed_command()), "--config", str(config), *(["--json"] if use_json else [])],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    if use_json:
        assert json.loads(result.stdout) == {"status": "error", "exit_code": 1}
    else:
        assert result.stdout == b""
    assert result.stderr == b"The private build-log service failed.\n"


@pytest.mark.parametrize("use_json", [False, True])
def test_shutdown_failure_has_fixed_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    use_json: bool,
) -> None:
    """Return a nonzero result without exposing a private shutdown exception."""

    class FailingStop:
        host = "127.0.0.1"
        bound_port = 12345

        def __init__(self, _path: Path) -> None:
            pass

        def start(self) -> None:
            signal.raise_signal(signal.SIGTERM)

        def stop(self) -> None:
            raise ValueError("private fixture detail must not be printed")

    monkeypatch.setattr(fleet_build_artifact_server, "BuildArtifactServer", FailingStop)
    arguments = ["--config", str(tmp_path / "unused"), *(["--json"] if use_json else [])]
    assert fleet_build_artifact_server.main(arguments) == 1
    output = capsys.readouterr()
    assert output.err == "The private build-log service failed.\n"
    assert "private fixture detail" not in output.out
    if use_json:
        assert [json.loads(line) for line in output.out.splitlines()] == [
            {"status": "ready", "host": "127.0.0.1", "port": 12345},
            {"status": "error", "exit_code": 1},
        ]
    else:
        assert output.out == "Private build-log service ready.\n"
