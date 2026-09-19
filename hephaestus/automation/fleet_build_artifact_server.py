"""Serve private retained Fleet build logs through a local TLS connection."""

from __future__ import annotations

import argparse
import hmac
import io
import logging
import re
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from hephaestus.automation.fleet_build_artifacts import (
    MAX_CONFIGURATION_BYTES,
    ArtifactCatalog,
    ArtifactRequestError,
    canonical,
    decode_object,
    fields,
    integer,
    read_private_file,
)
from hephaestus.cli.localization import text
from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status

logger = logging.getLogger(__name__)
_CONNECTIONS = 8
_REQUEST_BYTES = 16 * 1024
_CONNECTION_SECONDS = 2.0
_SHUTDOWN_SECONDS = 3.0
_ROUTE = re.compile(r"/v1/fleet/build-jobs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/logs")


def _tls_context(certificate: bytes, private_key: bytes) -> ssl.SSLContext:
    """Load checked private copies without ambient trust, key logging, or a prompt."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="hephaestus-artifact-tls-") as temporary:
        root = Path(temporary)
        for name, data in (("certificate.pem", certificate), ("private-key.pem", private_key)):
            path = root / name
            path.write_bytes(data)
            path.chmod(0o600)
        context.load_cert_chain(
            root / "certificate.pem", root / "private-key.pem", password=lambda: b""
        )
    return context


@dataclass
class _Peer:
    connection: socket.socket
    deadline: float


class _HeaderLimitError(ValueError):
    pass


class _HeaderReader(io.BufferedIOBase):
    """Apply an aggregate bound before the standard HTTP parser allocates headers."""

    def __init__(self, source: Any) -> None:
        super().__init__()
        self._source = source
        self._remaining = _REQUEST_BYTES

    def readline(self, size: int | None = -1) -> bytes:
        limit = self._remaining + 1
        if size is not None and size >= 0:
            limit = min(limit, size)
        data: bytes = self._source.readline(limit)
        self._remaining -= len(data)
        if self._remaining < 0:
            raise _HeaderLimitError("The request exceeds its byte limit.")
        return data

    def close(self) -> None:
        self._source.close()
        super().close()


def _query(target: str) -> tuple[str, int, str, str, int, int]:
    parsed = urlsplit(target)
    match = _ROUTE.fullmatch(parsed.path)
    if match is None or parsed.scheme or parsed.netloc or parsed.fragment:
        raise ArtifactRequestError(404)
    pairs = parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=True,
        encoding="ascii",
        errors="strict",
        max_num_fields=5,
    )
    values = dict(pairs)
    if len(values) != len(pairs) or set(values) != {
        "attempt",
        "snapshotDigest",
        "stream",
        "after",
        "limit",
    }:
        raise ArtifactRequestError(400)
    for name in ("attempt", "after", "limit"):
        if re.fullmatch(r"0|[1-9][0-9]{0,18}", values[name]) is None:
            raise ArtifactRequestError(400)
    return (
        match[1],
        int(values["attempt"]),
        values["snapshotDigest"],
        values["stream"],
        int(values["after"]),
        int(values["limit"]),
    )


def _authorized(authorizations: list[str], bearer: bytes) -> bool:
    try:
        candidate = authorizations[0].encode("ascii") if len(authorizations) == 1 else b""
    except UnicodeError:
        candidate = b""
    return hmac.compare_digest(candidate, b"Bearer " + bearer)


class _ArtifactHandler(BaseHTTPRequestHandler):
    """Serve one authenticated read with fixed errors and no request logging."""

    server: _ArtifactHTTPServer

    def setup(self) -> None:
        super().setup()
        self.rfile = _HeaderReader(self.rfile)

    def handle(self) -> None:
        # The aggregate limit can fail before the HTTP parser sets this field.
        self.request_version = "HTTP/1.0"
        try:
            self.handle_one_request()
        except _HeaderLimitError:
            self.send_error(431)

    def _write(self, status: int, value: dict[str, Any]) -> None:
        data = canonical(value)
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        self._write(code, {"error": "private_artifact_request_failed"})

    def do_GET(self) -> None:
        if not _authorized(self.headers.get_all("Authorization", []), self.server.bearer):
            self.send_error(401)
            return
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all(
            "Content-Length", []
        ) not in ([], ["0"]):
            self.send_error(400)
            return
        try:
            page = self.server.catalog.page(*_query(self.path))
        except ArtifactRequestError as error:
            self.send_error(error.status)
        except (ValueError, UnicodeError):
            self.send_error(400)
        else:
            self._write(200, page)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _ArtifactHTTPServer(TCPServer):
    """Bound raw connections before TLS and enforce their absolute lifetime."""

    request_queue_size = _CONNECTIONS
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        catalog: ArtifactCatalog,
        bearer: bytes,
        context: ssl.SSLContext,
        clock: Callable[[], float],
    ) -> None:
        self.catalog = catalog
        self.bearer = bearer
        self._context = context
        self._clock = clock
        self._lock = threading.Lock()
        self._peers: dict[threading.Thread, _Peer] = {}
        self._threads: set[threading.Thread] = set()
        self._stopping = False
        super().__init__(address, _ArtifactHandler)

    @staticmethod
    def _close(connection: socket.socket) -> None:
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        connection.close()

    def process_request(self, request: Any, client_address: Any) -> None:
        peer = _Peer(request, self._clock() + _CONNECTION_SECONDS)
        with self._lock:
            self._threads = {thread for thread in self._threads if thread.is_alive()}
            if self._stopping or len(self._threads) >= _CONNECTIONS:
                self._close(request)
                return
            thread = threading.Thread(
                target=self._run_peer,
                args=(peer, client_address),
                name="FleetBuildArtifactRequest",
                daemon=True,
            )
            self._peers[thread] = peer
            self._threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._peers.pop(thread)
                self._threads.remove(thread)
                self._close(request)
                raise

    def _run_peer(self, peer: _Peer, client_address: Any) -> None:
        try:
            with self._lock:
                if self._stopping or self._clock() >= peer.deadline:
                    return
                connection = self._context.wrap_socket(
                    peer.connection, server_side=True, do_handshake_on_connect=False
                )
                peer.connection = connection
                connection.settimeout(max(0.001, peer.deadline - self._clock()))
            connection.do_handshake()
            self.finish_request(connection, client_address)
        except (OSError, ValueError):
            logger.debug("A private artifact connection ended before completion.")
        except Exception:
            logger.error("A private artifact connection failed.")
        finally:
            with self._lock:
                self._close(peer.connection)
                self._peers.pop(threading.current_thread(), None)

    def service_actions(self) -> None:
        now = self._clock()
        with self._lock:
            for peer in self._peers.values():
                if now >= peer.deadline:
                    self._close(peer.connection)
            self._threads = {thread for thread in self._threads if thread.is_alive()}

    def stop_connections(self) -> list[threading.Thread]:
        """Close every owned connection and return the exact handler threads."""
        with self._lock:
            self._stopping = True
            for peer in self._peers.values():
                self._close(peer.connection)
            return list(self._threads)


class _IPv6ArtifactHTTPServer(_ArtifactHTTPServer):
    address_family = socket.AF_INET6


class BuildArtifactServer:
    """Load a private configuration and explicitly start or stop its TLS service."""

    def __init__(self, config_path: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Validate all private inputs without binding a network socket."""
        config = fields(
            decode_object(read_private_file(config_path, MAX_CONFIGURATION_BYTES)),
            "schema host port certificateFile privateKeyFile bearerFile registrations",
        )
        if config["schema"] != "hi/hephaestus/build-artifact-service/v1" or config["host"] not in (
            "127.0.0.1",
            "::1",
        ):
            raise ValueError("The artifact service configuration is invalid.")
        self.host: str = config["host"]
        self._port = integer(config["port"], 65535)
        inputs: dict[str, bytes] = {}
        for name in ("certificateFile", "privateKeyFile", "bearerFile"):
            if not isinstance(config[name], str):
                raise ValueError("The private configuration path is invalid.")
            maximum = 4096 if name == "bearerFile" else MAX_CONFIGURATION_BYTES
            inputs[name] = read_private_file(Path(config[name]), maximum)
        bearer = inputs["bearerFile"]
        if not bearer or any(byte <= 32 or byte >= 127 for byte in bearer):
            raise ValueError("The private bearer value is invalid.")
        self._catalog = ArtifactCatalog(config["registrations"])
        self._bearer = bearer
        self._context = _tls_context(inputs["certificateFile"], inputs["privateKeyFile"])
        self._clock = clock
        self._server: _ArtifactHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        """Return the assigned listener port, or zero while stopped."""
        return int(self._server.server_address[1]) if self._server is not None else 0

    def start(self) -> None:
        """Bind the explicit loopback address and start the bounded accept loop."""
        if self._server is not None:
            return
        server_type = _IPv6ArtifactHTTPServer if self.host == "::1" else _ArtifactHTTPServer
        server = server_type(
            (self.host, self._port), self._catalog, self._bearer, self._context, self._clock
        )
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.025},
            name="FleetBuildArtifactListener",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server, self._thread = server, thread

    def stop(self) -> None:
        """Close the listener and connections, then require owned threads to stop."""
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        deadline = time.monotonic() + _SHUTDOWN_SECONDS
        workers = server.stop_connections()
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        for worker in [thread, *workers]:
            worker.join(max(0.0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in [thread, *workers]):
            raise RuntimeError("The artifact service did not stop within its deadline.")
        self._server, self._thread = None, None


def main(argv: list[str] | None = None) -> int:
    """Run the private service until an explicit interrupt or termination signal."""
    parser = argparse.ArgumentParser(description=text(__doc__ or ""))
    add_json_arg(parser)
    add_version_arg(parser)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=text("Read the private service configuration from this file."),
    )
    args = parser.parse_args(argv)
    service: BuildArtifactServer | None = None
    stopped = threading.Event()
    previous: dict[int, Any] = {}
    result = 0

    def stop(_number: int, _frame: Any) -> None:
        stopped.set()

    try:
        service = BuildArtifactServer(args.config)
        previous = {
            number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)
        }
        service.start()
        ready = {"status": "ready", "host": service.host, "port": service.bound_port}
        print(
            canonical(ready).decode() if args.json else text("Private build-log service ready."),
            flush=True,
        )
        stopped.wait()
    except (OSError, ValueError, RuntimeError):
        print(text("The private build-log service failed."), file=sys.stderr)
        result = 1
    finally:
        try:
            if service is not None:
                service.stop()
        except (OSError, ValueError, RuntimeError):
            print(text("The private build-log service failed."), file=sys.stderr)
            result = 1
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    if args.json:
        emit_json_status(result)
    return result
