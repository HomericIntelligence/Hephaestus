"""Refuse uncertain or mismatched handoffs and preserve actual restart evidence.

Apply as tests/unit/automation/test_fleet_build_result_handoff_recovery.py after
the result-handoff controls module. These are proposed tests, not run evidence.
Admission and allocation are fixtures. Recipe processes, SDK HTTP, journals,
publication, source snapshots, collection and directory locks are real. The
fixture source lease does not qualify production isolation, Slurm or Pyxis.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
import time
from contextlib import AbstractContextManager
from typing import Any, cast

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_jobs import FleetBuildEvidence, ResultHandoff
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_fleet_build_job_fixture import (
    JobBuildCase,
    job_build_case as job_build_case,
)
from tests.unit.automation.test_fleet_build_owner_exit import _writer_is_held
from tests.unit.automation.test_fleet_build_result_handoff import (
    _expected_from_state,
    _open_root,
    _published_bytes,
    _root_is_held,
)
from tests.unit.automation.test_fleet_build_result_handoff_controls import AcknowledgedBuild

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]


def _handoff(
    case: JobBuildCase,
    record: Json,
    *,
    deadline: float,
    shutdown: threading.Event,
) -> AbstractContextManager[FleetBuildEvidence]:
    """Use the current actual supervisor, including after a real owner restart."""
    operation = getattr(case.publisher.service, "result_handoff", None)
    assert callable(operation), "the supervisor has no result-handoff callable"
    return cast(ResultHandoff, operation)(
        copy.deepcopy(record), deadline=deadline, shutdown=shutdown
    )


def _effects(case: JobBuildCase) -> Json:
    """Copy actual history and external effects without requiring healthy publication."""
    publisher, http = case.publisher, case.http
    assert http is not None
    scheduler = publisher.scheduler
    assert len(scheduler.starts) == len(scheduler.releases) == 1
    assert scheduler.children and all(child.poll() == 0 for child in scheduler.children.values())
    assert scheduler.observations[-1]["schedulerTerminal"] is True
    assert scheduler.observations[-1]["kernelEmpty"] is True
    return {
        "history": _published_bytes(publisher.root / "state"),
        "step": _published_bytes(scheduler.state),
        "bundle": _published_bytes(publisher.evidence_root),
        "journal": publisher.service.journal.snapshot(),
        "status": publisher.service.status(),
        "claims": copy.deepcopy(publisher.client.claims),
        "facts": copy.deepcopy(publisher.client.facts),
        "starts": copy.deepcopy(scheduler.starts),
        "releases": copy.deepcopy(scheduler.releases),
        "cancels": copy.deepcopy(scheduler.cancels),
        "observations": copy.deepcopy(scheduler.observations),
        "http": copy.deepcopy(http.requests),
    }


def _submit(case: JobBuildCase) -> None:
    """Admit this exact fresh source through the real SDK and HTTP fixture."""
    admission = case.call(case.owner.submit(deadline=time.monotonic() + 5))
    assert admission["build"]["request"] == case.submission


def _observe_accepted(case: JobBuildCase) -> Json:
    """Project only the controller's actual accepted fact through the fixture HTTP server."""
    http = case.http
    assert http is not None
    fact = copy.deepcopy(case.publisher.client.facts[-1])
    assert fact["outcome"] == "completed" and fact["exitCode"] == 0
    http.record["status"] = fact["outcome"]
    http.record["build"]["terminal"] = fact
    observed = case.call(case.owner.status(deadline=time.monotonic() + 5))
    assert observed["build"]["terminal"] == fact
    assert observed["collectionVerified"] is False
    return observed


def _collect_retained(case: JobBuildCase, record: Json) -> None:
    """Collect using authority from actual supervisor history rather than the receipt."""
    state = case.publisher.state()
    assert state["published"] is True and state["reason"] == "" and state["cancel"] is None
    assert state["cleanup"] == "confirmed_empty"
    reference, expected = _expected_from_state(state)
    baseline = case.collect(deadline=time.monotonic() + 5)
    assert baseline["status"] == "verified_current" and baseline["sourceCurrent"] is True
    deadline = time.monotonic() + 5
    with (
        file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True),
        _handoff(case, record, deadline=deadline, shutdown=threading.Event()) as evidence,
    ):
        assert evidence.reference == reference and evidence.expected == expected
        assert _root_is_held(evidence.evidence_root)
        assert (
            collect_build_result(
                evidence.evidence_root,
                reference=evidence.reference,
                expected=evidence.expected,
                source=case.publisher.source,
                snapshot_policy=case.publisher.policy,
                deadline=deadline,
            )
            == baseline
        )
    assert not _root_is_held(case.publisher.evidence_root)


def test_lost_controller_reply_refuses_until_exact_replay_is_acknowledged(
    job_build_case: JobBuildCase,
) -> None:
    """An accepted fact without a retained acknowledgment cannot supply a handoff."""
    case = job_build_case
    _submit(case)
    case.publisher.client.drop_reply = True
    fact = case.publish()
    record = _observe_accepted(case)
    state = copy.deepcopy(case.publisher.state())
    assert state["published"] is False and state["reason"]
    assert case.publisher.client.facts == [fact] and state["terminal"] == fact
    before = _effects(case)
    with (
        pytest.raises(RuntimeError),
        _handoff(case, record, deadline=time.monotonic() + 5, shutdown=threading.Event()),
    ):
        pytest.fail("the lost controller acknowledgment supplied a result handoff")
    assert _effects(case) == before
    assert not _root_is_held(case.publisher.evidence_root)
    assert case.publisher.service.handle(case.publisher.command)["status"] == "completed"
    assert case.publisher.client.facts == [fact, fact]
    assert case.publisher.state()["terminal"] == state["terminal"]
    replayed = _effects(case)
    for name in ("claims", "starts", "releases", "cancels", "observations", "bundle", "step"):
        assert replayed[name] == before[name], name
    _collect_retained(case, record)
    assert _effects(case) == replayed


def test_uncertain_acknowledgment_append_poison_refuses_handoff(
    job_build_case: JobBuildCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real synchronized acknowledgment can remain ineligible after append uncertainty."""
    case = job_build_case
    _submit(case)
    journal = case.publisher.service.journal
    actual_apply = journal._apply
    applied: list[Json] = []

    def apply(record: Json) -> None:
        """Apply the real synchronized row before failing inside the journal's append."""
        actual_apply(record)
        if record["kind"] == "build" and record["value"]["published"] is True and not applied:
            assert case.publisher.client.facts == [record["value"]["terminal"]]
            actual = json.loads(
                (journal.directory / "receipts.jsonl").read_bytes().splitlines()[-1]
            )
            assert actual == record
            applied.append(copy.deepcopy(record))
            raise OSError("fixture lost acknowledgment append confirmation after synchronization")

    with monkeypatch.context() as fault:
        fault.setattr(journal, "_apply", apply)
        with pytest.raises((OSError, RuntimeError)):
            case.publisher.service.handle(case.publisher.command)
    assert len(applied) == 1
    saved = journal.snapshot()
    assert saved["write_uncertain"] is True and saved["closed"] is False
    assert saved["records"][-1] == applied[0]
    assert case.publisher.state() == applied[0]["value"]
    with pytest.raises(RuntimeError):
        journal.require_writable()
    assert _writer_is_held(journal.directory)
    record = _observe_accepted(case)
    collected = case.collect(deadline=time.monotonic() + 5)
    assert collected["status"] == "verified_current" and collected["sourceCurrent"] is True
    before = _effects(case)
    with (
        pytest.raises(RuntimeError),
        _handoff(case, record, deadline=time.monotonic() + 5, shutdown=threading.Event()),
    ):
        pytest.fail("an uncertain acknowledgment append supplied a result handoff")
    assert _effects(case) == before
    assert not _root_is_held(case.publisher.evidence_root)
    assert _writer_is_held(journal.directory)


def test_clean_supervisor_and_step_owner_restart_recollects_without_reexecution(
    job_build_case: JobBuildCase,
) -> None:
    """Reopen actual journals and use the original accepted attempt and bundle."""
    case = job_build_case
    observed = AcknowledgedBuild(case)
    before = _effects(case)
    old_service, old_owner = case.publisher.service, case.publisher.owner
    case.publisher.close_owners()
    assert old_service.journal.snapshot()["closed"] is True
    assert not _writer_is_held(old_service.journal.directory)
    case.publisher.open()
    assert case.publisher.service is not old_service and case.publisher.owner is not old_owner
    assert _effects(case) == before
    with file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True):
        observed.collect_again()
    assert _effects(case) == before


def test_each_mismatched_observed_identity_refuses_without_changing_retained_authority(
    job_build_case: JobBuildCase,
) -> None:
    """Change only the supplied observation and preserve the valid supervisor history."""
    case = job_build_case
    observed = AcknowledgedBuild(case)
    record = observed.observed
    changes = (
        (("id",), record["id"] + "-other"),
        (("build", "attempt"), record["build"]["attempt"] + 1),
        (("generation",), record["generation"] + 1),
        (("parent", "claim", "workspace"), str(case.publisher.source) + "-other"),
        (
            ("build", "policy", "recipe", "resources", "wallSeconds"),
            record["build"]["policy"]["recipe"]["resources"]["wallSeconds"] + 1,
        ),
        (("build", "request", "snapshot", "manifestDigest"), "0" * 64),
        (("build", "terminal", "eventId"), record["build"]["terminal"]["eventId"] + "-other"),
    )
    before = _effects(case)
    with file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True):
        for path, value in changes:
            changed = copy.deepcopy(record)
            target = changed
            for name in path[:-1]:
                target = target[name]
            assert target[path[-1]] != value, path
            target[path[-1]] = value
            with (
                pytest.raises(ValueError),
                _handoff(case, changed, deadline=time.monotonic() + 5, shutdown=threading.Event()),
            ):
                pytest.fail(f"a changed observation supplied a handoff: {path}")
            assert _effects(case) == before, path
            observed.collect_again()


def test_missing_referenced_bundle_file_refuses_without_publication_or_repair(
    job_build_case: JobBuildCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove one actual referenced file after a positive collection, then require refusal."""
    case = job_build_case
    observed = AcknowledgedBuild(case)
    publisher = case.publisher.publisher
    actual_publish, actual_retain = publisher.publish, publisher._retain
    calls: list[str] = []

    def publish(command: Json, lease: Json, terminal: Json, *, deadline: float) -> Json:
        """Record and delegate any actual publication entered by the reader."""
        calls.append("publish")
        return actual_publish(command, lease, terminal, deadline=deadline)

    def retain(reference: str, files: dict[str, bytes], deadline: float) -> None:
        """Record and delegate any actual retention entered by the reader."""
        calls.append("retain")
        actual_retain(reference, files, deadline)

    monkeypatch.setattr(publisher, "publish", publish)
    monkeypatch.setattr(publisher, "_retain", retain)
    bundle = case.publisher.evidence_root / observed.reference["id"]
    receipt = json.loads((bundle / "receipt.json").read_bytes())
    missing = bundle / receipt["files"]["stdout"]["path"]
    assert missing.read_bytes() == b"fixture build\n"
    missing.unlink()
    before = _effects(case)
    with pytest.raises(RuntimeError), observed.handoff():
        pytest.fail("a missing referenced file supplied a result handoff")
    assert not missing.exists() and calls == []
    assert _effects(case) == before
    assert not _root_is_held(case.publisher.evidence_root)


class ContendedHandoff:
    """Observe one real handoff thread blocked by the actual evidence-root descriptor."""

    def __init__(self, observed: AcknowledgedBuild, *, timeout: float) -> None:
        """Retain finite ownership until the test releases the descriptor and joins."""
        self.observed, self.timeout = observed, timeout
        self.shutdown = threading.Event()
        self.contended = threading.Event()
        self.finished = threading.Event()
        self.yielded = False
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self.run, name="fixture-contended-handoff")

    def observe_flock(self, descriptor: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """Record actual kernel refusal only for this thread and evidence-root inode."""
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        actual_flock = fcntl.flock

        def flock(target: int, operation: int) -> None:
            """Delegate the lock and retain only the selected actual contention."""
            try:
                actual_flock(target, operation)
            except BlockingIOError:
                if threading.current_thread() is self.thread:
                    actual = os.fstat(target)
                    if (actual.st_dev, actual.st_ino) == identity:
                        self.contended.set()
                raise

        monkeypatch.setattr(fcntl, "flock", flock)

    def run(self) -> None:
        """Invoke the real public handoff under one absolute deadline and shutdown event."""
        try:
            with _handoff(
                self.observed.case,
                self.observed.observed,
                deadline=time.monotonic() + self.timeout,
                shutdown=self.shutdown,
            ):
                self.yielded = True
        except BaseException as error:
            self.error = error
        finally:
            self.finished.set()


def _contend(
    observed: AcknowledgedBuild, mode: str, monkeypatch: pytest.MonkeyPatch
) -> ContendedHandoff:
    """Release the actual root and join the exact attempt before asserting its result."""
    attempt = ContendedHandoff(observed, timeout=0.5 if mode == "deadline" else 5)
    descriptor = _open_root(observed.case.publisher.evidence_root)
    contended = finished = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempt.observe_flock(descriptor, monkeypatch)
        attempt.thread.start()
        contended = attempt.contended.wait(2)
        if mode == "shutdown":
            attempt.shutdown.set()
        finished = attempt.finished.wait(2)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        attempt.shutdown.set()
        if attempt.thread.ident is not None:
            attempt.thread.join(6)
            assert not attempt.thread.is_alive(), "the actual contended handoff did not exit"
    assert contended, "the actual handoff did not reach kernel contention on the root inode"
    assert finished, "the actual handoff did not refuse while the root descriptor stayed locked"
    return attempt


def test_root_contention_observes_deadline_and_shutdown_before_lease_release(
    job_build_case: JobBuildCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require bounded actual contention refusal and a fresh handoff after each release."""
    case = job_build_case
    observed = AcknowledgedBuild(case)
    before = _effects(case)
    with file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True):
        for mode, error_type in (("deadline", TimeoutError), ("shutdown", InterruptedError)):
            with monkeypatch.context() as patch:
                attempt = _contend(observed, mode, patch)
            assert isinstance(attempt.error, error_type), (mode, attempt.error)
            assert not attempt.yielded, mode
            assert _effects(case) == before
            observed.collect_again()
