"""Check private attachment with real Unix sockets and a fixed pipe process."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.automation.test_fleet_containment import Engine, specification, supervisor

pytestmark = pytest.mark.precommit


@pytest.fixture
def tmp_path():
    """Keep actual Unix socket paths below the macOS path-length limit."""
    with tempfile.TemporaryDirectory(prefix="hf-", dir="/tmp") as directory:
        yield Path(directory).resolve()


class PipeEngine(Engine):
    """Replace only the engine with a fixed echo process; no container is implied."""

    def __init__(self) -> None:
        super().__init__()
        self.process = None

    def attach(self, container_id):
        super().attach(container_id)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-c",
                "import sys\nfor line in sys.stdin.buffer:\n"
                " sys.stdout.buffer.write(line); sys.stdout.buffer.flush()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return self.process


@contextmanager
def endpoint(tmp_path):
    """Use the real durable supervisor with explicit engine/kernel substitutes."""
    from hephaestus.automation.fleet_attachment import AttachmentEndpoint

    engine = PipeEngine()
    owner = supervisor(tmp_path, engine=engine)
    lease = owner.create(specification(tmp_path))
    server = AttachmentEndpoint(owner, lease["leaseId"])
    try:
        yield owner, server, engine
    finally:
        server.close()
        if engine.process is not None:
            if engine.process.poll() is None:
                engine.process.terminate()
            engine.process.wait(timeout=2)
        owner.close()


def connect(server, **changes):
    """Send the bounded binding handshake over the actual private socket."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(server.path))
    request = {
        "schema": "hi/fleet/attachment/v1",
        "leaseId": server.lease_id,
        "bindingDigest": server.binding_digest,
        **changes,
    }
    client.sendall(json.dumps(request).encode() + b"\n")
    response = bytearray()
    while not response.endswith(b"\n"):
        response.extend(client.recv(1))
    return client, json.loads(response)


def test_attachment_validates_actual_supervisor_before_forwarding(tmp_path):
    """The pipe becomes available only after the durable active/kernel record."""
    with endpoint(tmp_path) as (owner, server, engine):
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client, result = connect(server)
        try:
            assert result == {"status": "attached"}
            assert owner.inspect(server.lease_id)["phase"] == "active"
            assert "capture" in engine.calls
            client.sendall(b'{"synthetic":"marker"}\n')
            assert client.recv(1024) == b'{"synthetic":"marker"}\n'
            client.shutdown(socket.SHUT_WR)
            assert client.recv(1024) == b""
        finally:
            client.close()
            thread.join(timeout=3)
        assert not thread.is_alive()
        assert owner.inspect(server.lease_id)["phase"] == "active"
        assert "remove" not in engine.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"bindingDigest": "0" * 64},
        {"leaseId": "0" * 32},
        {"schema": "other"},
        {"argv": ["anything"]},
    ],
)
def test_invalid_binding_never_starts_a_process(tmp_path, changes):
    """A client cannot supply a command, replace a lease, or reuse another owner."""
    with endpoint(tmp_path) as (owner, server, engine):
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client, result = connect(server, **changes)
        client.close()
        thread.join(timeout=3)
        assert result["status"] == "rejected"
        assert "attach" not in engine.calls
        assert owner.inspect(server.lease_id)["phase"] == "created"


def test_uncertain_kernel_observation_never_exposes_process_stream(tmp_path):
    """Failed boundary observation leaves the lease reserved without a usable stream."""
    with endpoint(tmp_path) as (owner, server, _engine):
        owner.kernel.fail_capture = True
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client, result = connect(server)
        client.close()
        thread.join(timeout=3)
        assert result["status"] == "rejected"
        assert owner.inspect(server.lease_id)["phase"] == "uncertain"


def test_retained_binding_cannot_attach_after_owner_changes(tmp_path):
    """Compare the retained engine and assignment binding again before a start."""
    with endpoint(tmp_path) as (owner, server, engine):
        owner.leases[server.lease_id]["spec"]["generation"] = 2
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client, result = connect(server)
        client.close()
        thread.join(timeout=3)
        assert result["status"] == "rejected"
        assert "attach" not in engine.calls


def test_endpoint_is_private_and_does_not_replace_retained_socket(tmp_path):
    """A second owner cannot unlink the first owner's listener."""
    from hephaestus.automation.fleet_attachment import AttachmentEndpoint

    with endpoint(tmp_path) as (owner, server, _engine):
        assert server.path.parent == owner.journal.directory.resolve()
        assert server.path.stat().st_mode & 0o777 == 0o600
        with pytest.raises((FileExistsError, OSError, ValueError)):
            AttachmentEndpoint(owner, server.lease_id)


def test_installed_style_client_relays_only_the_owned_stream(tmp_path):
    """Exercise the program transport entry module and its actual process pipes."""
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry

    with endpoint(tmp_path) as (owner, server, _engine):
        home = tmp_path / "codex-home"
        home.mkdir(mode=0o700)
        lease = EnvironmentLease.from_endpoint(server, "session-1-env", Path(sys.executable))
        registry = EnvironmentRegistry(home, [lease])
        program = tomllib.loads(registry.write_configuration().read_text())["environments"][0]
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        try:
            result = subprocess.run(
                [program["program"], *program["args"]],
                input=b"fixed client marker\n",
                capture_output=True,
                timeout=5,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == b"fixed client marker\n"
            assert owner.inspect(server.lease_id)["phase"] == "active"
        finally:
            thread.join(timeout=3)
        assert not thread.is_alive()


def test_client_drains_remote_eof_without_waiting_for_provider_stdin(tmp_path):
    """A closed remote stream terminates the client while provider input remains open."""
    path = tmp_path / "endpoint.sock"
    request = {
        "schema": "hi/fleet/attachment/v1",
        "leaseId": "1" * 32,
        "bindingDigest": "2" * 64,
    }
    errors = []
    payload = b"final bounded remote response\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        listener.settimeout(3)

        def respond():
            try:
                channel, _ = listener.accept()
                with channel:
                    channel.settimeout(3)
                    with channel.makefile("rb") as stream:
                        assert json.loads(stream.readline(1024)) == request
                    channel.sendall(b'{"status":"attached"}\n' + payload)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=respond)
        thread.start()
        client = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hephaestus.automation.fleet_attachment",
                "--socket",
                str(path),
                "--lease-id",
                request["leaseId"],
                "--binding-digest",
                request["bindingDigest"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert client.wait(timeout=3) == 0
            assert client.stdin is not None and not client.stdin.closed
            assert client.stdout.read() == payload
            assert client.stderr.read() == b""
        finally:
            if client.poll() is None:
                client.kill()
            client.wait(timeout=2)
            for stream in (client.stdin, client.stdout, client.stderr):
                stream.close()
            thread.join(timeout=4)
        assert not thread.is_alive()
        assert errors == []


@pytest.mark.parametrize(
    "field",
    [
        "worker_id",
        "session_id",
        "execution_id",
        "generation",
        "workspace",
        "container_id",
        "image_digest",
    ],
)
def test_registry_assignment_cannot_select_another_endpoints_stream(tmp_path, field):
    """Reject a declared owner that differs from the actual endpoint before attach."""
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry

    with endpoint(tmp_path) as (owner, server, engine):
        other_workspace = tmp_path / "other-workspace"
        other_workspace.mkdir()
        changed = {
            "worker_id": "other-worker",
            "session_id": "other-session",
            "execution_id": "other-execution",
            "generation": 2,
            "workspace": other_workspace,
            "container_id": "a" * 64,
            "image_digest": "sha256:" + "b" * 64,
        }
        home = tmp_path / "runtime"
        home.mkdir(mode=0o700)
        original = EnvironmentLease.from_endpoint(server, "owned-env", Path(sys.executable))
        declared = replace(original, **{field: changed[field]})
        try:
            registry = EnvironmentRegistry(home, [declared])
        except ValueError as error:
            assert str(error) == "environment_binding_mismatch"
            assert not (home / "environments.toml").exists()
            assert "attach" not in engine.calls
            assert owner.inspect(server.lease_id)["phase"] == "created"
            return
        program = tomllib.loads(registry.write_configuration().read_text())["environments"][0]
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        try:
            result = subprocess.run(
                [program["program"], *program["args"]],
                input=b"must not reach another assignment\n",
                capture_output=True,
                timeout=5,
                check=False,
            )
            assert result.returncode != 0, "different assignment reached the owned tool stream"
            assert "attach" not in engine.calls
            assert owner.inspect(server.lease_id)["phase"] == "created"
        finally:
            thread.join(timeout=3)
        assert not thread.is_alive()


def test_handshake_has_a_total_deadline_for_a_trickling_peer(tmp_path):
    """Continuous partial input must not reset the ten-second handshake budget."""
    with endpoint(tmp_path) as (owner, server, engine):
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.1)
        client.connect(str(server.path))
        response = bytearray()
        started = time.monotonic()
        try:
            client.sendall(b"{")
            while time.monotonic() - started < 11:
                try:
                    part = client.recv(1024)
                except TimeoutError:
                    client.sendall(b" ")
                    continue
                response.extend(part)
                if not part or response.endswith(b"\n"):
                    break
            assert response, "trickling input extended the total handshake deadline"
            assert json.loads(response) == {"status": "rejected"}
            assert time.monotonic() - started < 10.75
            assert "attach" not in engine.calls
            assert owner.inspect(server.lease_id)["phase"] == "created"
        finally:
            # Cleanup follows the deadline assertion and cannot produce its success.
            client.close()
            thread.join(timeout=2)
        assert not thread.is_alive()


def test_close_interrupts_an_accepted_incomplete_handshake(tmp_path, monkeypatch):
    """Closing an endpoint must stop its accepted reader before caller cleanup."""
    from hephaestus.automation import fleet_attachment

    waiting = threading.Event()
    read_handshake = fleet_attachment._read_handshake

    def observe_reader(*args, **kwargs):
        # Observe entry, then delegate unchanged to the real socket reader.
        waiting.set()
        return read_handshake(*args, **kwargs)

    monkeypatch.setattr(fleet_attachment, "_read_handshake", observe_reader)
    with endpoint(tmp_path) as (owner, server, engine):
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(server.path))
        client.sendall(b"{")
        try:
            assert waiting.wait(timeout=2), "the actual accepted reader did not begin"
            server.close()
            thread.join(timeout=1)
            assert not thread.is_alive(), "endpoint close left the accepted handshake running"
            assert "attach" not in engine.calls
            assert owner.inspect(server.lease_id)["phase"] == "created"
        finally:
            # Keep the client open until after the close-completion assertion.
            client.close()
            thread.join(timeout=2)


def test_relay_does_not_restore_flags_on_a_reused_descriptor():
    """A closed writer number can belong to another pipe before relay cleanup."""
    from hephaestus.automation.fleet_attachment import _relay

    channel, peer = socket.socketpair()
    reader, source = os.pipe()
    sink, writer = os.pipe()
    unrelated, unrelated_writer = os.pipe()
    os.set_blocking(unrelated, False)
    stopped = threading.Event()
    replaced = threading.Event()

    def close_owned_writer():
        os.close(writer)
        os.dup2(unrelated, writer)
        replaced.set()
        stopped.set()

    thread = threading.Thread(
        target=_relay,
        args=(channel, reader, writer, close_owned_writer),
        kwargs={"stopped": stopped},
    )
    thread.start()
    try:
        peer.shutdown(socket.SHUT_WR)
        assert replaced.wait(timeout=2), "the owned writer was not closed and reused"
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not os.get_blocking(unrelated), "cleanup changed an unrelated pipe's flags"
    finally:
        stopped.set()
        peer.close()
        channel.close()
        thread.join(timeout=2)
        for descriptor in (reader, source, sink, writer, unrelated, unrelated_writer):
            with suppress(OSError):
                os.close(descriptor)
