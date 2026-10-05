"""Reject a command when runtime health fails during a real socket read."""

from __future__ import annotations

import io
import json
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import fleet_worker_cli
from hephaestus.automation.fleet_worker_runtime import FleetWorkerRuntime
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP
from tests.unit.automation.test_fleet_worker import command
from tests.unit.automation.test_fleet_worker_runtime import (
    runtime_case as runtime_case,
)

pytestmark = pytest.mark.precommit


class HealthLostError(RuntimeError):
    """Stop this server after the controlled health failure."""


class FrameExchange:
    """Retain the real socket exchange and its frame-read health gates."""

    def __init__(self, socket_path: Path, frame: bytes) -> None:
        """Prepare one bounded exchange without opening a connection."""
        self.socket_path = socket_path
        self.frame = frame
        self.prefix = frame[:-1]
        self.ready = threading.Event()
        self.prefix_sent = threading.Event()
        self.prefix_read = threading.Event()
        self.frame_read = threading.Event()
        self.health_lost = threading.Event()
        self.consumed = bytearray()
        self.client_errors: list[BaseException] = []
        self.responses: list[bytes] = []
        self.health_observations: list[tuple[bool, bool]] = []
        self.observed_stream: socket.SocketIO | None = None
        self.deadline = time.monotonic() + 15

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Observe only the accepted stream while delegating its actual reads."""
        original_setup = socketserver.StreamRequestHandler.setup
        original_readinto = socket.SocketIO.readinto

        def observe_setup(handler: socketserver.StreamRequestHandler) -> None:
            """Retain the actual buffered reader for this server connection."""
            original_setup(handler)
            if handler.request.getsockname() == str(self.socket_path):
                assert isinstance(handler.rfile, io.BufferedReader)
                assert isinstance(handler.rfile.raw, socket.SocketIO)
                self.observed_stream = handler.rfile.raw

        def observe_readinto(stream: socket.SocketIO, buffer: Any) -> int | None:
            """Read actual bytes before acknowledging prefix and frame completion."""
            size = original_readinto(stream, buffer)
            if stream is self.observed_stream and size:
                self.consumed.extend(memoryview(buffer)[:size])
                if self.consumed == self.prefix:
                    self.prefix_read.set()
                if self.consumed == self.frame:
                    self.frame_read.set()
            return size

        monkeypatch.setattr(socketserver.StreamRequestHandler, "setup", observe_setup)
        monkeypatch.setattr(socket.SocketIO, "readinto", observe_readinto)

    def check_health(self) -> None:
        """Reject admission after the client reports health loss during its frame."""
        if time.monotonic() >= self.deadline:
            raise AssertionError("the socket health test did not stop")
        if not self.ready.is_set():
            self.ready.set()
            assert self.prefix_sent.wait(5), "the client did not send the JSON prefix"
        self.health_observations.append((self.health_lost.is_set(), self.frame_read.is_set()))
        if self.health_lost.is_set():
            raise HealthLostError("runtime health failed during frame read")

    def client(self) -> None:
        """Send the newline only after the real server has consumed the prefix."""
        try:
            assert self.ready.wait(5), "the server did not become ready"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(str(self.socket_path))
                connection.sendall(self.prefix)
                self.prefix_sent.set()
                assert self.prefix_read.wait(5), "the server did not read the JSON prefix"
                # The server must wait for the newline after its first healthy check.
                self.health_lost.set()
                connection.sendall(b"\n")
                with connection.makefile("rb") as stream:
                    self.responses.append(stream.readline(fleet_worker_cli._MAX_MESSAGE + 1))
        except BaseException as error:
            self.client_errors.append(error)

    def release(self) -> None:
        """Release every client gate before its owning test joins the thread."""
        self.ready.set()
        self.prefix_sent.set()
        self.prefix_read.set()


def test_worker_cli_checks_health_after_reading_the_command_frame(
    runtime_case: tuple[FleetWorkerRuntime, BuildConsumerHTTP],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the journal unchanged when health fails before the final newline."""
    assert threading.current_thread() is threading.main_thread()
    # The factory fixture uses a short private build path and production storage checks.
    runtime, http = runtime_case
    runtime.start()
    worker, journal = runtime.worker, runtime.journal
    assert worker is not None and journal is not None and worker.journal is journal
    message = command("drain", target="worker-a", number=981)
    message["targetKind"] = "workers"
    frame = (json.dumps(message) + "\n").encode()
    socket_path = journal.directory / "worker.sock"
    journal_path = journal.directory / "receipts.jsonl"
    before = journal_path.read_bytes()
    assert journal.snapshot()["draining"] is False

    exchange = FrameExchange(socket_path, frame)
    exchange.install(monkeypatch)
    client_thread = threading.Thread(target=exchange.client, name="fleet-frame-health-client")
    client_thread.start()
    try:
        # serve installs process signal handlers and must use the main test thread.
        with pytest.raises(HealthLostError, match="runtime health failed during frame read"):
            fleet_worker_cli.serve(worker, check_health=exchange.check_health)
    finally:
        exchange.release()
        client_thread.join(timeout=6)
        assert not client_thread.is_alive(), "the socket client did not exit"

    assert exchange.client_errors == []
    assert exchange.consumed == frame
    assert exchange.prefix_read.is_set() and exchange.frame_read.is_set()
    assert exchange.health_observations[0] == (False, False)
    assert (True, True) in exchange.health_observations
    assert exchange.responses
    if exchange.responses[0]:
        assert json.loads(exchange.responses[0]).get("status") != "completed"
    assert journal_path.read_bytes() == before
    snapshot = journal.snapshot()
    assert snapshot["draining"] is False
    assert message["idempotencyKey"] not in snapshot["commands"]
    assert not socket_path.exists()

    # The same real worker must accept this command when no health failure intervenes.
    accepted = worker.handle(message)
    assert accepted["status"] == "completed"
    assert journal.snapshot()["draining"] is True
    assert http.requests == []
