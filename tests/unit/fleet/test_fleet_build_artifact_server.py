"""Check the private build artifact service without a live socket or TLS peer."""

from __future__ import annotations

import io
import json
import os
import ssl
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hephaestus.fleet import build_artifact_server as service
from hephaestus.fleet.build_artifacts import ArtifactRequestError

pytestmark = [
    pytest.mark.precommit,
    pytest.mark.requires_posix,
    pytest.mark.skipif(
        os.name != "posix", reason="Private inputs require POSIX descriptor operations."
    ),
]

_DIGEST = "a" * 64
_VALID_TARGET = (
    "/v1/fleet/build-jobs/build-fixture/logs"
    f"?attempt=1&snapshotDigest={_DIGEST}&stream=stdout&after=0&limit=65536"
)


def _private(path: Path, data: bytes) -> None:
    """Write an owner-only fixture file."""
    path.write_bytes(data)
    path.chmod(0o600)


def _config(root: Path, **changes: Any) -> Path:
    """Make a valid private configuration file with optional overrides."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for name, data in (
        ("certificate.pem", b"certificate"),
        ("private-key.pem", b"private key"),
        ("bearer", b"fixture-token"),
    ):
        _private(root / name, data)
    config: dict[str, Any] = {
        "schema": "hi/hephaestus/build-artifact-service/v1",
        "host": "127.0.0.1",
        "port": 0,
        "certificateFile": str(root / "certificate.pem"),
        "privateKeyFile": str(root / "private-key.pem"),
        "bearerFile": str(root / "bearer"),
        "registrations": [],
    }
    config.update(changes)
    path = root / "service.json"
    _private(path, json.dumps(config).encode())
    return path


def _fake_tls(_certificate: bytes, _private_key: bytes) -> object:
    """Return an opaque TLS context because these tests do not bind a socket."""
    return object()


def test_query_accepts_the_documented_target() -> None:
    """Parse the exact bounded target into the catalog request fields."""
    assert service._query(_VALID_TARGET) == (
        "build-fixture",
        1,
        _DIGEST,
        "stdout",
        0,
        65536,
    )


@pytest.mark.parametrize(
    "target",
    [
        "/v1/fleet/build-jobs/build-fixture/other",
        "https://example.invalid" + _VALID_TARGET,
        "//example.invalid" + _VALID_TARGET,
        _VALID_TARGET + "#fragment",
    ],
)
def test_query_rejects_a_route_outside_the_contract(target: str) -> None:
    """Reject an unknown path, authority, scheme, or fragment as not found."""
    with pytest.raises(ArtifactRequestError) as error:
        service._query(target)
    assert error.value.status == 404


@pytest.mark.parametrize(
    "target",
    [
        _VALID_TARGET.replace("&stream=stdout", ""),
        _VALID_TARGET.replace("&after=0", "&after="),
        _VALID_TARGET.replace("&after=0", "&after=01"),
        _VALID_TARGET.replace("&after=0", "&after=-1"),
        _VALID_TARGET.replace("&after=0", "&after=1.0"),
    ],
)
def test_query_rejects_a_malformed_or_unbounded_value(target: str) -> None:
    """Reject a missing, duplicate, extra, or non-decimal field."""
    with pytest.raises(ArtifactRequestError) as error:
        service._query(target)
    assert error.value.status == 400


def test_query_rejects_malformed_query_syntax() -> None:
    """Let the strict parser reject a malformed query before catalog access."""
    with pytest.raises(ValueError):
        service._query(_VALID_TARGET.replace("attempt=1", "attempt"))


@pytest.mark.parametrize(
    "target",
    [
        _VALID_TARGET + "&extra=1",
        _VALID_TARGET.replace("&after=0", "&after=0&after=1"),
    ],
)
def test_query_rejects_more_than_the_field_bound(target: str) -> None:
    """Reject an extra or duplicate field before allocating the request."""
    with pytest.raises(ValueError):
        service._query(target)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["Bearer fixture-token"], True),
        ([], False),
        (["Bearer fixture-token", "Bearer fixture-token"], False),
        (["Bearer wrong"], False),
        (["Béarer fixture-token"], False),
    ],
)
def test_authorization_requires_one_exact_ascii_bearer(headers: list[str], expected: bool) -> None:
    """Accept only one exact ASCII bearer header."""
    assert service._authorized(headers, b"fixture-token") is expected


def test_header_reader_bounds_the_complete_request() -> None:
    """Apply the aggregate request bound before the HTTP parser allocates."""
    reader = service._HeaderReader(io.BytesIO(b"header\r\nbody\r\n"))
    assert reader.readline(7) == b"header\r"
    assert reader.readline() == b"\n"
    reader.close()


def test_header_reader_rejects_an_oversized_request() -> None:
    """Reject a request that exceeds the documented aggregate limit."""
    data = b"x" * (service._REQUEST_BYTES + 1)
    reader = service._HeaderReader(io.BytesIO(data))
    with pytest.raises(service._HeaderLimitError):
        reader.readline()
    reader.close()


def test_service_loads_a_valid_loopback_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load a valid configuration without binding a socket."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    loaded = service.BuildArtifactServer(_config(tmp_path))
    assert loaded.host == "127.0.0.1"
    assert loaded.bound_port == 0


def test_tls_context_uses_a_private_certificate_copy() -> None:
    """Load the supplied certificate without ambient trust or a key-log hook."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit fixture")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2025, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2035, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    context = service._tls_context(
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema": "other"}, "configuration is invalid"),
        ({"host": "0.0.0.0"}, "configuration is invalid"),
        ({"port": 65536}, "integer is outside its range"),
        ({"certificateFile": 1}, "configuration path is invalid"),
    ],
)
def test_service_rejects_an_invalid_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    message: str,
) -> None:
    """Reject an invalid schema, host, port, or private path."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    with pytest.raises(ValueError, match=message):
        service.BuildArtifactServer(_config(tmp_path, **changes))


@pytest.mark.parametrize("bearer", [b"", b"token\n", b"token\x00"])
def test_service_rejects_an_invalid_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bearer: bytes
) -> None:
    """Require a non-empty printable-ASCII bearer value."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    path = _config(tmp_path)
    (tmp_path / "bearer").write_bytes(bearer)
    (tmp_path / "bearer").chmod(0o600)
    with pytest.raises(ValueError, match="bearer value is invalid"):
        service.BuildArtifactServer(path)


def test_service_rejects_missing_configuration_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a configuration with a missing or extra field."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    path = _config(tmp_path)
    value = json.loads(path.read_text())
    value["extra"] = True
    _private(path, json.dumps(value).encode())
    with pytest.raises(ValueError, match="invalid fields"):
        service.BuildArtifactServer(path)


class _FakeServer:
    """Record the listener contract without accepting a socket."""

    label = "ipv4"

    def __init__(self, address: tuple[str, int], *_args: Any) -> None:
        self.server_address = (address[0], 12345)
        self.closed = False
        self.shutdown_called = False

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        del poll_interval

    def stop_connections(self) -> list[threading.Thread]:
        return []

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.closed = True


class _FakeIPv6Server(_FakeServer):
    label = "ipv6"


def test_service_selects_and_stops_the_configured_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select IPv4 or IPv6 without binding and stop idempotently."""
    recorded: list[str] = []

    class _IPv4(_FakeServer):
        def __init__(self, address: tuple[str, int], *args: Any) -> None:
            recorded.append("ipv4")
            super().__init__(address, *args)

    class _IPv6(_FakeIPv6Server):
        def __init__(self, address: tuple[str, int], *args: Any) -> None:
            recorded.append("ipv6")
            super().__init__(address, *args)

    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    monkeypatch.setattr(service, "_ArtifactHTTPServer", _IPv4)
    monkeypatch.setattr(service, "_IPv6ArtifactHTTPServer", _IPv6)

    ipv4 = service.BuildArtifactServer(_config(tmp_path / "v4"))
    ipv4.start()
    ipv4.start()
    assert ipv4.bound_port == 12345
    assert recorded == ["ipv4"]
    ipv4.stop()
    ipv4.stop()
    assert ipv4.bound_port == 0

    ipv6 = service.BuildArtifactServer(_config(tmp_path / "v6", host="::1"))
    ipv6.start()
    assert ipv6.bound_port == 12345
    assert recorded == ["ipv4", "ipv6"]
    ipv6.stop()


def test_service_start_failure_closes_the_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close a listener when its owner thread cannot start."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    closed: list[bool] = []

    class _BrokenThread:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    class _Server(_FakeServer):
        def server_close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(service, "_ArtifactHTTPServer", _Server)
    monkeypatch.setattr(cast(Any, service).threading, "Thread", _BrokenThread)
    loaded = service.BuildArtifactServer(_config(tmp_path))
    with pytest.raises(RuntimeError, match="thread start failed"):
        loaded.start()
    assert closed == [True]


def test_service_stop_reports_a_thread_that_does_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail when an owned handler outlives the shutdown deadline."""
    monkeypatch.setattr(service, "_tls_context", _fake_tls)
    monkeypatch.setattr(service, "_SHUTDOWN_SECONDS", 1.0)

    class _Clock:
        def monotonic(self) -> float:
            return 5.0

    monkeypatch.setattr(service, "time", _Clock())

    class _ForeverThread:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return True

        def join(self, _timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(service, "_ArtifactHTTPServer", _FakeServer)
    monkeypatch.setattr(cast(Any, service).threading, "Thread", _ForeverThread)
    loaded = service.BuildArtifactServer(_config(tmp_path))
    loaded.start()
    with pytest.raises(RuntimeError, match="did not stop"):
        loaded.stop()


class _Socket:
    """Record the owned connection cleanup contract."""

    def __init__(self, *, fail_shutdown: bool = False) -> None:
        self.closed = False
        self.fail_shutdown = fail_shutdown

    def shutdown(self, _how: int) -> None:
        if self.fail_shutdown:
            raise OSError("already closed")

    def close(self) -> None:
        self.closed = True

    def settimeout(self, _seconds: float) -> None:
        pass

    def do_handshake(self) -> None:
        pass


def _peer(connection: Any, deadline: float) -> Any:
    """Build the private peer record without a real socket type."""
    return service._Peer(connection, deadline)


class _Thread:
    """Stand in for an owned handler thread."""

    def __init__(
        self, target: Any = None, args: Any = (), name: str = "", daemon: bool = False
    ) -> None:
        del target, args, name, daemon
        self.alive = True

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive


def _bare_server() -> Any:
    """Create the server state without binding a real listener."""
    server: Any = object.__new__(service._ArtifactHTTPServer)
    server.catalog = object()
    server.bearer = b"token"
    server._context = object()
    server._clock = lambda: 10.0
    server._lock = threading.Lock()
    server._peers = {}
    server._threads = set()
    server._stopping = False
    return server


def test_server_process_request_admits_and_bounds_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start one owned handler and close requests beyond the connection bound."""
    server = _bare_server()
    monkeypatch.setattr(cast(Any, service).threading, "Thread", _Thread)
    admitted = _Socket()
    server.process_request(admitted, ("127.0.0.1", 1))
    assert len(server._threads) == 1
    assert len(server._peers) == 1

    rejected = _Socket()
    server._threads = {_Thread() for _ in range(service._CONNECTIONS)}
    server.process_request(rejected, ("127.0.0.1", 2))
    assert rejected.closed is True

    stopped = _Socket()
    server._stopping = True
    server.process_request(stopped, ("127.0.0.1", 3))
    assert stopped.closed is True


def test_server_process_request_closes_when_a_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the connection when the handler thread cannot start."""
    server = _bare_server()

    class _BrokenThread(_Thread):
        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(cast(Any, service).threading, "Thread", _BrokenThread)
    denied = _Socket()
    with pytest.raises(RuntimeError, match="thread start failed"):
        server.process_request(denied, ("127.0.0.1", 1))
    assert denied.closed is True
    assert server._threads == set()


def test_server_service_actions_closes_expired_peers() -> None:
    """Close a peer whose absolute deadline has expired."""
    server = _bare_server()
    expired = _Socket(fail_shutdown=True)
    thread = _Thread()
    thread.alive = False
    server._peers[thread] = _peer(expired, 5.0)
    server._threads = {thread}
    server.service_actions()
    assert expired.closed is True
    assert server._threads == set()


def test_server_stop_connections_closes_and_returns_threads() -> None:
    """Stop every owned connection and return the exact handler threads."""
    server = _bare_server()
    connection = _Socket()
    thread = _Thread()
    server._peers[thread] = _peer(connection, 20.0)
    server._threads = {thread}
    assert server.stop_connections() == [thread]
    assert server._stopping is True
    assert connection.closed is True


def test_server_run_peer_finishes_one_connection() -> None:
    """Complete a handshake and dispatch one request."""
    finished: list[Any] = []
    server = _bare_server()
    connection = _Socket()
    server._context = type(
        "Context",
        (),
        {"wrap_socket": lambda self, value, **_kwargs: value},
    )()
    server.finish_request = lambda value, _address: finished.append(value)
    peer = _peer(connection, 20.0)
    server._peers[threading.current_thread()] = peer
    server._run_peer(peer, ("127.0.0.1", 1))
    assert finished == [connection]
    assert connection.closed is True
    assert server._peers == {}


def test_server_run_peer_closes_after_a_tls_failure() -> None:
    """Close a peer when TLS setup or the handler fails."""
    server = _bare_server()

    class _FailingContext:
        def wrap_socket(self, _value: Any, **_kwargs: Any) -> Any:
            raise OSError("handshake failed")

    server._context = _FailingContext()
    connection = _Socket()
    peer = _peer(connection, 20.0)
    server._peers[threading.current_thread()] = peer
    server._run_peer(peer, ("127.0.0.1", 1))
    assert connection.closed is True
    assert server._peers == {}


def test_server_run_peer_closes_an_expired_peer() -> None:
    """Do not start a handshake after the absolute deadline."""
    server = _bare_server()
    connection = _Socket()
    peer = _peer(connection, 5.0)
    server._peers[threading.current_thread()] = peer
    server._run_peer(peer, ("127.0.0.1", 1))
    assert connection.closed is True
    assert server._peers == {}


class _Headers:
    """Supply the request headers used by the handler contract."""

    def __init__(
        self,
        *,
        authorization: list[str] | None = None,
        transfer_encoding: list[str] | None = None,
        content_length: list[str] | None = None,
    ) -> None:
        self.authorization = authorization if authorization is not None else ["Bearer token"]
        self.transfer_encoding = transfer_encoding or []
        self.content_length = content_length or []

    def get_all(self, name: str, default: Any = None) -> list[str]:
        if name == "Authorization":
            return self.authorization
        if name == "Transfer-Encoding":
            return self.transfer_encoding
        if name == "Content-Length":
            return self.content_length
        return default or []


class _Catalog:
    """Return or raise one catalog result."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error

    def page(self, *_args: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.result


def _handler(
    *,
    path: str = _VALID_TARGET,
    headers: _Headers | None = None,
    catalog: _Catalog | None = None,
) -> Any:
    """Create one handler with recorded response operations."""
    handler: Any = object.__new__(service._ArtifactHandler)
    handler.path = path
    handler.headers = headers or _Headers()
    handler.server = type(
        "Server",
        (),
        {"catalog": catalog or _Catalog(result={"ok": True}), "bearer": b"token"},
    )()
    handler.writes = []
    handler.errors = []
    handler.send_error = lambda status, message=None, explain=None: handler.errors.append(status)
    handler._write = lambda status, value: handler.writes.append((status, value))
    return handler


def test_handler_setup_wraps_the_request_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the aggregate header bound before the HTTP parser reads."""
    monkeypatch.setattr(cast(Any, service).BaseHTTPRequestHandler, "setup", lambda _self: None)
    handler: Any = object.__new__(service._ArtifactHandler)
    handler.rfile = io.BytesIO(b"header\r\n")
    handler.setup()
    assert isinstance(handler.rfile, service._HeaderReader)


def test_handler_handle_maps_an_oversized_request_to_431() -> None:
    """Return the fixed 431 response without leaking a parser exception."""
    handler: Any = object.__new__(service._ArtifactHandler)
    handler.errors = []
    handler.send_error = lambda status, message=None, explain=None: handler.errors.append(status)

    def _oversized() -> None:
        raise service._HeaderLimitError("too large")

    handler.handle_one_request = _oversized
    handler.handle()
    assert handler.request_version == "HTTP/1.0"
    assert handler.errors == [431]

    handler.handle_one_request = lambda: None
    handler.handle()
    assert handler.errors == [431]


def test_handler_write_sets_the_fixed_response_headers() -> None:
    """Write one bounded JSON response with no caching or connection reuse."""
    handler: Any = object.__new__(service._ArtifactHandler)
    handler.wfile = io.BytesIO()
    handler.close_connection = False
    statuses: list[int] = []
    headers: list[tuple[str, str]] = []
    handler.send_response_only = statuses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: None
    handler._write(200, {"ok": True})
    assert statuses == [200]
    assert handler.close_connection is True
    assert handler.wfile.getvalue() == b'{"ok":true}'
    assert dict(headers) == {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": "11",
        "Cache-Control": "no-store",
        "Connection": "close",
    }


def test_handler_send_error_uses_the_fixed_body() -> None:
    """Do not expose a parser or catalog detail in an error response."""
    handler: Any = object.__new__(service._ArtifactHandler)
    writes: list[tuple[int, dict[str, Any]]] = []
    handler._write = lambda status, value: writes.append((status, value))
    handler.send_error(400, "private", "private")
    assert writes == [(400, {"error": "private_artifact_request_failed"})]


@pytest.mark.parametrize(
    ("headers", "catalog", "expected"),
    [
        (_Headers(authorization=[]), _Catalog(), 401),
        (_Headers(transfer_encoding=["chunked"]), _Catalog(), 400),
        (_Headers(content_length=["1"]), _Catalog(), 400),
        (
            _Headers(),
            _Catalog(error=ArtifactRequestError(404)),
            404,
        ),
        (
            _Headers(),
            _Catalog(error=ArtifactRequestError(409)),
            409,
        ),
        (_Headers(), _Catalog(error=ValueError("bad value")), 400),
    ],
)
def test_handler_get_maps_request_failures(
    headers: _Headers, catalog: _Catalog, expected: int
) -> None:
    """Map authentication, framing, query, and catalog failures to fixed statuses."""
    handler = _handler(headers=headers, catalog=catalog)
    handler.do_GET()
    assert handler.errors == [expected]
    assert handler.writes == []


def test_handler_get_writes_an_authenticated_page() -> None:
    """Write one catalog page after the request checks pass."""
    handler = _handler(catalog=_Catalog(result={"data": "ok"}))
    handler.do_GET()
    assert handler.errors == []
    assert handler.writes == [(200, {"data": "ok"})]
    handler.log_message("ignored", 1)


def test_server_construction_stores_the_owned_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Store the catalog, bearer, TLS context, and clock without binding."""
    monkeypatch.setattr(cast(Any, service).TCPServer, "__init__", lambda *_args, **_kwargs: None)
    catalog = object()
    context = object()
    server = service._ArtifactHTTPServer(
        ("127.0.0.1", 0), cast(Any, catalog), b"token", cast(Any, context), lambda: 1.0
    )
    assert server.catalog is catalog
    assert server.bearer == b"token"
    assert server._context is context
    assert server._peers == {}
    assert server._threads == set()
    assert server._stopping is False


def test_server_service_actions_keeps_a_live_peer() -> None:
    """Do not close a peer before its absolute deadline."""
    server = _bare_server()
    connection = _Socket()
    thread = _Thread()
    server._peers[thread] = _peer(connection, 20.0)
    server._threads = {thread}
    server.service_actions()
    assert connection.closed is False


def test_server_run_peer_logs_a_non_tls_failure() -> None:
    """Close a peer whose handler raises a non-TLS failure."""
    server = _bare_server()

    class _FailingContext:
        def wrap_socket(self, _value: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("handler failed")

    server._context = _FailingContext()
    connection = _Socket()
    peer = _peer(connection, 20.0)
    server._peers[threading.current_thread()] = peer
    server._run_peer(peer, ("127.0.0.1", 1))
    assert connection.closed is True
    assert server._peers == {}


def test_run_reports_a_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a fixed failure and restore the signal handlers."""

    class _FailingService:
        def __init__(self, _path: Path) -> None:
            pass

        def start(self) -> None:
            raise ValueError("private detail")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(service, "BuildArtifactServer", _FailingService)
    result = service._run(["--config", str(tmp_path / "unused.json")])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert "Traceback" not in captured.err
    assert "private detail" not in captured.err
    first_failure = captured.err
    assert service._run(["--config", str(tmp_path / "unused.json")]) == 1
    assert capsys.readouterr().err == first_failure


@pytest.mark.parametrize("use_json", [False, True])
def test_run_reports_ready_then_stops_on_a_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    use_json: bool,
) -> None:
    """Report readiness, stop on a signal, and restore both handlers."""

    class _Signals:
        SIGINT = 2
        SIGTERM = 15

        def __init__(self) -> None:
            self.handlers: dict[int, Any] = {}

        def signal(self, number: int, handler: Any) -> Any:
            previous = self.handlers.get(number)
            self.handlers[number] = handler
            return previous

        def raise_signal(self, number: int) -> None:
            self.handlers[number](number, None)

    signals = _Signals()
    original_int = object()
    original_term = object()
    signals.handlers = {signals.SIGINT: original_int, signals.SIGTERM: original_term}
    created: list[Any] = []

    class _Service:
        def __init__(self, _path: Path) -> None:
            self.host = "127.0.0.1"
            self.bound_port = 12345
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            signals.raise_signal(signals.SIGTERM)

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(service, "signal", signals)
    monkeypatch.setattr(service, "BuildArtifactServer", _Service)
    arguments = ["--config", str(tmp_path / "unused.json")]
    if use_json:
        arguments.append("--json")
    result = service._run(arguments)
    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    if use_json:
        lines = captured.out.splitlines()
        assert json.loads(lines[0]) == {
            "status": "ready",
            "host": "127.0.0.1",
            "port": 12345,
        }
        assert json.loads(lines[-1]) == {"status": "ok", "exit_code": 0}
    else:
        assert captured.out == "Private build-log service ready.\n"
    assert created[0].stopped is True
    assert signals.handlers == {
        signals.SIGINT: original_int,
        signals.SIGTERM: original_term,
    }
