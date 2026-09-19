"""Attach provider streams through the private, durable container supervisor."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import select
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_containment import ContainedExecSupervisor, ContainerSpec
from hephaestus.cli.localization import text

_SCHEMA = "hi/fleet/attachment/v1"
_BUFFER = 65536


def binding_digest(lease: dict[str, Any]) -> str:
    """Bind the immutable owner, container, image, resources, and engine context."""
    ContainerSpec.from_document(lease["spec"])
    if not re.fullmatch(r"[0-9a-f]{64}", lease["containerId"]):
        raise ValueError("invalid_container_identity")
    value = {key: lease[key] for key in ("schema", "leaseId", "spec", "engine", "containerId")}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_handshake(channel: socket.socket, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    value = bytearray()
    while len(value) < 1024:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("attachment_handshake_timeout")
        channel.settimeout(remaining)
        part = channel.recv(1)
        if not part:
            raise ValueError("attachment_handshake_incomplete")
        value.extend(part)
        if part == b"\n":
            return json.loads(value)
    raise ValueError("attachment_handshake_limit")


def _read_ready(
    descriptors: list[int],
    channel: int,
    reader: int,
    errors: int | None,
    inbound: bytearray,
    outbound: bytearray,
) -> tuple[bool, bool, int | None]:
    peer_closed = reader_closed = False
    for descriptor in descriptors:
        limit = (
            _BUFFER - len(inbound)
            if descriptor == channel
            else _BUFFER - len(outbound)
            if descriptor == reader
            else _BUFFER
        )
        data = os.read(descriptor, limit)
        if descriptor == channel:
            peer_closed = not data
            inbound.extend(data)
        elif descriptor == reader:
            reader_closed = not data
            outbound.extend(data)
        elif not data:
            errors = None
    return peer_closed, reader_closed, errors


@contextlib.contextmanager
def _nonblocking(descriptors: list[int]) -> Iterator[dict[int, bool]]:
    original = {descriptor: os.get_blocking(descriptor) for descriptor in descriptors}
    try:
        for descriptor in descriptors:
            os.set_blocking(descriptor, False)
        yield original
    finally:
        for descriptor, blocking in original.items():
            with contextlib.suppress(OSError):
                os.set_blocking(descriptor, blocking)


def _relay(
    channel: socket.socket,
    reader: int,
    writer: int,
    close_writer: Callable[[], None] | None,
    *,
    errors: int | None = None,
    stopped: threading.Event | None = None,
) -> None:
    """Forward bounded buffers without recording provider or tool content."""
    inbound = bytearray()
    outbound = bytearray()
    peer_open = reader_open = writer_open = True
    output_closed = False
    channel.setblocking(False)
    descriptors = [reader, writer, *([] if errors is None else [errors])]
    with _nonblocking(descriptors) as original:
        while stopped is None or not stopped.is_set():
            if not peer_open and not inbound and writer_open:
                if close_writer is not None:
                    # Stop restoring this number before releasing its ownership.
                    os.set_blocking(writer, original.pop(writer))
                    close_writer()
                else:
                    # The client drains the remote output, then exits even while
                    # its provider keeps stdin open. The caller owns stdout EOF.
                    return
                writer_open = False
            if not reader_open and not outbound and not output_closed:
                channel.shutdown(socket.SHUT_WR)
                output_closed = True
            if output_closed and not peer_open and not inbound:
                return
            reads = []
            writes = []
            if peer_open and len(inbound) < _BUFFER:
                reads.append(channel.fileno())
            if reader_open and len(outbound) < _BUFFER:
                reads.append(reader)
            if errors is not None:
                reads.append(errors)
            if inbound and writer_open:
                writes.append(writer)
            if outbound:
                writes.append(channel.fileno())
            readable, writable, _ = select.select(reads, writes, [], 0.1)
            peer_closed, reader_closed, errors = _read_ready(
                readable, channel.fileno(), reader, errors, inbound, outbound
            )
            peer_open = peer_open and not peer_closed
            reader_open = reader_open and not reader_closed
            for descriptor in writable:
                pending = inbound if descriptor == writer else outbound
                count = os.write(descriptor, pending)
                del pending[:count]


class AttachmentEndpoint:
    """Expose one fixed lease through an owned socket, with no engine command input."""

    def __init__(self, supervisor: ContainedExecSupervisor, lease_id: str) -> None:
        """Bind a new listener without replacing any retained listener or socket."""
        if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            raise ValueError("invalid_container_lease")
        self.supervisor = supervisor
        self.lease_id = lease_id
        lease = supervisor.inspect(lease_id)
        self.binding_digest = binding_digest(lease)
        self.path = supervisor.journal.directory.resolve() / f"a-{lease_id[:12]}.sock"
        self._stopped = threading.Event()
        self.ready = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection: socket.socket | None = None
        self._identity: os.stat_result | None = None
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(str(self.path))
            self._identity = self.path.stat()
            self.path.chmod(0o600)
            self._listener.listen(1)
            self._listener.settimeout(0.25)
        except BaseException:
            self.close()
            raise

    def serve_once(self) -> None:
        """Serve one attachment; a failed or detached stream never disposes a lease."""
        self.ready.set()
        while not self._stopped.is_set():
            try:
                channel, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with channel:
                with self._connection_lock:
                    if self._stopped.is_set():
                        return
                    self._connection = channel
                try:
                    self._serve(channel)
                finally:
                    with self._connection_lock:
                        self._connection = None
            return

    def _serve(self, channel: socket.socket) -> None:
        channel.settimeout(10)
        try:
            request = _read_handshake(channel, 10)
            expected = {
                "schema": _SCHEMA,
                "leaseId": self.lease_id,
                "bindingDigest": self.binding_digest,
            }
            if request != expected:
                raise ValueError("attachment_not_owned")
            with self.supervisor.operation_lock:
                lease = self.supervisor.inspect(self.lease_id)
                if binding_digest(lease) != self.binding_digest:
                    raise ValueError("attachment_binding_changed")
                process = self.supervisor.start(self.lease_id)
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise ValueError("attachment_stream_unavailable")
            channel.sendall(b'{"status":"attached"}\n')
        except (KeyError, TypeError, OSError, ValueError, RuntimeError):
            with contextlib.suppress(OSError):
                channel.sendall(b'{"status":"rejected"}\n')
            return
        try:
            _relay(
                channel,
                process.stdout.fileno(),
                process.stdin.fileno(),
                process.stdin.close,
                errors=process.stderr.fileno(),
                stopped=self._stopped,
            )
        except OSError:
            pass
        finally:
            # Closing this pipe can stop the exec-server, but does not prove that
            # its descendants stopped. Only supervisor disposal can release it.
            process.stdin.close()

    def close(self) -> None:
        """Close only this socket identity and retain unresolved container ownership."""
        self._stopped.set()
        with self._connection_lock:
            if self._connection is not None:
                with contextlib.suppress(OSError):
                    self._connection.shutdown(socket.SHUT_RDWR)
        self._listener.close()
        with contextlib.suppress(FileNotFoundError):
            current = self.path.lstat()
            if self._identity is not None and (current.st_dev, current.st_ino) == (
                self._identity.st_dev,
                self._identity.st_ino,
            ):
                self.path.unlink()


def main(argv: list[str] | None = None) -> int:
    """Run the provider's private program transport without an engine executable."""
    parser = argparse.ArgumentParser(description=text(__doc__ or ""))
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--binding-digest", required=True)
    arguments = parser.parse_args(argv)
    try:
        endpoint = arguments.socket.lstat()
        parent = arguments.socket.parent.stat()
        if (
            not arguments.socket.is_absolute()
            or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid()
            or endpoint.st_mode & 0o077
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("attachment_socket_not_private")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(30)
            channel.connect(str(arguments.socket))
            channel.sendall(
                json.dumps(
                    {
                        "schema": _SCHEMA,
                        "leaseId": arguments.lease_id,
                        "bindingDigest": arguments.binding_digest,
                    }
                ).encode()
                + b"\n"
            )
            if _read_handshake(channel, 30) != {"status": "attached"}:
                raise ValueError("attachment_rejected")
            _relay(channel, sys.stdin.fileno(), sys.stdout.fileno(), None)
        return 0
    except (OSError, ValueError):
        print(text("Fleet attachment unavailable; reconciliation is required."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
