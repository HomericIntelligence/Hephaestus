"""Connect one pinned Codex app-server through its private stdio transport."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import queue
import select
import signal
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeGuard

from hephaestus.agents.codex_isolation import CODEX_VERSION_OUTPUT
from hephaestus.agents.pi_plugins import run_bounded_command
from hephaestus.automation.fleet_isolation import provider_environment

LIVE_ACTIVITY_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
        "thread/tokenUsage/updated",
    }
)
_FRAME_MAX_BYTES = 1024 * 1024
_LIFECYCLE_MAX_BYTES = 4 * 1024 * 1024
_LIFECYCLE_MAX_RECORDS = 256
_PROTOCOL_ID_MAX_BYTES = 1024
_RESULT_ID_FIELDS = {
    "thread/start": ("thread", "id"),
    "thread/resume": ("thread", "id"),
    "turn/start": ("turn", "id"),
}
_TURN_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})


def _valid_protocol_id(value: Any) -> TypeGuard[str]:
    """Return whether one provider identity fits its private transport bound."""
    return (
        isinstance(value, str)
        and bool(value)
        and "\0" not in value
        and len(value.encode("utf-8")) <= _PROTOCOL_ID_MAX_BYTES
    )


def _validate_provider_params(params: Any) -> dict[str, Any]:
    """Return provider parameters after bounded identity validation."""
    if not isinstance(params, dict):
        raise ValueError("invalid provider parameters")
    for field in ("thread", "turn", "item"):
        if field in params and not isinstance(params[field], dict):
            raise ValueError("invalid provider parameters")
    for field in ("threadId", "turnId", "itemId"):
        if field in params and not _valid_protocol_id(params[field]):
            raise ValueError("invalid provider identity")
    for field in ("thread", "turn", "item"):
        nested = params.get(field)
        if isinstance(nested, dict) and "id" in nested and not _valid_protocol_id(nested["id"]):
            raise ValueError("invalid provider identity")
    return params


def _validate_provider_result(method: str, result: Any) -> dict[str, Any]:
    """Return a result only when fields used by the worker are valid."""
    if not isinstance(result, dict):
        raise ValueError("invalid provider result")
    identity_fields = _RESULT_ID_FIELDS.get(method)
    if identity_fields is None:
        return result
    container_name, identity_name = identity_fields
    container = result.get(container_name)
    if not isinstance(container, dict) or not _valid_protocol_id(container.get(identity_name)):
        raise ValueError("invalid provider result identity")
    return result


class ProviderError(RuntimeError):
    """Report an unconfirmed provider operation without publishing private output."""

    def __init__(self, code: str, *, rpc_error: dict[str, Any] | None = None) -> None:
        """Keep optional RPC diagnostics private while the public message stays bounded."""
        super().__init__(code)
        self.rpc_error = rpc_error


def read_provider_version(command: tuple[str, ...], *, env: dict[str, str], timeout: float) -> str:
    """Return the pinned provider version through a bounded process boundary."""
    result = run_bounded_command((*command, "--version"), env=env, timeout=timeout)
    if result.timed_out:
        raise ProviderError("provider_version_timeout")
    if result.output_overflow:
        raise ProviderError("provider_version_response_limit")
    if result.returncode != 0:
        raise ProviderError("provider_version_command_failed")
    version = result.stdout.strip()
    if version != CODEX_VERSION_OUTPUT:
        raise ProviderError("provider_version_mismatch")
    return version


class CodexAppServer:
    """Own one provider process and route responses independently of notifications."""

    def __init__(self, command: list[str], codex_home: Path, *, timeout: float = 30) -> None:
        """Prepare a bounded connection with a private authentication owner."""
        self.command = command
        self.codex_home = codex_home
        self.timeout = timeout
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_LIFECYCLE_MAX_RECORDS)
        self._event_bytes = 0
        self._observation_bytes = 0
        self._observations: dict[tuple[str, str], dict[str, Any]] = {}
        self._receive_sequence = 0
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._failure: str | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._owner_fd: int | None = None
        self._cleanup_confirmed = True

    def start(self) -> None:
        """Check the pinned binary before starting its stdio service."""
        owner_fd = os.open(
            self.codex_home / "fleet-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(owner_fd)
            raise RuntimeError("another authentication owner is active") from error
        self._owner_fd = owner_fd
        env = provider_environment(self.codex_home)
        read_provider_version(tuple(self.command), env=env, timeout=10)
        self.process = subprocess.Popen(
            [
                *self.command,
                "app-server",
                "--listen",
                "stdio://",
                "--disable",
                "multi_agent",
                "--disable",
                "multi_agent_v2",
                "-c",
                "agents.enabled=false",
                "-c",
                'cli_auth_credentials_store="file"',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=self.codex_home,
            start_new_session=True,
            pass_fds=(owner_fd,),
        )
        self._reader = threading.Thread(
            target=self._read, name="fleet-provider-reader", daemon=True
        )
        if self.process.stdin is not None:
            os.set_blocking(self.process.stdin.fileno(), False)
        self._reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "hephaestus-fleet", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _send(self, message: dict[str, Any], *, deadline: float | None = None) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProviderError("provider_not_started")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        if len(data) > _FRAME_MAX_BYTES:
            raise ProviderError("provider_message_limit")
        try:
            with self._write_lock:
                descriptor = self.process.stdin.fileno()
                if deadline is None:
                    deadline = time.monotonic() + self.timeout
                remaining = memoryview(data)
                while remaining:
                    budget = deadline - time.monotonic()
                    if budget <= 0 or not select.select([], [descriptor], [], budget)[1]:
                        raise ProviderError("provider_write_timeout")
                    try:
                        count = os.write(descriptor, remaining[:65536])
                    except BlockingIOError:
                        continue
                    remaining = remaining[count:]
        except OSError as error:
            raise ProviderError("provider_disconnected") from error

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Wait for one matching response while the reader accepts activity events."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._lock:
            if self._failure:
                raise ProviderError(self._failure)
            self._next_id += 1
            request_id = self._next_id
            future: Future[dict[str, Any]] = Future()
            self._pending[request_id] = future
        try:
            self._send({"id": request_id, "method": method, "params": params}, deadline=deadline)
            response = future.result(timeout=max(0, deadline - time.monotonic()))
            if "error" in response:
                raise ProviderError("provider_rejected_request", rpc_error=response["error"])
            try:
                return _validate_provider_result(method, response.get("result"))
            except ValueError as error:
                raise ProviderError("provider_invalid_result") from error
        except TimeoutError as error:
            raise ProviderError("provider_timeout") from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def respond(self, request_id: str | int, response: dict[str, Any]) -> None:
        """Send a response only after the worker validates request ownership."""
        self._send({"id": request_id, "result": response})

    def reject(self, request_id: str | int) -> None:
        """Reject unsupported server requests without granting permissions."""
        self._send({"id": request_id, "error": {"code": -32601, "message": "unsupported request"}})

    @staticmethod
    def _validate_incoming_message(message: dict[str, Any]) -> None:
        """Reject malformed provider frames before worker dispatch."""
        if "method" not in message:
            request_id = message.get("id")
            if type(request_id) is not int or request_id < 0:
                raise ValueError("invalid response identity")
            if ("result" in message) == ("error" in message):
                raise ValueError("invalid response shape")
            if "error" in message and not isinstance(message["error"], dict):
                raise ValueError("invalid response error")
            return
        method = message["method"]
        if not isinstance(method, str) or not method or len(method) > 256:
            raise ValueError("invalid provider method")
        params = _validate_provider_params(message.get("params", {}))
        if method == "turn/completed":
            turn = params.get("turn")
            if (
                not _valid_protocol_id(params.get("threadId"))
                or not isinstance(turn, dict)
                or not _valid_protocol_id(turn.get("id"))
                or turn.get("status") not in _TURN_TERMINAL_STATUSES
            ):
                raise ValueError("invalid turn completion")
        if "id" in message:
            request_id = message["id"]
            if type(request_id) not in {int, str} or (
                isinstance(request_id, str) and not _valid_protocol_id(request_id)
            ):
                raise ValueError("invalid provider request identity")

    def _enqueue(self, message: dict[str, Any], *, frame_bytes: int | None = None) -> None:
        """Queue one validated frame within aggregate record and byte limits."""
        self._validate_incoming_message(message)
        if frame_bytes is None:
            frame_bytes = len(
                (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
            )
        if frame_bytes > _FRAME_MAX_BYTES:
            raise queue.Full
        with self._lock:
            self._receive_sequence += 1
            message["_fleetSequence"] = self._receive_sequence
            if message["method"] in LIVE_ACTIVITY_METHODS:
                params = message.get("params", {})
                thread_id, turn_id = params.get("threadId"), params.get("turnId")
                if not _valid_protocol_id(thread_id) or not _valid_protocol_id(turn_id):
                    raise ValueError("invalid observation identity")
                key = (thread_id, turn_id)
                if key not in self._observations and len(self._observations) >= 4096:
                    raise queue.Full
                observation = {
                    "method": message["method"],
                    "_fleetSequence": self._receive_sequence,
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
                observation_bytes = len(
                    json.dumps(observation, separators=(",", ":"), ensure_ascii=False).encode()
                )
                previous = self._observations.get(key)
                previous_bytes = previous.get("_fleetBytes", 0) if previous is not None else 0
                queued_bytes = self._event_bytes + self._observation_bytes - previous_bytes
                if queued_bytes + observation_bytes > _LIFECYCLE_MAX_BYTES:
                    raise queue.Full
                observation["_fleetBytes"] = observation_bytes
                self._observations[key] = observation
                self._observation_bytes += observation_bytes - previous_bytes
            else:
                queued_bytes = self._event_bytes + self._observation_bytes
                if queued_bytes + frame_bytes > _LIFECYCLE_MAX_BYTES:
                    raise queue.Full
                message["_fleetBytes"] = frame_bytes
                self.events.put_nowait(message)
                self._event_bytes += frame_bytes

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Return ordered lifecycle events and coalesced metadata observations."""
        with self._lock:
            messages = list(self._observations.values())
            self._observations.clear()
            self._observation_bytes = 0
            while True:
                try:
                    message = self.events.get_nowait()
                except queue.Empty:
                    break
                self._event_bytes -= message.pop("_fleetBytes")
                messages.append(message)
        messages.sort(key=lambda message: message["_fleetSequence"])
        for message in messages:
            message.pop("_fleetBytes", None)
            del message["_fleetSequence"]
        return messages

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        reason = "provider_disconnected"
        try:
            while line := process.stdout.readline(_FRAME_MAX_BYTES + 1):
                if not line.endswith(b"\n") or len(line) > _FRAME_MAX_BYTES:
                    raise ValueError("message limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid message")
                if "method" in message:
                    self._enqueue(message, frame_bytes=len(line))
                else:
                    self._validate_incoming_message(message)
                    with self._lock:
                        request_id = message.get("id")
                        future = self._pending.get(request_id) if type(request_id) is int else None
                        if future is not None and not future.done():
                            future.set_result(message)
        except (OSError, ValueError, queue.Full):
            reason = "provider_protocol_failure"
            process.terminate()
        finally:
            with self._lock:
                self._failure = reason
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ProviderError(reason))

    @property
    def failed(self) -> bool:
        """Report a closed or invalid protocol stream."""
        return self._failure is not None

    def close(self) -> bool:
        """Stop the owned process group and wait for bounded cleanup."""
        process = self.process
        if process is None:
            self._release_owner()
            return self._cleanup_confirmed
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            self._cleanup_confirmed = False
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        # Tool descendants can retain the pipes after the app-server exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            self._cleanup_confirmed = False
        process.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=3)
            if self._reader.is_alive():
                raise ProviderError("provider_pipe_cleanup_uncertain")
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        self._release_owner()
        self.process = None
        return self._cleanup_confirmed

    def _release_owner(self) -> None:
        if self._owner_fd is not None:
            os.close(self._owner_fd)
            self._owner_fd = None
