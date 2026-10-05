"""Keep borrowed build execution and its completion evidence bound together.

Admission and scheduler identities are controlled fixtures. Snapshot bytes,
recipe execution, private journals, HTTP SDK requests and collection are real.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from hephaestus.automation.fleet_build_jobs import (
    FleetBuildEvidence,
    FleetBuildJobContext,
    FleetBuildJobRunner,
)
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from tests.unit.automation.test_fleet_build_job_fixture import JobBuildCase


class PreparedCase:
    """Supply actual exclusion and an already-open owner lifetime to one job."""

    def __init__(self, root: Path) -> None:
        self.build = JobBuildCase(root)
        self.source_lock = self.build.root / "source-access.lock"
        self.evidence_lock = self.build.root / "evidence-access.lock"
        self.stop_publication = threading.Event()
        self.published = threading.Event()
        self.publisher_error: BaseException | None = None
        self.exit_failure = False
        self.change_after_publication = False
        self.source_deadlines: list[float] = []
        self.evidence_deadlines: list[float] = []
        self.publication = threading.Thread(
            target=self._publish_after_status_read, name="fixture-publisher"
        )
        self.publication.start()

    def _publish_after_status_read(self) -> None:
        """Wait for POST then GET so the admission reply has reset its fixture record."""
        http = self.build.http
        assert http is not None
        while not self.stop_publication.is_set():
            if len(http.requests) < 2:
                self.stop_publication.wait(0.01)
                continue
            try:
                assert http.requests[0]["method"] == "POST"
                assert http.requests[0]["path"].endswith("/submit")
                assert http.requests[1]["method"] == "GET"
                assert http.requests[1]["path"].endswith(
                    "/" + self.build.publisher.command["targetId"]
                )
                self.build.publish()
                if self.change_after_publication:
                    path = self.build.publisher.source / "justfile"
                    path.write_bytes(path.read_bytes() + b"\n# changed after execution\n")
            except BaseException as error:
                self.publisher_error = error
            finally:
                self.published.set()
            return

    @contextmanager
    def source_lease(self, *, deadline: float, shutdown: threading.Event) -> Iterator[None]:
        """Use a real exclusion lock while the job holds the fixture source."""
        assert deadline > time.monotonic() and not shutdown.is_set()
        self.source_deadlines.append(deadline)
        with file_lock(self.source_lock, blocking=False, require_exclusive=True):
            yield
            if self.exit_failure:
                raise RuntimeError("fixture source lease exit failed")

    @contextmanager
    def result_handoff(
        self, record: dict[str, Any], *, deadline: float, shutdown: threading.Event
    ) -> Iterator[FleetBuildEvidence]:
        """Transfer the actual supervisor lease and receipt under evidence exclusion."""
        assert not shutdown.is_set()
        assert self.published.wait(max(0, deadline - time.monotonic()))
        assert self.publisher_error is None, self.publisher_error
        assert record["build"]["terminal"] == self.build.publisher.state()["terminal"]
        reference, expected = self.build.handoff()
        self.evidence_deadlines.append(deadline)
        with file_lock(self.evidence_lock, blocking=False, require_exclusive=True):
            yield FleetBuildEvidence(
                evidence_root=self.build.publisher.evidence_root,
                reference=reference,
                expected=expected,
            )

    def runner(self) -> FleetBuildJobRunner:
        """Construct only the product adapter over resources that already exist."""
        command = self.build.publisher.command
        context = FleetBuildJobContext(
            owner=self.build.owner,
            repository=command["payload"]["policy"]["workspace"]["repository"],
            source=self.build.publisher.source,
            submission=copy.deepcopy(self.build.submission),
            parent=copy.deepcopy(command["payload"]["parent"]),
            snapshot=self.build.publisher.snapshot,
            source_lease=self.source_lease,
            result_handoff=self.result_handoff,
        )
        return FleetBuildJobRunner(loop=self.build.loop, contexts={"job-context": context})

    def job(self) -> BuildTestJob:
        return BuildTestJob(
            repo=self.build.publisher.command["payload"]["policy"]["workspace"]["repository"],
            cwd=self.build.publisher.source,
            argv=("just", "test-unit"),
            timeout_s=8,
            expected_head_sha=str(self.build.submission["snapshot"]["baseCommit"]),
            descr="fleet_unit_recipe",
            fleet_context_id="job-context",
        )

    def collect(self, *, deadline: float) -> dict[str, Any]:
        """Hold both real locks for the independent completion oracle."""
        with (
            file_lock(self.source_lock, blocking=False, require_exclusive=True),
            file_lock(self.evidence_lock, blocking=False, require_exclusive=True),
        ):
            return self.build.collect(deadline=deadline)

    def close(self) -> None:
        self.stop_publication.set()
        self.publication.join(10)
        assert not self.publication.is_alive(), "the fixture publisher did not stop"
        try:
            assert self.publisher_error is None, self.publisher_error
        finally:
            self.build.close()


@pytest.fixture
def prepared_case(tmp_path: Path) -> Iterator[PreparedCase]:
    """Keep the actual fixture lifetime through each public job completion."""
    case = PreparedCase(tmp_path)
    try:
        yield case
    finally:
        case.close()


def lock_is_held(path: Path) -> bool:
    """Attempt independent acquisition through a new real lock descriptor."""
    try:
        with file_lock(path, blocking=False, require_exclusive=True):
            return False
    except LockUnavailableError:
        return True


@contextmanager
def submitted_pool(
    case: PreparedCase, monkeypatch: pytest.MonkeyPatch, *, failure: str = ""
) -> Iterator[tuple[WorkerPool, CompletionQueue, list[dict[str, Any]]]]:
    """Observe real receipt writes and reject any local heavy-tool boundary."""
    completion = CompletionQueue(maxsize=2)
    observations: list[dict[str, Any]] = []
    real_write = write_secure

    def no_local_execution(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("selected Fleet work reached the local process boundary")

    def observe_write(path: Path, content: str, *args: Any, **kwargs: Any) -> Any:
        payload = json.loads(content)
        state = payload.get("fleet_receipt_state")
        observations.append(
            {
                "path": path,
                "payload": payload,
                "source_held": lock_is_held(case.source_lock),
                "evidence_held": lock_is_held(case.evidence_lock),
            }
        )
        if state == failure:
            raise OSError("fixture receipt write failed before publication")
        returned = real_write(path, content, *args, **kwargs)
        if state == "finalized" and failure == "finalized_after_write":
            raise OSError("fixture finalization reply lost after the write")
        return returned

    monkeypatch.setattr(worker_pool, "run_subprocess", no_local_execution)
    monkeypatch.setattr(worker_pool, "write_secure", observe_write)
    pool = WorkerPool(
        fleet_build_runner=case.runner(),
        size=1,
        shutdown=threading.Event(),
        completion_q=completion,
        lock_dir=case.build.root / "pipeline-locks",
        evidence_receipt_dir=case.build.root / "pipeline-receipts",
    )
    try:
        yield pool, completion, observations
    finally:
        pool.shutdown()


def test_submitted_fleet_job_keeps_verified_collection_through_receipt_finalization(
    prepared_case: PreparedCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain independently collected bytes and observe both real exclusion leases."""
    case = prepared_case
    with submitted_pool(case, monkeypatch) as (pool, completions, writes):
        started = time.monotonic()
        handle = pool.submit(case.job(), StageName.IMPLEMENTATION, claim_key="fixture#17")
        completed, result = completions.get(timeout=10)
        assert completed is handle
        assert result.ok is True, "a valid borrowed Fleet capability was not executed"
        assert result.worker_id.startswith("hephaestus-pipeline-worker")
        assert isinstance(result.value, dict)
        collection = case.collect(deadline=time.monotonic() + 5)
        assert result.value == collection
        assert collection["status"] == "verified_current"
        assert collection["sourceCurrent"] is True
        assert collection["outcome"] == "completed"
        assert type(collection["exitCode"]) is int and collection["exitCode"] == 0
        pending = next(
            row for row in writes if row["payload"].get("fleet_receipt_state") == "pending"
        )
        final = writes[-1]
        assert pending["source_held"] and pending["evidence_held"]
        assert pending["payload"]["ok"] is False and pending["payload"]["succeeded"] is False
        assert final["path"] == pending["path"]
        assert final["payload"]["fleet_receipt_state"] == "finalized"
        assert final["payload"]["ok"] is True and final["payload"]["succeeded"] is True
        assert final["payload"]["fleet_collection"] == collection
        assert final["payload"]["tested_patch_sha256"] is None
        assert not final["source_held"] and not final["evidence_held"]
        assert case.source_deadlines == case.evidence_deadlines
        assert len(case.source_deadlines) == 1
        assert started < case.source_deadlines[0]
        assert case.build.http is not None
        assert sum(row["method"] == "POST" for row in case.build.http.requests) == 1
        assert not any(row["path"].endswith("/cancel") for row in case.build.http.requests)
        assert len(list((case.build.root / "pipeline-receipts").glob("*.json"))) == 1
        with pytest.raises(Empty):
            completions.get_nowait()


@pytest.mark.parametrize(
    "change",
    [
        "command",
        "context",
        "repository",
        "cwd",
        "head",
        "immutable",
        "host_runner",
        "archive",
        "commitment",
    ],
)
def test_invalid_fleet_job_never_submits_or_uses_local_execution(
    prepared_case: PreparedCase, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Reject incompatible registration and changed actual snapshot bytes."""
    case, job = prepared_case, prepared_case.job()
    if change == "command":
        job = replace(job, argv=("just", "ci-all"))
    elif change == "context":
        job = replace(job, fleet_context_id="unregistered-context")
    elif change == "repository":
        job = replace(job, repo="different/repository")
    elif change == "cwd":
        job = replace(job, cwd=case.build.root)
    elif change == "head":
        job = replace(job, expected_head_sha="0" * 40)
    elif change == "immutable":
        job = replace(job, immutable_source=True)
    elif change == "host_runner":
        job = replace(job, verified_runner_source_revision="0" * 40)
    elif change == "archive":
        archive = case.build.publisher.snapshot.artifact / "source.tar"
        raw = archive.read_bytes()
        assert raw.count(b"fixture build") == 1
        archive.write_bytes(raw.replace(b"fixture build", b"changed build", 1))
    elif change == "commitment":
        case.build.submission["snapshot"]["manifestDigest"] = "0" * 64
    with submitted_pool(case, monkeypatch) as (pool, completions, _writes):
        handle = pool.submit(job, StageName.IMPLEMENTATION)
        completed, result = completions.get(timeout=10)
        assert completed is handle
        assert result.ok is False
        assert not result.interrupted
        assert case.build.http is not None and case.build.http.requests == []
        assert case.build.publisher.scheduler.starts == []
        assert not any(
            json.loads(path.read_text()).get("succeeded") is True
            for path in (case.build.root / "pipeline-receipts").glob("*.json")
        )


@pytest.mark.parametrize("failure", ["lease_exit", "pending", "finalized", "finalized_after_write"])
def test_failed_resource_or_receipt_finalization_cannot_publish_usable_success(
    prepared_case: PreparedCase, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Fail completion when real resource exit or receipt publication fails."""
    case = prepared_case
    case.exit_failure = failure == "lease_exit"
    with submitted_pool(case, monkeypatch, failure=failure) as (pool, completions, _writes):
        handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
        completed, result = completions.get(timeout=10)
        assert completed is handle
        assert result.ok is False
        assert result.error
        assert case.build.http is not None
        assert sum(row["method"] == "POST" for row in case.build.http.requests) == 1
        for path in (case.build.root / "pipeline-receipts").glob("*.json"):
            receipt = json.loads(path.read_text())
            assert receipt["ok"] is False and receipt["succeeded"] is False
            assert receipt.get("fleet_receipt_state") != "finalized"
        assert not lock_is_held(case.source_lock)
        assert not lock_is_held(case.evidence_lock)


def test_source_changed_after_remote_execution_cannot_be_a_current_success(
    prepared_case: PreparedCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the actual historical collection after a source change."""
    case = prepared_case
    case.change_after_publication = True
    with submitted_pool(case, monkeypatch) as (pool, completions, _writes):
        handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
        completed, result = completions.get(timeout=10)
        assert completed is handle
        assert result.ok is False
        assert isinstance(result.value, dict)
        assert result.value == case.collect(deadline=time.monotonic() + 5)
        assert result.value["status"] == "historical"
        assert result.value["sourceCurrent"] is False
        assert case.build.http is not None
        assert sum(row["method"] == "POST" for row in case.build.http.requests) == 1
