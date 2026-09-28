"""Check private attachment with real Unix sockets and a fixed pipe process."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.unit.automation.test_fleet_containment import Engine, specification, supervisor

pytestmark = pytest.mark.precommit


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
