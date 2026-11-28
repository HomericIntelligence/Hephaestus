"""Observe Codex hooks through a private local socket."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import signal
import socket
import socketserver
import stat
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, TextIO, cast

from hephaestus.errors import boundary_main
from hephaestus.fleet.codex_attention import (
    SUPPORTED_HOOKS,
    AttentionIdentity,
    CodexAttentionAdapter,
    sanitize_hook,
)

_MAX_REQUEST_BYTES = 16_384
_MAX_RESPONSE_BYTES = 1_024
_MAX_FRAME_BYTES = 65_536
_MAX_JSON_DEPTH = 16
_MAX_SOCKET_PATH_BYTES = 103


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_json_value(value: Any, depth: int = 0) -> None:
    """Reject excessive JSON depth and non-finite numbers."""
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting exceeds the observation limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _validate_json_value(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_json_value(child, depth + 1)


def _reject_json_constant(constant: str) -> None:
    """Reject JavaScript constants that are not finite JSON numbers."""
    raise ValueError("non-finite JSON number")


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    """Decode one unambiguous bounded JSON object."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid observation JSON") from error
    if not isinstance(value, dict):
        raise ValueError("observation payload must be an object")
    _validate_json_value(value)
    return cast(dict[str, Any], value)


def build_hooks_config(python: Path, state_dir: Path) -> dict[str, Any]:
    """Build synchronous observation-only hooks for one private socket."""
    if not python.is_absolute() or not state_dir.is_absolute():
        raise ValueError("python and state dir must be absolute")
    command = shlex.join(
        [
            str(python),
            "-m",
            "hephaestus.fleet.codex_attention_cli",
            "hook",
            "--state-dir",
            str(state_dir),
        ]
    )
    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 3 if event == "SessionEnd" else 2,
                        }
                    ],
                }
            ]
            for event in sorted(SUPPORTED_HOOKS)
        }
    }


def _private_state_dir(state_dir: Path) -> Path:
    """Create or validate an owned, non-symlinked private state directory."""
    if not state_dir.is_absolute():
        raise ValueError("state dir must be absolute")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = state_dir.lstat()
    resolved = state_dir.resolve(strict=True)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or resolved != state_dir
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("state dir must be an owned mode-0700 real directory")
    return state_dir


def _socket_metadata(path: Path) -> os.stat_result:
    """Return metadata for an owned socket, rejecting other filesystem types."""
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("observation socket path is not an owned Unix socket")
    return metadata


def _remove_stale_socket(path: Path) -> None:
    """Remove a refused owned socket without disconnecting an active observer."""
    _socket_metadata(path)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
    except OSError as error:
        raise ValueError("observation socket availability is uncertain") from error
    else:
        raise ValueError("observation socket is already active")
    finally:
        probe.close()


class _ObservationServer(socketserver.UnixStreamServer):
    """Bind one request handler to its attention server owner."""

    owner: CodexAttentionServer


class _ObservationHandler(socketserver.StreamRequestHandler):
    """Accept one bounded metadata frame and return a private acknowledgement."""

    server: _ObservationServer

    def handle(self) -> None:
        """Handle one observation request without reflecting raw input."""
        response = {"accepted": False, "error": "invalid_observation"}
        try:
            self.connection.settimeout(1.0)
            raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
            if len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ValueError("invalid observation frame")
            payload = _decode_json_object(raw)
            self.server.owner.accept(payload)
        except (OSError, TypeError, UnicodeError, ValueError):
            self.server.owner.warn_invalid()
        else:
            response = {"accepted": True}
        try:
            self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
        except OSError:
            self.server.owner.errors.write(
                "codex attention: observation acknowledgement unavailable\n"
            )
            self.server.owner.errors.flush()


class CodexAttentionServer:
    """Expose one bounded allocation-local observation socket."""

    def __init__(
        self,
        state_dir: Path,
        adapter: CodexAttentionAdapter,
        *,
        output: TextIO = sys.stdout,
        errors: TextIO = sys.stderr,
    ) -> None:
        """Initialize the server boundary without opening its socket."""
        self.state_dir = state_dir
        self.adapter = adapter
        self.output = output
        self.errors = errors
        self._server: _ObservationServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        _private_state_dir(self.state_dir)
        if len(os.fsencode(self.socket_path)) > _MAX_SOCKET_PATH_BYTES:
            raise ValueError("observation socket path exceeds the portable byte limit")

    @property
    def socket_path(self) -> Path:
        """Return the owned socket path."""
        return self.state_dir / "observation.sock"

    def __enter__(self) -> CodexAttentionServer:
        """Create and return the private Unix socket server."""
        _private_state_dir(self.state_dir)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            _remove_stale_socket(self.socket_path)
        server = _ObservationServer(
            str(self.socket_path), _ObservationHandler, bind_and_activate=False
        )
        try:
            server.owner = self
            server.timeout = 0.1
            server.server_bind()
            self.socket_path.chmod(0o600)
            self._socket_identity = (
                self.socket_path.stat().st_dev,
                self.socket_path.stat().st_ino,
            )
            server.server_activate()
        except BaseException:
            server.server_close()
            raise
        self._server = server
        return self

    def __exit__(self, *exception: object) -> None:
        """Close the server on context exit."""
        self.close()

    def warn_invalid(self) -> None:
        """Report invalid input without including its contents."""
        self.errors.write("codex attention: invalid observation\n")
        self.errors.flush()

    def _emit(self, frame: dict[str, Any]) -> None:
        encoded = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > _MAX_FRAME_BYTES:
            raise ValueError("observation frame exceeds the transport limit")
        self.output.write(encoded + "\n")
        self.output.flush()

    def accept(self, payload: dict[str, Any]) -> None:
        """Validate, project, and emit one hook event."""
        self._emit(self.adapter.handle(sanitize_hook(payload)))

    def handle_once(self) -> None:
        """Process one bounded request and any due heartbeats."""
        if self._server is None:
            raise ValueError("observation server is not active")
        self._server.handle_request()
        for frame in self.adapter.due_heartbeats():
            self._emit(frame)

    def close(self) -> None:
        """Close and remove only the socket inode owned by this server."""
        if self._server is None:
            return
        self._server.server_close()
        self._server = None
        try:
            metadata = _socket_metadata(self.socket_path)
            identity = (metadata.st_dev, metadata.st_ino)
            if identity == self._socket_identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._socket_identity = None


def send_hook(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    timeout: float = 0.5,
) -> dict[str, Any]:
    """Send one sanitized hook event through the private observation socket."""
    event = sanitize_hook(payload)
    encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("observation request exceeds the transport limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(encoded)
        with connection.makefile("rb") as response_stream:
            raw = response_stream.readline(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid observation acknowledgement")
    response = _decode_json_object(raw)
    if response != {"accepted": True}:
        raise ValueError("observation was not accepted")
    return response


def _parser() -> argparse.ArgumentParser:
    """Build the module command-line parser."""
    parser = argparse.ArgumentParser(prog="hephaestus.fleet.codex_attention_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="emit trusted NDJSON observations")
    serve.add_argument("--state-dir", type=Path, required=True)
    serve.add_argument("--cluster", required=True)
    serve.add_argument("--account-tier", choices=("root", "admin", "user"), required=True)
    serve.add_argument("--unix-user", required=True)
    serve.add_argument("--node", required=True)
    serve.add_argument("--harness-version", required=True)
    serve.add_argument("--adapter-instance-id", default=uuid.uuid4().hex)
    serve.add_argument("--slurm-job-id")
    serve.add_argument("--heartbeat-seconds", type=float, default=30.0)
    serve.add_argument("--max-sessions", type=int, default=256)

    hook = commands.add_parser("hook", help="send one Codex hook observation")
    hook.add_argument("--state-dir", type=Path, required=True)
    hook.add_argument("--timeout", type=float, default=0.5)

    hooks = commands.add_parser("print-hooks", help="print Codex hooks.json")
    hooks.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run the module through the shared error boundary."""
    return boundary_main(
        _run,
        argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        stream=stderr,
    )


def _run(
    argv: list[str] | None = None,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run a server, print hook configuration, or send one hook event."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "print-hooks":
        stdout.write(
            json.dumps(build_hooks_config(Path(sys.executable), arguments.state_dir), indent=2)
            + "\n"
        )
        return 0
    if arguments.command == "hook":
        try:
            raw = stdin.read(_MAX_REQUEST_BYTES + 1)
            if len(raw.encode()) > _MAX_REQUEST_BYTES:
                raise ValueError("hook input exceeds the transport limit")
            payload = _decode_json_object(raw.encode())
            send_hook(
                arguments.state_dir / "observation.sock",
                payload,
                timeout=arguments.timeout,
            )
        except (OSError, TypeError, UnicodeError, ValueError):
            stderr.write("codex attention: observation unavailable\n")
            stderr.flush()
        return 0

    identity = AttentionIdentity(
        cluster=arguments.cluster,
        account_tier=arguments.account_tier,
        unix_user=arguments.unix_user,
        node=arguments.node,
        harness_version=arguments.harness_version,
        adapter_instance_id=arguments.adapter_instance_id,
        slurm_job_id=arguments.slurm_job_id,
    )
    adapter = CodexAttentionAdapter(
        identity,
        heartbeat_seconds=arguments.heartbeat_seconds,
        max_sessions=arguments.max_sessions,
    )
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        with CodexAttentionServer(
            arguments.state_dir, adapter, output=stdout, errors=stderr
        ) as server:
            while not stopping.is_set():
                server.handle_once()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
