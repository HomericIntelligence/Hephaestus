"""Exercise the real SDK with exported controller responses over local HTTP.

The fixture is a controlled transport, not a running controller or allocation.
It keeps uncertainty and lifecycle assertions separate from execution proof.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import socket
import threading
import time
import unittest
from contextlib import suppress
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
from agamemnon_client import AgamemnonClient, AgamemnonConfig

from hephaestus.automation.fleet_build_service import FleetBuildService

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/fleet_build_consumer/controller.json"


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-private-key'


def controller() -> dict[str, Any]:
    """Read the exact retained controller export for each independent case."""
    return json.loads(FIXTURE.read_text())


class BuildConsumerHTTP:
    """Own one finite serial HTTP fixture and its response controls."""

    def __init__(self) -> None:
        self.data = controller()
        self.record = copy.deepcopy(self.data["admission"]["record"])
        self.requests: list[dict[str, Any]] = []
        self.mode = "normal"
        self.override: Any = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lost: set[str] = set()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, _format: str, *_args: Any) -> None:
                """Keep fixture credentials and bodies out of test output."""

            def do_GET(self) -> None:
                """Serve one captured controller read."""
                self.reply()

            def do_POST(self) -> None:
                """Serve one captured controller write."""
                self.reply()

            def reply(self) -> None:
                self.connection.settimeout(1)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 16384:
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(size)) if size else None
                fixture.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": body,
                        "headers": dict(self.headers),
                    }
                )
                fixture.entered.set()
                if self.path.endswith("/submit"):
                    result = copy.deepcopy(fixture.data["admission"])
                    fixture.record = copy.deepcopy(result["record"])
                    status = 202
                elif self.path.endswith("/cancel"):
                    result = copy.deepcopy(fixture.data["cancelResponse"])
                    fixture.record = copy.deepcopy(result["record"])
                    status = 202
                else:
                    result = copy.deepcopy(fixture.record)
                    status = 200
                if fixture.mode == "conflict":
                    status, result = 409, {"error": "controlled identity conflict"}
                if fixture.override is not None:
                    result = copy.deepcopy(fixture.override)
                if fixture.mode == "stall":
                    fixture.release.wait(2)
                if (
                    fixture.mode == "lost"
                    and self.command == "POST"
                    and self.path not in fixture._lost
                ):
                    fixture._lost.add(self.path)
                    with suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                    return
                encoded = json.dumps(result).encode()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(encoded)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,))
        self.thread.start()

    @property
    def port(self) -> int:
        """Return only this fixture's owned loopback port."""
        return self.server.server_port

    def close(self) -> None:
        """Release a stalled handler before shutdown and join the owned thread."""
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("owned fixture thread did not stop")


class FleetBuildServiceTests(unittest.IsolatedAsyncioTestCase):
    """Keep the SDK real and control only the external HTTP endpoint."""

    async def asyncSetUp(self) -> None:
        self.http = BuildConsumerHTTP()
        self.addCleanup(self.http.close)
        self.client = AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=self.http.port, api_key=FIXTURE_AUTH_VALUE, timeout=1
            ),
            trust_env=False,
        )
        self.addAsyncCleanup(self.client.aclose)

    def service(self) -> FleetBuildService:
        """Use a new facade over the actual installed SDK and retained intent."""
        return FleetBuildService(self.client, self.http.data["submission"])

    def deadline(self) -> float:
        """Give each ordinary request a finite one-second budget."""
        return time.monotonic() + 1

    async def test_direct_sdk_http_control_uses_the_actual_controller_envelope(self) -> None:
        response = await self.client.fleet_build_submit(self.http.data["submission"])
        self.assertEqual(response, self.http.data["admission"])
        record = await self.client.fleet_build_status(response["record"]["id"])
        self.assertEqual(record, response["record"])
        self.assertEqual([r["method"] for r in self.http.requests], ["POST", "GET"])
        self.assertEqual(self.http.requests[0]["body"], self.http.data["submission"])

    async def test_direct_sdk_lost_response_control_retains_accepted_fixture_state(self) -> None:
        self.http.mode = "lost"
        with self.assertRaises(httpx.RemoteProtocolError):
            await self.client.fleet_build_submit(self.http.data["submission"])
        self.assertEqual(len(self.http.requests), 1)
        record = await self.client.fleet_build_status(self.http.data["admission"]["record"]["id"])
        self.assertEqual(record, self.http.data["admission"]["record"])
        self.assertEqual([r["method"] for r in self.http.requests], ["POST", "GET"])

    async def test_actual_202_submission_preserves_intent_and_ordinary_auth(self) -> None:
        service = self.service()
        result = await service.submit(deadline=self.deadline())
        self.assertEqual(result, self.http.data["admission"]["record"])
        self.assertEqual(service.build_id, result["id"])
        self.assertEqual(self.http.requests[0]["body"], self.http.data["submission"])
        self.assertEqual(self.http.requests[0]["path"], "/v1/fleet/build-jobs/submit")
        headers = {k.lower(): v for k, v in self.http.requests[0]["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer fixture-private-key")
        self.assertNotIn("x-fleet-build-key", headers)
        self.assertFalse(result["collectionVerified"])

    async def test_status_accepts_bare_record_and_legal_lifecycle_change(self) -> None:
        service = self.service()
        result = await service.status(deadline=self.deadline())
        self.assertEqual(result, self.http.record)
        self.http.record = copy.deepcopy(self.http.data["terminalResponse"]["record"])
        later = await service.status(deadline=self.deadline())
        self.assertEqual(later, self.http.record)
        self.assertFalse(later["collectionVerified"])
        self.assertEqual([r["method"] for r in self.http.requests], ["GET", "GET"])

    async def test_input_and_returned_copies_cannot_change_the_retained_intent(self) -> None:
        submission = copy.deepcopy(self.http.data["submission"])
        service = FleetBuildService(self.client, submission)
        original = copy.deepcopy(submission)
        submission["parent"]["sessionId"] = "changed-after-construction"
        exposed = service.submission
        self.assertEqual(exposed, original)
        exposed["snapshot"]["manifestDigest"] = "0" * 64
        first = await service.submit(deadline=self.deadline())
        self.assertEqual(self.http.requests[0]["body"], original)
        first["build"]["request"]["parent"]["generation"] = 99
        second = await service.status(deadline=self.deadline())
        self.assertEqual(second, self.http.record)
        self.assertEqual(service.submission, original)

    async def test_closed_submission_rejects_bad_inputs_before_http(self) -> None:
        mutations = [
            lambda x: x.update(endpoint="https://example.invalid"),
            lambda x: x.update(recipeId="arbitrary-shell"),
            lambda x: x.update(parameters={"command": "anything"}),
            lambda x: x.update(idempotencyKey="../escape"),
            lambda x: x["parent"].update(sessionId=""),
            lambda x: x["parent"].update(generation=True),
            lambda x: x["parent"].update(generation=1.0),
            lambda x: x["snapshot"].update(members=2.0),
            lambda x: x["snapshot"].update(manifestDigest="bad"),
            lambda x: x["snapshot"].update(path="/private/source"),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.http.data["submission"])
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                FleetBuildService(self.client, value)
        self.assertEqual(self.http.requests, [])

    async def test_changed_record_identity_is_refused_on_reads_and_admission(self) -> None:
        mutations = [
            lambda r: r.update(id="build-wrong"),
            lambda r: r.update(kind="sessions"),
            lambda r: r["build"]["request"]["snapshot"].update(manifestDigest="0" * 64),
            lambda r: r["parent"].update(executionId="another-execution"),
            lambda r: r["build"].update(attempt=1.0),
            lambda r: r["build"]["request"]["parent"].update(generation=1.0),
            lambda r: r["build"]["policy"]["allocation"].update(generation=7),
            lambda r: r["build"].update(policyDigest="0" * 64),
        ]
        for mutate in mutations:
            record = copy.deepcopy(self.http.data["admission"]["record"])
            mutate(record)
            for operation in ("status", "submit"):
                self.http.override = (
                    record
                    if operation == "status"
                    else {"record": record, "command": self.http.data["admission"]["command"]}
                )
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    await getattr(self.service(), operation)(deadline=self.deadline())

    async def test_wrong_success_envelope_and_command_binding_are_refused(self) -> None:
        for response in [
            {"record": self.http.record, "replayed": False},
            self.http.record,
            {"record": self.http.record, "command": {"targetId": "another-build"}},
        ]:
            self.http.override = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                await self.service().submit(deadline=self.deadline())
        self.http.override = self.http.data["admission"]
        with self.assertRaises(ValueError):
            await self.service().status(deadline=self.deadline())

    async def test_lost_submit_reply_recovers_by_status_and_explicit_exact_replay(self) -> None:
        self.http.mode = "lost"
        service = self.service()
        retained = service.submission
        with self.assertRaises(httpx.RemoteProtocolError):
            await service.submit(deadline=self.deadline())
        self.assertEqual(len(self.http.requests), 1)
        recreated = FleetBuildService(self.client, retained)
        record = await recreated.status(deadline=self.deadline())
        self.assertEqual(record, self.http.record)
        replay = await recreated.submit(deadline=self.deadline())
        self.assertEqual(replay["id"], record["id"])
        posts = [r for r in self.http.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        self.assertFalse(any(r["path"].endswith("/deliver") for r in self.http.requests))

    async def test_prepare_cancel_is_read_only_and_uses_tool_not_parent_generation(self) -> None:
        service = self.service()
        # This is a derived transport fixture, not another controller export.
        # Change the admitted allocation and every corresponding commitment.
        admission = copy.deepcopy(self.http.data["admission"])
        policy = copy.deepcopy(admission["record"]["build"]["policy"])
        policy["allocation"]["generation"] = 7
        policy_digest = sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        admission["record"]["generation"] = 7
        admission["record"]["build"]["allocation"] = copy.deepcopy(policy["allocation"])
        admission["record"]["build"]["policy"] = copy.deepcopy(policy)
        admission["record"]["build"]["policyDigest"] = policy_digest
        admission["command"]["generation"] = 7
        admission["command"]["payload"]["policy"] = copy.deepcopy(policy)
        admission["command"]["payload"]["policyDigest"] = policy_digest
        self.http.data["admission"] = admission
        self.http.record = admission["record"]
        prepared = await service.prepare_cancel(
            "cancel-owned", "cancel-key", deadline=self.deadline()
        )
        self.assertEqual(
            prepared,
            {
                "schema": "hi/fleet/build-cancel/v1",
                "commandId": "cancel-owned",
                "idempotencyKey": "cancel-key",
                "generation": 7,
                "attempt": 1,
            },
        )
        self.assertEqual([r["method"] for r in self.http.requests], ["GET"])
        self.assertNotEqual(prepared["generation"], service.submission["parent"]["generation"])

    async def test_cancel_sends_exact_retained_body_without_new_status_or_cleanup_claim(
        self,
    ) -> None:
        service = self.service()
        intent = copy.deepcopy(self.http.data["cancelRequest"])
        result = await service.cancel(intent, deadline=self.deadline())
        self.assertEqual(result, self.http.data["cancelResponse"]["record"])
        self.assertEqual(result["status"], "cancelling")
        self.assertFalse(result["collectionVerified"])
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(self.http.requests[0]["body"], intent)
        self.assertTrue(self.http.requests[0]["path"].endswith("/cancel"))

    async def test_lost_cancel_replays_retained_body_despite_later_status(self) -> None:
        service = self.service()
        intent = copy.deepcopy(self.http.data["cancelRequest"])
        self.http.mode = "lost"
        with self.assertRaises(httpx.RemoteProtocolError):
            await service.cancel(intent, deadline=self.deadline())
        self.assertEqual(len(self.http.requests), 1)
        self.http.record["generation"] = 9
        recreated = self.service()
        result = await recreated.cancel(intent, deadline=self.deadline())
        self.assertEqual(result["id"], self.http.data["admission"]["record"]["id"])
        self.assertEqual([r["body"] for r in self.http.requests], [intent, intent])
        self.assertEqual([r["method"] for r in self.http.requests], ["POST", "POST"])

    async def test_cancel_rejects_changed_ack_and_invalid_intent(self) -> None:
        intent = copy.deepcopy(self.http.data["cancelRequest"])
        for changes in (
            {"generation": True},
            {"attempt": 1.0},
            {"commandId": "../x"},
            {"extra": "untrusted"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.service().cancel({**intent, **changes}, deadline=self.deadline())
        self.assertEqual(self.http.requests, [])
        self.http.override = copy.deepcopy(self.http.data["cancelResponse"])
        self.http.override["record"]["build"]["cancellation"]["idempotencyKey"] = "different"
        with self.assertRaises(ValueError):
            await self.service().cancel(intent, deadline=self.deadline())

    async def test_conflict_preserves_intent_without_automatic_retry(self) -> None:
        from agamemnon_client import AgamemnonAPIError

        self.http.mode = "conflict"
        service = self.service()
        with self.assertRaises(AgamemnonAPIError):
            await service.submit(deadline=self.deadline())
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(service.submission, self.http.data["submission"])

    async def test_expired_or_nonfinite_deadlines_refuse_before_http(self) -> None:
        service = self.service()
        for deadline in (time.monotonic() - 1, math.inf, math.nan):
            for operation in (service.submit, service.status):
                with self.subTest(deadline=deadline), self.assertRaises((ValueError, TimeoutError)):
                    await operation(deadline=deadline)
        self.assertEqual(self.http.requests, [])

    async def test_outer_deadline_cancels_a_stalled_real_sdk_request(self) -> None:
        self.http.mode = "stall"
        service = self.service()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            await service.status(deadline=start + 0.1)
        self.assertTrue(self.http.entered.is_set())
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertEqual(len(self.http.requests), 1)

    async def test_caller_cancellation_propagates_without_retry(self) -> None:
        self.http.mode = "stall"
        operation = asyncio.create_task(self.service().status(deadline=self.deadline()))
        try:
            observed = await asyncio.to_thread(self.http.entered.wait, 0.5)
            self.assertTrue(observed)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertEqual(len(self.http.requests), 1)
        finally:
            operation.cancel()
            with suppress(asyncio.CancelledError):
                await operation
