"""Compose the frozen SDK with a real loopback fixture and the build journal.

The HTTP server returns real exported JSON through a controlled endpoint. It
is not a running Agamemnon controller or a qualified build allocation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pytest

from tests.unit.automation.test_fleet_build_supervisor import (
    FIXTURE,
    FixtureExecutor,
    contract,
    records,
)


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-api-key'


class BuildHTTPFixture:
    """Serve exported controller JSON over controlled HTTP, without controller acceptance."""

    def __init__(self, state: Path, *, mode: str = "normal") -> None:
        self.state = state
        self.mode = mode
        self.requests: list[dict[str, Any]] = []
        self.release = threading.Event()
        self.claim_received = threading.Event()
        self.url = ""
        self.port = 0
        self.errors: list[Exception] = []
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._lost: set[str] = set()
        self._sockets: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    def __enter__(self) -> BuildHTTPFixture:
        """Bind one loopback listener and start only fixture-owned threads."""
        if self._server is not None:
            raise RuntimeError("HTTP fixture cannot be entered twice")
        server = _BuildHTTPServer(self)
        self._server = server
        server.timeout = 0.05
        self.port = server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        return self

    def _serve(self) -> None:
        assert self._server is not None
        try:
            while not self._closing.is_set():
                self._server.handle_request()
        except (OSError, ValueError) as error:
            if not self._closing.is_set():
                with self._lock:
                    self.errors.append(error)

    def close(self) -> None:
        """Release held replies, close owned sockets, and join within one two-second limit."""
        if self._server is None:
            return
        self._closing.set()
        self.release.set()
        deadline = time.monotonic() + 2.0
        self._server.server_close()
        with self._lock:
            sockets = tuple(self._sockets)
            threads = tuple(self._threads)
        for connection in sockets:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        owned = (self._server_thread, *threads)
        for thread in owned:
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread is not None and thread.is_alive() for thread in owned):
            raise RuntimeError("HTTP fixture could not stop every owned thread within two seconds")
        if self.errors:
            raise AssertionError(f"HTTP fixture failed: {self.errors!r}")

    def __exit__(self, *_: Any) -> None:
        """Close every socket and thread owned by this fixture."""
        self.close()


class _BuildHTTPHandler(BaseHTTPRequestHandler):
    """Read bounded requests and return the fixture's controlled responses."""

    protocol_version = "HTTP/1.0"
    server: _BuildHTTPServer

    def setup(self) -> None:
        """Limit socket waits before the handler creates its streams."""
        self.request.settimeout(1.0)
        super().setup()

    def log_message(self, _format: str, *_args: Any) -> None:
        """Keep fixture request logs out of the process output."""

    def reply(self, status: int, value: dict[str, Any]) -> None:
        """Write one complete JSON response and close the connection."""
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def _read_body(self) -> tuple[bytes, dict[str, Any]] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                self.reply(413, {"error": "fixture request exceeds its byte limit"})
                return None
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if len(raw) != length or not isinstance(body, dict):
                raise ValueError("invalid fixture request body")
        except ValueError:
            self.reply(400, {"error": "invalid fixture JSON request"})
            return None
        return raw, body

    def do_POST(self) -> None:
        """Capture durable order before applying the controlled response mode."""
        owner = self.server.owner
        journal = records(owner.state) if (owner.state / "receipts.jsonl").exists() else []
        parsed = self._read_body()
        if parsed is None:
            return
        raw, body = parsed
        with owner._lock:
            owner.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "raw_body": raw,
                    "body": body,
                    "journal": journal,
                }
            )
        if (
            self.headers.get("Authorization") != "Bearer fixture-api-key"
            or self.headers.get("X-Fleet-Build-Key") != "fixture-supervisor-key"
        ):
            self.reply(403, {"error": "fixture authentication denied"})
            return
        claim_path, fact_path = self.server.claim_path, self.server.fact_path
        if self.path not in (claim_path, fact_path):
            self.reply(404, {"error": "unknown fixture build route"})
            return
        if self.path == claim_path:
            owner.claim_received.set()
            if owner.mode == "denied-grant":
                self.reply(403, {"error": "controlled grant denial"})
                return
            if owner.mode == "delayed-claim" and not owner.release.wait(5.0):
                self.reply(504, {"error": "controlled claim hold expired"})
                return
        if owner._closing.is_set():
            return
        loss_mode = "lose-claim-once" if self.path == claim_path else "lose-fact-once"
        with owner._lock:
            lose = owner.mode == loss_mode and loss_mode not in owner._lost
            if lose:
                owner._lost.add(loss_mode)
        if lose:
            self.close_connection = True
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.path == claim_path:
            self.reply(200, contract()["grantResponse"])
        elif isinstance(body.get("eventId"), str):
            self.reply(200, {"eventId": body["eventId"]})
        else:
            self.reply(400, {"error": "fixture fact has no event ID"})


class _BuildHTTPServer(ThreadingHTTPServer):
    """Retain each accepted TCP socket and its request thread for fixture cleanup."""

    block_on_close = False

    def __init__(self, owner: BuildHTTPFixture) -> None:
        """Bind only a loopback listener with the fixed exported build routes."""
        self.owner = owner
        prefix = "/v1/fleet/build-jobs/" + contract()["admission"]["command"]["targetId"]
        self.claim_path, self.fact_path = prefix + "/claim-run", prefix + "/facts"
        super().__init__(("127.0.0.1", 0), _BuildHTTPHandler)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], address: tuple[str, int]
    ) -> None:
        """Start one retained request thread only while the fixture remains open."""
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP fixture accepts only TCP sockets")
        owner = self.owner
        with owner._lock:
            if owner._closing.is_set():
                self.shutdown_request(request)
                return
            thread = threading.Thread(
                target=self.owned_request, args=(request, address), daemon=True
            )
            owner._sockets.add(request)
            owner._threads.append(thread)
            thread.start()

    def owned_request(self, request: socket.socket, address: tuple[str, int]) -> None:
        """Release the accepted socket and retain unexpected handler errors."""
        owner = self.owner
        try:
            self.finish_request(request, address)
        except OSError:
            pass  # A deadline or controlled reply loss can close the peer socket.
        except Exception as error:
            with owner._lock:
                owner.errors.append(error)
        finally:
            self.shutdown_request(request)
            with owner._lock:
                owner._sockets.discard(request)


def sdk_types():
    """Bind either the supplied source fixture or the declared installed SDK."""
    import agamemnon_client.client as module
    from agamemnon_client import AgamemnonConfig

    if source := os.environ.get("HEPH_TEST_SDK_SOURCE"):
        expected = Path(source).resolve() / "agamemnon_client" / "client.py"
    else:
        installed = distribution("HomericIntelligence-Agamemnon")
        expected = Path(installed.locate_file("agamemnon_client/client.py")).resolve()
        origin = json.loads(installed.read_text("direct_url.json") or "{}")
        assert origin["vcs_info"]["commit_id"] == "ef39b3506bb29debe7e728f33e8d80c0923a330c"
    assert Path(module.__file__).resolve() == expected
    return module.AgamemnonClient, AgamemnonConfig


def client_factory(controller, events: dict[str, list[int]]):
    """Create a real SDK client on the bridge's loop and observe its closure."""

    async def create():
        from agamemnon_client import AgamemnonClient, AgamemnonConfig

        sdk_types()

        class TrackedClient(AgamemnonClient):
            async def aclose(self) -> None:
                await super().aclose()
                events["closed"].append(id(asyncio.get_running_loop()))

        events["created"].append(id(asyncio.get_running_loop()))
        return TrackedClient(
            AgamemnonConfig(
                host="127.0.0.1", port=controller.port, timeout=2.0, api_key=FIXTURE_AUTH_VALUE
            ),
            trust_env=False,
        )

    return create


def make_bridge(controller, events: dict[str, list[int]]):
    """Load the new bridge within each case, after collection succeeds."""
    from hephaestus.automation.fleet_build_client import BuildClientBridge

    return BuildClientBridge(
        client_factory=client_factory(controller, events),
        supervisor_key="fixture-supervisor-key",
    )


def make_transport_service(root: Path, bridge: Any, executor: FixtureExecutor):
    """Use the real supervisor and verifier with the controlled executor only."""
    from hephaestus.automation.fleet_build_supervisor import BuildSnapshot, BuildSupervisor
    from hephaestus.automation.fleet_snapshot import SnapshotPolicy

    return BuildSupervisor(
        state_dir=root / "state",
        workspace_root=root / "workspaces",
        policy=contract()["admission"]["command"]["payload"]["policy"],
        client=bridge,
        snapshot=BuildSnapshot(FIXTURE / "snapshot", SnapshotPolicy(20, 8192)),
        executor=executor,
        claim_id_factory=lambda: contract()["claimRequest"]["claimId"],
    )


def test_frozen_sdk_and_actual_loopback_endpoint_are_a_valid_control(tmp_path: Path) -> None:
    """The real SDK reaches the controlled endpoint with both explicit keys."""
    with BuildHTTPFixture(tmp_path / "state") as controller:

        async def request():
            client_type, config_type = sdk_types()
            async with client_type(
                config_type(
                    host="127.0.0.1", port=controller.port, timeout=2.0, api_key=FIXTURE_AUTH_VALUE
                ),
                trust_env=False,
            ) as client:
                return await client.fleet_build_claim_run(
                    contract()["admission"]["command"]["targetId"],
                    contract()["claimRequest"],
                    "fixture-supervisor-key",
                )

        assert asyncio.run(request()) == contract()["grantResponse"]
    assert len(controller.requests) == 1
    captured_request = controller.requests[0]
    assert captured_request["path"].endswith("/claim-run")
    assert captured_request["body"] == contract()["claimRequest"]
    assert captured_request["headers"]["Authorization"] == "Bearer fixture-api-key"
    assert captured_request["headers"]["X-Fleet-Build-Key"] == "fixture-supervisor-key"


def test_bridge_runs_one_real_transport_attempt_after_durable_claim(tmp_path: Path) -> None:
    """Actual HTTP observes claim and terminal writes around one fixed child."""
    events: dict[str, list[int]] = {"created": [], "closed": []}
    executor = FixtureExecutor(tmp_path / "state")
    command = contract()["admission"]["command"]
    with BuildHTTPFixture(tmp_path / "state") as controller:
        with make_bridge(controller, events) as bridge:
            with make_transport_service(tmp_path, bridge, executor) as service:
                result = service.handle(command)
                assert result["status"] == "completed"
                assert result["collectionVerified"] is False
                assert service.handle(command) == result
        bridge.close()
    assert len(executor.started) == len(executor.disposed) == 1
    assert len(controller.requests) == 2
    claim, fact = controller.requests
    assert claim["body"] == contract()["claimRequest"]
    assert any(row.get("claim") == claim["body"] for row in claim["journal"])
    assert fact["path"].endswith("/facts")
    assert any(row.get("terminal") == fact["body"] for row in fact["journal"])
    assert fact["body"] == records(tmp_path / "state")[-1]["terminal"]
    assert events["created"] == events["closed"] and len(events["created"]) == 1


@pytest.mark.parametrize("mode", ["lose-claim-once", "lose-fact-once"])
def test_bridge_lost_http_reply_replays_retained_identity(tmp_path: Path, mode: str) -> None:
    """A lost real response cannot change the claim or repeat the owned child."""
    events: dict[str, list[int]] = {"created": [], "closed": []}
    executor = FixtureExecutor(tmp_path / "state")
    command = contract()["admission"]["command"]
    with BuildHTTPFixture(tmp_path / "state", mode=mode) as controller:
        with make_bridge(controller, events) as bridge:
            with make_transport_service(tmp_path, bridge, executor) as service:
                assert service.handle(command)["status"] == "reconciliation_required"
        before = copy.deepcopy(controller.requests)
        assert len(executor.started) == (0 if mode == "lose-claim-once" else 1)
        with make_bridge(controller, events) as bridge:
            with make_transport_service(tmp_path, bridge, executor) as service:
                assert service.handle(command)["status"] == "completed"
    assert len(executor.started) == len(executor.disposed) == 1
    retried_path = "/claim-run" if mode == "lose-claim-once" else "/facts"
    retried = [r for r in controller.requests if r["path"].endswith(retried_path)]
    assert len(retried) == 2 and retried[0]["body"] == retried[1]["body"]
    assert controller.requests[: len(before)] == before
    assert len(controller.requests) == 3
    assert len(events["created"]) == len(events["closed"]) == 2


def test_bridge_denied_actual_http_grant_has_no_execution_effects(tmp_path: Path) -> None:
    """Correct source bytes cannot replace denied controller authority."""
    events: dict[str, list[int]] = {"created": [], "closed": []}
    executor = FixtureExecutor(tmp_path / "state")
    with BuildHTTPFixture(tmp_path / "state", mode="denied-grant") as controller:
        with make_bridge(controller, events) as bridge:
            with make_transport_service(tmp_path, bridge, executor) as service:
                result = service.handle(contract()["admission"]["command"])
                assert result["status"] == "reconciliation_required"
    assert len(controller.requests) == 1
    assert executor.prepared == executor.started == executor.disposed == []
    assert all(row.get("terminal") is None for row in records(tmp_path / "state"))


def test_bridge_caller_deadline_bounds_the_actual_http_wait(tmp_path: Path) -> None:
    """The caller's smaller budget controls a request with a larger SDK budget."""
    events: dict[str, list[int]] = {"created": [], "closed": []}
    with BuildHTTPFixture(tmp_path / "state", mode="delayed-claim") as controller:
        with make_bridge(controller, events) as bridge:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                bridge.claim_run(
                    contract()["admission"]["command"]["targetId"],
                    contract()["claimRequest"],
                    deadline=started + 0.2,
                )
            assert time.monotonic() - started < 1.5
            assert len(controller.requests) == 1
            assert not controller.release.is_set()
    assert events["created"] == events["closed"]


def test_bridge_rejects_nested_event_loop_and_calls_after_close(tmp_path: Path) -> None:
    """The synchronous owner cannot nest a runner or reuse a closed client."""
    events: dict[str, list[int]] = {"created": [], "closed": []}
    with BuildHTTPFixture(tmp_path / "state") as controller:
        with make_bridge(controller, events) as bridge:

            async def nested() -> None:
                with pytest.raises(RuntimeError, match=r"loop|async"):
                    bridge.claim_run(
                        contract()["admission"]["command"]["targetId"],
                        contract()["claimRequest"],
                        deadline=time.monotonic() + 1,
                    )

            asyncio.run(nested())
        with pytest.raises(RuntimeError, match="closed"):
            bridge.claim_run(
                contract()["admission"]["command"]["targetId"],
                contract()["claimRequest"],
                deadline=time.monotonic() + 1,
            )
    assert controller.requests == []
    assert events["created"] == events["closed"] and len(events["created"]) == 1
