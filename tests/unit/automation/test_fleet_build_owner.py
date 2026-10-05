"""Exercise caller-owned intent retention with the real SDK and private journal."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import tempfile
import time
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from agamemnon_client import AgamemnonClient, AgamemnonConfig

from hephaestus.automation.fleet_build_mcp import FleetBuildDispatcher
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_journal import WorkerJournal
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-private-key'


class FleetBuildOwnerTests(unittest.IsolatedAsyncioTestCase):
    """Borrow one fixture-owned journal; do not open a second active writer."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="heph-build-owner-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "journal"
        self.journal = WorkerJournal(self.directory)
        self.addCleanup(lambda: self.journal.close())
        self.http = BuildConsumerHTTP()
        self.addCleanup(self.http.close)
        self.client = AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=self.http.port, api_key=FIXTURE_AUTH_VALUE, timeout=1
            ),
            trust_env=False,
        )
        self.addAsyncCleanup(self.client.aclose)
        self.identities = 0

    def ids(self) -> tuple[str, str]:
        """Supply retained fixture identities and count preparation attempts."""
        self.identities += 1
        return "build-stop-1", "build-stop-key-1"

    def owner(self, submission: dict[str, Any] | None = None) -> FleetBuildOwner:
        service = FleetBuildService(self.client, submission or self.http.data["submission"])
        return FleetBuildOwner(service, self.journal, "fixture-context", cancellation_ids=self.ids)

    def deadline(self) -> float:
        return time.monotonic() + 1

    def reopen(self) -> None:
        """Release the old writer before reconstructing its retained records."""
        self.journal.close()
        self.journal = WorkerJournal(self.directory)

    def durable_values(self) -> list[dict[str, Any]]:
        """Read flushed bytes rather than the owner's in-memory record view."""
        return [
            json.loads(line)["value"]
            for line in (self.directory / "receipts.jsonl").read_text().splitlines()
        ]

    async def test_real_journal_and_installed_sdk_controls_are_usable(self) -> None:
        self.journal.append("consumer-fixture", {"retained": "control"})
        self.reopen()
        self.assertEqual(self.journal.records[-1]["value"], {"retained": "control"})
        actual = await self.client.fleet_build_status(self.http.record["id"])
        self.assertEqual(actual, self.http.record)
        self.assertEqual(len(self.http.requests), 1)

    async def test_submission_is_in_the_real_journal_before_http(self) -> None:
        async def observe(request: httpx.Request) -> None:
            if request.method == "POST":
                self.assertTrue(
                    any(
                        value.get("submission") == self.http.data["submission"]
                        for value in self.durable_values()
                    )
                )

        self.client._client.event_hooks["request"] = [observe]
        result = await self.owner().submit(deadline=self.deadline())
        self.assertEqual(result, self.http.data["admission"]["record"])
        self.assertEqual(len(self.http.requests), 1)
        admission = self.durable_values()[-1]["admission"]
        self.assertEqual(admission["buildId"], result["id"])
        self.assertEqual(admission["commandId"], result["id"] + "-start")
        self.assertEqual(admission["parent"], result["parent"])
        self.assertEqual(admission["snapshot"], self.http.data["submission"]["snapshot"])
        self.assertEqual(admission["policy"], result["build"]["policy"])
        self.assertEqual(admission["policyDigest"], result["build"]["policyDigest"])
        self.assertNotIn("status", admission)

    async def test_lost_submission_restarts_with_the_same_durable_request(self) -> None:
        self.http.mode = "lost"
        with self.assertRaises(httpx.RemoteProtocolError):
            await self.owner().submit(deadline=self.deadline())
        self.reopen()
        recovered = self.owner()
        record = await recovered.status(deadline=self.deadline())
        replay = await recovered.submit(deadline=self.deadline())
        self.assertEqual(record["id"], replay["id"])
        posts = [r for r in self.http.requests if r["method"] == "POST"]
        self.assertEqual([r["body"] for r in posts], [self.http.data["submission"]] * 2)
        self.assertEqual(len(self.http.requests), 3)

    async def test_changed_context_intent_is_refused_after_restart_before_http(self) -> None:
        await self.owner().submit(deadline=self.deadline())
        self.reopen()
        changed = copy.deepcopy(self.http.data["submission"])
        changed["snapshot"]["manifestDigest"] = "0" * 64
        with self.assertRaises(ValueError):
            self.owner(changed)
        self.assertEqual(len(self.http.requests), 1)

    async def test_coherent_changed_admission_is_refused_after_restart(self) -> None:
        await self.owner().submit(deadline=self.deadline())
        self.reopen()
        changed = copy.deepcopy(self.http.record)
        changed["generation"] = 7
        changed["build"]["allocation"]["generation"] = 7
        changed["build"]["policy"]["allocation"]["generation"] = 7
        changed["build"]["policyDigest"] = sha256(
            json.dumps(
                changed["build"]["policy"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        self.http.record = changed
        # This derived response is coherent on its own; only retained identity differs.
        current = await FleetBuildService(self.client, self.http.data["submission"]).status(
            deadline=self.deadline()
        )
        self.assertEqual(current["generation"], 7)
        with self.assertRaises(ValueError):
            await self.owner().status(deadline=self.deadline())
        self.assertEqual(len(self.http.requests), 3)

    async def test_legal_status_changes_preserve_the_retained_admission(self) -> None:
        await self.owner().submit(deadline=self.deadline())
        self.assertTrue(self.durable_values(), "submission and admission must be retained")
        admission = copy.deepcopy(self.durable_values()[-1]["admission"])
        self.reopen()
        self.http.record["status"] = "completed"
        self.http.record["build"]["cleanup"] = "confirmed_empty"
        self.http.record["build"]["outcome"] = "completed"
        observed = await self.owner().status(deadline=self.deadline())
        self.assertEqual(observed["status"], "completed")
        self.assertFalse(observed["collectionVerified"])
        self.assertEqual(self.durable_values()[-1]["admission"], admission)

    async def test_retained_context_corruption_cannot_replace_the_original_intent(self) -> None:
        await self.owner().submit(deadline=self.deadline())
        self.assertTrue(self.durable_values(), "submission and admission must be retained")
        saved = copy.deepcopy(self.durable_values()[-1])
        saved["admission"]["generation"] = 1.0
        self.journal.append("build-consumer", saved)
        self.reopen()
        with self.assertRaises(ValueError):
            self.owner()
        self.assertEqual(len(self.http.requests), 1)

    async def test_new_cancel_prepares_and_persists_the_complete_body_before_post(self) -> None:
        async def observe(request: httpx.Request) -> None:
            if request.url.path.endswith("/cancel"):
                sent = json.loads(request.content)
                self.assertTrue(
                    any(value.get("cancellation") == sent for value in self.durable_values())
                )

        self.client._client.event_hooks["request"] = [observe]
        result = await self.owner().cancel(deadline=self.deadline())
        self.assertEqual(result, self.http.data["cancelResponse"]["record"])
        self.assertEqual(result["status"], "cancelling")
        self.assertEqual(self.identities, 1)
        self.assertEqual([r["method"] for r in self.http.requests], ["GET", "POST"])
        self.assertEqual(self.http.requests[-1]["body"], self.http.data["cancelRequest"])

    async def test_lost_cancel_restart_replays_without_preparing_a_new_intent(self) -> None:
        self.http.mode = "lost"
        with self.assertRaises(httpx.RemoteProtocolError):
            await self.owner().cancel(deadline=self.deadline())
        first_body = self.http.requests[-1]["body"]
        self.reopen()
        result = await self.owner().cancel(deadline=self.deadline())
        self.assertEqual(result["status"], "cancelling")
        self.assertEqual(self.identities, 1)
        self.assertEqual([r["method"] for r in self.http.requests], ["GET", "POST", "POST"])
        self.assertEqual(self.http.requests[-1]["body"], first_body)

    async def test_retained_cancellation_cannot_be_replaced_after_restart(self) -> None:
        await self.owner().cancel(deadline=self.deadline())
        self.assertTrue(self.durable_values(), "complete cancellation must be retained")
        saved = copy.deepcopy(self.durable_values()[-1])
        saved["cancellation"]["commandId"] = "replacement-stop"
        saved["cancellation"]["idempotencyKey"] = "replacement-stop-key"
        self.journal.append("build-consumer", saved)
        self.reopen()
        with self.assertRaises(ValueError):
            self.owner()
        self.assertEqual(len(self.http.requests), 2)

    async def test_competing_cancellations_share_the_first_retained_intent(self) -> None:
        owner = self.owner()
        first, second = await asyncio.gather(
            owner.cancel(deadline=self.deadline()), owner.cancel(deadline=self.deadline())
        )
        self.assertEqual(first, self.http.data["cancelResponse"]["record"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.identities, 1)
        posts = [r["body"] for r in self.http.requests if r["method"] == "POST"]
        self.assertEqual(posts, [self.http.data["cancelRequest"]] * 2)

    async def test_storage_error_prevents_http_and_poisons_the_current_owner(self) -> None:
        owner = self.owner()
        with patch("os.fsync", side_effect=OSError("controlled file sync failure")):
            with self.assertRaises(OSError):
                await owner.submit(deadline=self.deadline())
        with self.assertRaises(RuntimeError):
            await owner.submit(deadline=self.deadline())
        self.assertEqual(self.http.requests, [])

    async def test_lock_wait_is_inside_the_whole_operation_deadline(self) -> None:
        owner = self.owner()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_request(_request: httpx.Request) -> None:
            entered.set()
            await release.wait()

        self.client._client.event_hooks["request"] = [hold_request]
        first = asyncio.create_task(owner.submit(deadline=time.monotonic() + 2))
        try:
            try:
                await asyncio.wait_for(entered.wait(), 0.5)
            except TimeoutError:
                self.fail("the first submission did not reach the actual SDK request")
            with self.assertRaises(TimeoutError):
                await owner.cancel(deadline=time.monotonic() + 0.04)
            self.assertEqual(self.identities, 0)
        finally:
            release.set()
            await asyncio.wait_for(first, 1)
        self.assertEqual(len(self.http.requests), 1)

    async def test_expired_owner_deadline_cannot_append_or_send(self) -> None:
        owner = self.owner()
        with self.assertRaises(TimeoutError):
            await owner.submit(deadline=time.monotonic() - 1)
        self.assertEqual(self.durable_values(), [])
        self.assertEqual(self.http.requests, [])

    async def test_dispatcher_uses_the_ordinary_owner_and_projects_private_results(self) -> None:
        dispatcher = FleetBuildDispatcher({"fixture-context": self.owner()}, timeout=1)
        result = await dispatcher.dispatch("fleet_build_submit", {"contextId": "fixture-context"})
        self.assertIn("buildId", result)
        self.assertEqual(result["buildId"], self.http.data["admission"]["record"]["id"])
        self.assertEqual(result["status"], "admitted")
        self.assertFalse(result["collectionVerified"])
        self.assertNotIn("command", result)
        self.assertNotIn("policy", result)
        self.assertNotIn("grant", result)
        stopped = await dispatcher.dispatch("fleet_build_cancel", {"contextId": "fixture-context"})
        self.assertEqual(stopped["status"], "cancelling")
        self.assertIsNone(stopped["cleanup"])
        status = await dispatcher.dispatch("fleet_build_status", {"contextId": "fixture-context"})
        self.assertEqual(status, stopped)
        self.assertEqual([r["method"] for r in self.http.requests], ["POST", "GET", "POST", "GET"])

    async def test_actual_terminal_export_stays_an_unverified_observation(self) -> None:
        self.http.record = copy.deepcopy(self.http.data["terminalResponse"]["record"])
        dispatcher = FleetBuildDispatcher({"fixture-context": self.owner()}, timeout=1)
        result = await dispatcher.dispatch("fleet_build_status", {"contextId": "fixture-context"})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(result["cleanup"], "confirmed_empty")
        self.assertFalse(result["collectionVerified"])
        self.assertEqual([r["method"] for r in self.http.requests], ["GET"])

    async def test_dispatcher_rejects_unknown_or_untrusted_arguments_before_io(self) -> None:
        dispatcher = FleetBuildDispatcher({"fixture-context": self.owner()}, timeout=1)
        cases: list[tuple[str, dict[str, Any]]] = [
            ("run_shell", {"contextId": "fixture-context"}),
            ("fleet_build_submit", {"contextId": "missing"}),
            ("fleet_build_submit", {"contextId": "fixture-context", "parent": {}}),
            ("fleet_build_status", {"contextId": "fixture-context", "url": "http://x"}),
            ("fleet_build_cancel", {"contextId": "fixture-context", "generation": 1}),
            ("fleet_build_cancel", {"contextId": "fixture-context", "commandId": "new"}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                await dispatcher.dispatch(name, arguments)
        self.assertEqual(self.http.requests, [])

    async def test_dispatcher_rejects_unbounded_timeouts(self) -> None:
        for value in (0, -1, math.inf, math.nan, True, 31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FleetBuildDispatcher({"fixture-context": self.owner()}, timeout=value)
        self.assertEqual(self.http.requests, [])

    async def test_dispatcher_requires_a_durable_owner_for_cancel(self) -> None:
        dispatcher = FleetBuildDispatcher({"fixture-context": None}, timeout=1)
        with self.assertRaises(RuntimeError):
            await dispatcher.dispatch("fleet_build_cancel", {"contextId": "fixture-context"})
        self.assertEqual(self.http.requests, [])

    async def test_dispatcher_exposes_only_three_closed_context_tools(self) -> None:
        tools = FleetBuildDispatcher({"fixture-context": self.owner()}, timeout=1).tools()
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"fleet_build_submit", "fleet_build_status", "fleet_build_cancel"},
        )
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(schema["required"], ["contextId"])
            self.assertEqual(set(schema["properties"]), {"contextId"})
            self.assertFalse(schema["additionalProperties"])
