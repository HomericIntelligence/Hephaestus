"""Join actual build evidence with an ordinary SDK caller for later job tests.

HTTP admission and scheduler identity are controlled fixtures. The recipe,
journal, publisher and collector are real.
This fixture does not qualify a deployed worker, Slurm, Pyxis, or either cluster.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from agamemnon_client import AgamemnonClient, AgamemnonConfig

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_contract import digest
from hephaestus.automation.fleet_build_service import FleetBuildOwner, FleetBuildService
from hephaestus.automation.fleet_build_storage import journal_directory
from hephaestus.automation.fleet_journal import WorkerJournal
from tests.unit.automation.test_fleet_build_publication import PublisherCase
from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP

T = TypeVar("T")


# Public test-only authentication value for the local fixture server.
FIXTURE_AUTH_VALUE = 'fixture-private-key'


class JobBuildCase:
    """Own fixture resources before a job borrows them; join them after it ends."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        publisher_root = self.root / "publisher"
        publisher_root.mkdir(mode=0o700)
        self.publisher = PublisherCase(publisher_root)
        self.http: BuildConsumerHTTP | None = None
        self.loop = asyncio.new_event_loop()
        self.loop_ready = threading.Event()
        self.loop_thread = threading.Thread(target=self._serve_loop, name="fixture-build-owner")
        self.loop_thread.start()
        try:
            assert self.loop_ready.wait(2), "the fixture owner loop did not start"
            # Bind only the fresh copy to this test's actual canonical source.
            self.publisher.close_owners()
            payload = self.publisher.command["payload"]
            payload["policy"]["workspace"]["parentWorkspace"] = str(self.publisher.source)
            payload["parent"]["claim"]["workspace"] = str(self.publisher.source)
            payload["policyDigest"] = digest(payload["policy"])
            self.publisher.open()
            self.http = BuildConsumerHTTP()
            self._bind_http()
            self.call(self._open_consumer())
        except BaseException:
            self.close()
            raise

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.loop_ready.set)
        self.loop.run_forever()

    def call(self, operation: Coroutine[Any, Any, T], *, timeout: float = 5) -> T:
        """Call fixture setup and observations on the one existing owner loop."""
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        return future.result(timeout=timeout)

    def _bind_http(self) -> None:
        """Build a controlled admission for the actual fresh snapshot and source."""
        assert self.http is not None
        command = copy.deepcopy(self.publisher.command)
        payload = command["payload"]
        self.submission = copy.deepcopy(self.http.data["submission"])
        self.submission.update(
            workspaceId=payload["policy"]["workspace"]["id"],
            idempotencyKey="publisher-source",
            parent={
                name: copy.deepcopy(payload["parent"][name])
                for name in ("targetKind", "targetId", "sessionId", "executionId", "generation")
            },
            snapshot=copy.deepcopy(payload["snapshot"]),
        )
        record = copy.deepcopy(self.http.data["admission"]["record"])
        record.update(
            id=command["targetId"],
            commandId=command["commandId"],
            generation=command["generation"],
            parent=copy.deepcopy(payload["parent"]),
        )
        record["build"].update(
            request=copy.deepcopy(self.submission),
            attempt=payload["attempt"],
            policy=copy.deepcopy(payload["policy"]),
            policyDigest=payload["policyDigest"],
            parametersDigest=payload["parametersDigest"],
            allocation=copy.deepcopy(payload["policy"]["allocation"]),
            snapshotWorkspace=payload["snapshotWorkspace"],
        )
        self.http.data["submission"] = copy.deepcopy(self.submission)
        self.http.data["admission"] = {"record": record, "command": command}
        self.http.record = copy.deepcopy(record)

    async def _open_consumer(self) -> None:
        assert self.http is not None
        self.client = AgamemnonClient(
            AgamemnonConfig(
                host="127.0.0.1", port=self.http.port, api_key=FIXTURE_AUTH_VALUE, timeout=2
            ),
            trust_env=False,
        )
        directory = self.root / "consumer-journal"
        with journal_directory(directory, time.monotonic() + 5):
            self.journal = WorkerJournal(directory)
        self.owner = FleetBuildOwner(
            FleetBuildService(self.client, self.submission),
            self.journal,
            "job-context",
            cancellation_ids=lambda: ("job-stop", "job-stop-key"),
        )

    def publish(self) -> dict[str, Any]:
        """Run the harmless recipe and expose only its actual terminal fact."""
        assert self.http is not None
        self.publisher.service.handle(self.publisher.command)
        fact = copy.deepcopy(self.publisher.state()["terminal"])
        assert fact["outcome"] == "completed"
        self.http.record["status"] = fact["outcome"]
        self.http.record["build"]["terminal"] = copy.deepcopy(fact)
        return fact

    def handoff(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read the real publisher reference and supervisor lease for collection."""
        state = self.publisher.state()
        fact, payload = state["terminal"], self.publisher.command["payload"]
        reference = {"id": fact["receipt"]["reference"], "digest": fact["receipt"]["digest"]}
        expected = {
            "buildId": self.publisher.command["targetId"],
            "attempt": payload["attempt"],
            "commandId": self.publisher.command["commandId"],
            "leaseId": state["lease"]["leaseId"],
            "parent": copy.deepcopy(payload["parent"]),
            "policy": copy.deepcopy(payload["policy"]),
            "policyDigest": payload["policyDigest"],
            "snapshot": copy.deepcopy(payload["snapshot"]),
        }
        return reference, expected

    def collect(self, *, deadline: float) -> dict[str, Any]:
        """Collect on the caller thread while its exclusive fixture source is held."""
        reference, expected = self.handoff()
        return collect_build_result(
            self.publisher.evidence_root,
            reference=reference,
            expected=expected,
            source=self.publisher.source,
            snapshot_policy=self.publisher.policy,
            deadline=deadline,
        )

    async def _close_consumer(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if hasattr(self, "client"):
                await self.client.aclose()
        finally:
            if hasattr(self, "journal"):
                self.journal.close()

    def close(self) -> None:
        """Acknowledge local coroutine exit before closing the SDK and journal."""
        if self.http is not None:
            self.http.release.set()
        try:
            if self.loop_thread.is_alive():
                try:
                    self.call(self._close_consumer())
                finally:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    self.loop_thread.join(5)
                    assert not self.loop_thread.is_alive(), "the fixture owner loop did not stop"
            self.loop.close()
        finally:
            try:
                if self.http is not None:
                    self.http.close()
            finally:
                self.publisher.close()


@pytest.fixture
def job_build_case(tmp_path: Path) -> Iterator[JobBuildCase]:
    """Close every owned process and thread after a failed or passing test."""
    case = JobBuildCase(tmp_path)
    try:
        yield case
    finally:
        case.close()


def test_real_sdk_publication_and_collection_share_one_fresh_snapshot(
    job_build_case: JobBuildCase,
) -> None:
    """Prove the proposed job fixture uses actual output and retained intent."""
    case = job_build_case
    assert case.http is not None
    deadline = time.monotonic() + 8
    admission = case.call(case.owner.submit(deadline=deadline))
    assert admission["build"]["request"] == case.submission
    assert len(case.http.requests) == 1
    values = [
        json.loads(line)["value"]
        for line in (case.root / "consumer-journal" / "receipts.jsonl").read_text().splitlines()
    ]
    assert values[-1]["submission"] == case.submission
    fact = case.publish()
    observed = case.call(case.owner.status(deadline=deadline))
    assert observed["build"]["terminal"] == fact
    assert observed["collectionVerified"] is False
    collected = case.collect(deadline=deadline)
    assert collected["status"] == "verified_current"
    assert collected["sourceCurrent"] is True
    assert collected["outcome"] == "completed"
    assert type(collected["exitCode"]) is int and collected["exitCode"] == 0
    reference, identity = case.handoff()
    assert collected["reference"] == reference
    assert collected["identity"] == identity
    assert case.publisher.scheduler.observations[-1]["schedulerTerminal"] is True
    assert case.publisher.scheduler.observations[-1]["kernelEmpty"] is True
    assert [request["method"] for request in case.http.requests] == ["POST", "GET"]
    assert not any(request["path"].endswith("/cancel") for request in case.http.requests)
