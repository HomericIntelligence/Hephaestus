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
from typing import Any

from hephaestus.agents.codex_isolation import CODEX_VERSION_OUTPUT
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


class ProviderError(RuntimeError):
    """Report an unconfirmed provider operation without publishing private output."""

    def __init__(self, code: str, *, rpc_error: dict[str, Any] | None = None) -> None:
        """Keep optional RPC diagnostics private while the public message stays bounded."""
        super().__init__(code)
        self.rpc_error = rpc_error


class CodexAppServer:
    """Own one provider process and route responses independently of notifications."""

    def __init__(self, command: list[str], codex_home: Path, *, timeout: float = 30) -> None:
        """Prepare a bounded connection with a private authentication owner."""
        self.command = command
        self.codex_home = codex_home
        self.timeout = timeout
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4096)
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
        version = subprocess.run(
            [*self.command, "--version"],
            env=env,
            capture_output=True,
            timeout=10,
            check=True,
        )
        if version.stdout.decode().strip() != CODEX_VERSION_OUTPUT:
            raise ProviderError("provider_version_mismatch")
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
        if len(data) > 1024 * 1024:
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
            result = response.get("result")
            if not isinstance(result, dict):
                raise ProviderError("provider_invalid_result")
            return result
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

    def _enqueue(self, message: dict[str, Any]) -> None:
        with self._lock:
            self._receive_sequence += 1
            message["_fleetSequence"] = self._receive_sequence
            if message["method"] in LIVE_ACTIVITY_METHODS:
                params = message.get("params", {})
                thread_id, turn_id = params.get("threadId"), params.get("turnId")
                if not isinstance(thread_id, str) or not isinstance(turn_id, str):
                    raise ValueError("invalid observation identity")
                key = (thread_id, turn_id)
                if key not in self._observations and len(self._observations) >= 4096:
                    raise queue.Full
                self._observations[key] = {
                    "method": message["method"],
                    "_fleetSequence": self._receive_sequence,
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            else:
                self.events.put_nowait(message)

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Return ordered lifecycle events and coalesced metadata observations."""
        with self._lock:
            messages = list(self._observations.values())
            self._observations.clear()
            while True:
                try:
                    messages.append(self.events.get_nowait())
                except queue.Empty:
                    break
        messages.sort(key=lambda message: message["_fleetSequence"])
        for message in messages:
            del message["_fleetSequence"]
        return messages

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        reason = "provider_disconnected"
        try:
            while line := process.stdout.readline(1024 * 1024 + 1):
                if not line.endswith(b"\n") or len(line) > 1024 * 1024:
                    raise ValueError("message limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid message")
                if "method" in message:
                    self._enqueue(message)
                else:
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
