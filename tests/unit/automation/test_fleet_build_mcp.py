"""Test the optional adapter through actual initialized MCP protocol sessions."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
from agamemnon_client import AgamemnonClient, AgamemnonConfig
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.shared.message import SessionMessage

from hephaestus.automation.fleet_build_mcp import FleetBuildDispatcher, create_mcp_server
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_journal import WorkerJournal
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-private-key'


class FleetBuildMCPTests(unittest.IsolatedAsyncioTestCase):
    """Keep the SDK, journal, protocol sessions and HTTP client real."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="heph-build-mcp-")
        self.addCleanup(self.temporary.cleanup)
        self.journal = WorkerJournal(Path(self.temporary.name) / "journal")
        self.addCleanup(self.journal.close)
        self.http = BuildConsumerHTTP()
        self.addCleanup(self.http.close)
        self.client = AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=self.http.port, api_key=FIXTURE_AUTH_VALUE, timeout=1
            ),
            trust_env=False,
        )
        self.addAsyncCleanup(self.client.aclose)
        self.owner = FleetBuildOwner(
            FleetBuildService(self.client, self.http.data["submission"]),
            self.journal,
            "fixture-context",
            cancellation_ids=lambda: ("build-stop-1", "build-stop-key-1"),
        )

    def server(self) -> Server[Any]:
        return create_mcp_server(FleetBuildDispatcher({"fixture-context": self.owner}, timeout=1))

    async def test_installed_protocol_runtime_initializes_and_pings(self) -> None:
        async with create_connected_server_and_client_session(
            Server("fixture-control", version="1"), timedelta(seconds=1)
        ) as client:
            result = await client.send_ping()
            self.assertEqual(result.model_dump(exclude_none=True), {})
        self.assertEqual(self.http.requests, [])

    async def test_falsy_malformed_terminal_is_not_absence(self) -> None:
        dispatcher = FleetBuildDispatcher({"fixture-context": self.owner}, timeout=1)
        self.http.record["build"]["terminal"] = None
        absent = await dispatcher.dispatch("fleet_build_status", {"contextId": "fixture-context"})
        self.assertIsNone(absent["cleanup"])
        self.http.record = copy.deepcopy(self.http.data["terminalResponse"]["record"])
        terminal = await dispatcher.dispatch("fleet_build_status", {"contextId": "fixture-context"})
        self.assertEqual(terminal["cleanup"], "confirmed_empty")
        value: Any
        for value in ([], 0, False, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.http.record["build"]["terminal"] = value
                await dispatcher.dispatch("fleet_build_status", {"contextId": "fixture-context"})
        self.assertEqual(len(self.http.requests), 6)

    def terminal_record(self, outcome: str) -> dict[str, Any]:
        # Controlled variants follow validation and terminal persistence in the
        # bound Agamemnon source; the exported fixture stays unchanged.
        if outcome == "cancelled":
            return copy.deepcopy(self.http.data["terminalResponse"]["record"])
        record = copy.deepcopy(self.http.data["persistedGrantDocument"]["record"])
        fact = copy.deepcopy(self.http.data["terminalFact"])
        fact.update(
            commandId=record["commandId"],
            outcome=outcome,
            exitCode=0 if outcome == "completed" else None,
        )
        record["status"] = outcome
        record["build"].update(terminal=fact, reservation="released", evidenceState="incomplete")
        return record

    async def test_status_terminal_outcomes_follow_controller_contract(self) -> None:
        dispatcher = FleetBuildDispatcher({"fixture-context": self.owner}, timeout=1)
        for outcome in ("cancelled", "completed", "timed_out", "orphaned"):
            with self.subTest(outcome=outcome):
                self.http.record = self.terminal_record(outcome)
                if outcome == "orphaned":
                    with self.assertRaises(ValueError):
                        await dispatcher.dispatch(
                            "fleet_build_status", {"contextId": "fixture-context"}
                        )
                else:
                    result = await dispatcher.dispatch(
                        "fleet_build_status", {"contextId": "fixture-context"}
                    )
                    self.assertEqual(result["outcome"], outcome)
                    self.assertEqual(result["status"], outcome)
                    self.assertEqual(result["cleanup"], "confirmed_empty")
                    self.assertFalse(result["collectionVerified"])
        self.assertEqual([request["method"] for request in self.http.requests], ["GET"] * 4)

    async def test_protocol_terminal_outcomes_follow_controller_contract(self) -> None:
        async with create_connected_server_and_client_session(
            self.server(), timedelta(seconds=1)
        ) as client:
            for outcome in ("cancelled", "completed", "timed_out", "orphaned"):
                with self.subTest(outcome=outcome):
                    self.http.record = self.terminal_record(outcome)
                    result = await client.call_tool(
                        "fleet_build_status", {"contextId": "fixture-context"}
                    )
                    self.assertEqual(result.isError, outcome == "orphaned")
                    if outcome != "orphaned":
                        self.assertIsNotNone(result.structuredContent)
                        content = cast(dict[str, Any], result.structuredContent)
                        self.assertEqual(content["outcome"], outcome)
                        self.assertEqual(content["status"], outcome)
                        self.assertEqual(content["cleanup"], "confirmed_empty")
                        self.assertFalse(content["collectionVerified"])
        self.assertEqual([request["method"] for request in self.http.requests], ["GET"] * 4)

    async def test_three_tools_use_the_real_ordinary_owner(self) -> None:
        async with create_connected_server_and_client_session(
            self.server(), timedelta(seconds=2)
        ) as client:
            listed = await client.list_tools()
            self.assertEqual(
                {tool.name for tool in listed.tools},
                {"fleet_build_submit", "fleet_build_status", "fleet_build_cancel"},
            )
            for name, expected in (
                ("fleet_build_submit", "admitted"),
                ("fleet_build_status", "admitted"),
                ("fleet_build_cancel", "cancelling"),
            ):
                result = await client.call_tool(name, {"contextId": "fixture-context"})
                self.assertFalse(result.isError)
                self.assertIsNotNone(result.structuredContent)
                content = cast(dict[str, Any], result.structuredContent)
                self.assertEqual(content["status"], expected)
                self.assertFalse(content["collectionVerified"])
                self.assertNotIn("policy", content)
                self.assertNotIn("grant", content)
        self.assertEqual(
            [request["method"] for request in self.http.requests],
            ["POST", "GET", "GET", "POST"],
        )
        self.assertEqual(self.http.requests[-1]["body"], self.http.data["cancelRequest"])

    async def test_closed_arguments_and_unknown_tools_return_redacted_errors(self) -> None:
        sentinel = "sentinel-private-input"
        async with create_connected_server_and_client_session(
            self.server(), timedelta(seconds=1)
        ) as client:
            for name, arguments in (
                (sentinel, {"contextId": "fixture-context"}),
                ("fleet_build_submit", {"contextId": sentinel}),
                (
                    "fleet_build_cancel",
                    {"contextId": "fixture-context", "commandId": sentinel},
                ),
            ):
                result = await client.call_tool(name, arguments)
                self.assertTrue(result.isError)
                self.assertNotIn(sentinel, result.model_dump_json())
        self.assertEqual(self.http.requests, [])

    async def test_lost_submission_reports_uncertainty_and_replays_same_bytes(self) -> None:
        self.http.mode = "lost"
        async with create_connected_server_and_client_session(
            self.server(), timedelta(seconds=2)
        ) as client:
            lost = await client.call_tool("fleet_build_submit", {"contextId": "fixture-context"})
            self.assertTrue(lost.isError)
            self.assertNotIn("fixture-private-key", lost.model_dump_json())
            observed = await client.call_tool(
                "fleet_build_status", {"contextId": "fixture-context"}
            )
            self.assertFalse(observed.isError)
            replay = await client.call_tool("fleet_build_submit", {"contextId": "fixture-context"})
            self.assertFalse(replay.isError)
        posts = [request for request in self.http.requests if request["method"] == "POST"]
        self.assertEqual([request["body"] for request in posts], [self.http.data["submission"]] * 2)
        values = [
            json.loads(line)["value"]
            for line in (self.journal.directory / "receipts.jsonl").read_text().splitlines()
        ]
        self.assertTrue(any(value.get("submission") == posts[0]["body"] for value in values))

    async def test_protocol_cancellation_releases_request_without_cancelling_build(self) -> None:
        server = self.server()
        entered = asyncio.Event()
        interrupted = asyncio.Event()
        identity: list[int | str] = []

        async def hold_request(_request: httpx.Request) -> None:
            identity.append(server.request_context.request_id)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                interrupted.set()

        self.client._client.event_hooks["request"] = [hold_request]
        async with create_connected_server_and_client_session(
            server, timedelta(seconds=2)
        ) as client:
            pending = asyncio.create_task(
                client.call_tool("fleet_build_submit", {"contextId": "fixture-context"})
            )
            try:
                try:
                    await asyncio.wait_for(entered.wait(), 0.5)
                except TimeoutError:
                    self.fail("protocol call did not reach the ordinary SDK request")
                await client.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=identity[0])
                        )
                    )
                )
                await asyncio.wait_for(interrupted.wait(), 0.5)
                with self.assertRaises(McpError):
                    await pending
                self.assertEqual(self.http.requests, [])
                self.client._client.event_hooks["request"] = []
                replay = await client.call_tool(
                    "fleet_build_submit", {"contextId": "fixture-context"}
                )
                self.assertFalse(replay.isError)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual([r["body"] for r in self.http.requests], [self.http.data["submission"]])
        self.assertTrue(all("/cancel" not in r["path"] for r in self.http.requests))

    async def test_debug_and_malformed_wire_logs_are_scoped_and_redacted(self) -> None:
        wire = "controlled-private-wire-sentinel"
        unrelated = "concurrent-unrelated-sentinel"
        start = asyncio.Event()
        observed_errors: list[str] = []
        root_logger = logging.getLogger()
        server_logger = logging.getLogger("mcp.server.lowlevel.server")

        async def other_task() -> None:
            await start.wait()
            root_logger.debug("ordinary root event %s", unrelated)
            server_logger.debug("ordinary named event %s", unrelated)

        async def receive(message: object) -> None:
            if isinstance(message, Exception):
                observed_errors.append(str(message))

        outside = asyncio.create_task(other_task())
        try:
            with self.assertLogs(level="DEBUG") as captured:
                levels = root_logger.level, server_logger.level
                async with create_connected_server_and_client_session(
                    self.server(), timedelta(seconds=1), message_handler=receive
                ) as client:
                    self.assertEqual((root_logger.level, server_logger.level), levels)
                    await client.call_tool("fleet_build_submit", {"contextId": wire})
                    # Send a malformed wire request through the real session transport.
                    # The public typed call API correctly cannot construct this shape.
                    await client._write_stream.send(
                        SessionMessage(
                            types.JSONRPCMessage(
                                types.JSONRPCRequest(
                                    jsonrpc="2.0",
                                    id=9000,
                                    method="tools/call",
                                    params={"name": wire, "arguments": []},
                                )
                            )
                        )
                    )
                    await client.send_ping()
                    self.assertTrue(
                        any("Invalid request parameters" in error for error in observed_errors)
                    )
                    start.set()
                    await asyncio.wait_for(outside, 0.5)
                self.assertEqual((root_logger.level, server_logger.level), levels)
            self.assertTrue(any(record.name == "root" for record in captured.records))
            self.assertTrue(any(record.name == server_logger.name for record in captured.records))
            self.assertNotIn(wire, "\n".join(captured.output))
            self.assertEqual(sum(unrelated in line for line in captured.output), 2)
            with self.assertLogs(level="DEBUG") as restored:
                root_logger.debug("post-exit root %s", unrelated)
                server_logger.debug("post-exit named %s", unrelated)
            self.assertEqual(sum(unrelated in line for line in restored.output), 2)
            self.assertEqual(self.http.requests, [])
        finally:
            start.set()
            await asyncio.gather(outside, return_exceptions=True)
