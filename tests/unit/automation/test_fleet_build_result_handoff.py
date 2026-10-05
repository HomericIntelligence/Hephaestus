"""Require real evidence-root exclusion and an acknowledged result handoff.

Apply as tests/unit/automation/test_fleet_build_result_handoff.py. These are
proposed behavior tests, not execution evidence. Admission and allocation are
controlled fixtures. Recipe processes, source snapshots, result bytes, SDK HTTP,
supervisor acknowledgment, journals and directory locks are real. This fixture
does not qualify production source isolation, Slurm, Pyxis or a deployed worker.
"""

from __future__ import annotations

import copy
import fcntl
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_jobs import FleetBuildEvidence
from hephaestus.automation.fleet_build_publication import PrivateBuildPublisher
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_fleet_build_job_fixture import (
    JobBuildCase,
    job_build_case as job_build_case,
)
from tests.unit.automation.test_fleet_build_publication import (
    PublisherCase,
    published_case as published_case,
)

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]


def _open_root(root: Path) -> int:
    """Open a distinct no-follow descriptor for the actual private directory."""
    assert root.is_absolute() and root.resolve(strict=True) == root
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        actual, named = os.fstat(descriptor), root.stat(follow_symlinks=False)
        assert stat.S_ISDIR(actual.st_mode)
        assert actual.st_uid == os.getuid() and stat.S_IMODE(actual.st_mode) == 0o700
        assert (actual.st_dev, actual.st_ino) == (named.st_dev, named.st_ino)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _root_is_held(root: Path) -> bool:
    """Try actual exclusive acquisition through a new independent descriptor."""
    descriptor = _open_root(root)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _published_bytes(root: Path) -> dict[str, bytes]:
    """Retain actual published file bytes for an unchanged-replay comparison."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _acknowledged_state(case: PublisherCase) -> Json:
    """Prove recipe exit and read acknowledgment from the actual journal file."""
    state = case.state()
    assert state["published"] is True
    assert state["cancel"] is None and state["reason"] == ""
    assert state["cleanup"] == "confirmed_empty"
    assert state["terminal"]["outcome"] == "completed"
    assert state["terminal"]["exitCode"] == 0
    assert all(
        isinstance(state["terminal"][name], dict) for name in ("receipt", "logs", "artifacts")
    )
    assert case.service.status()["cleanup"] == "confirmed_empty"
    assert case.client.facts == [state["terminal"]]
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    assert case.scheduler.children
    assert all(child.poll() == 0 for child in case.scheduler.children.values())
    assert case.scheduler.observations[-1]["schedulerTerminal"] is True
    assert case.scheduler.observations[-1]["kernelEmpty"] is True
    return copy.deepcopy(state)


def _expected_from_state(state: Json) -> tuple[Json, Json]:
    """Derive authority from retained command, lease and acknowledged terminal fact."""
    command, lease, fact = state["command"], state["lease"], state["terminal"]
    payload = command["payload"]
    reference = {"id": fact["receipt"]["reference"], "digest": fact["receipt"]["digest"]}
    expected = {
        "buildId": command["targetId"],
        "attempt": payload["attempt"],
        "commandId": command["commandId"],
        "leaseId": lease["leaseId"],
        "parent": payload["parent"],
        "policy": payload["policy"],
        "policyDigest": payload["policyDigest"],
        "snapshot": payload["snapshot"],
    }
    return copy.deepcopy(reference), copy.deepcopy(expected)


class PublisherReplay:
    """Retain one real replay until its caller releases the root lease and joins it."""

    def __init__(self, publisher: PrivateBuildPublisher, state: Json) -> None:
        """Prepare a bounded actual replay without creating another execution owner."""
        self.publisher = publisher
        self.state = copy.deepcopy(state)
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.root_contended = threading.Event()
        self.result: Json | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self.run, name="fixture-publisher-replay")

    def observe_root_contention(self, descriptor: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """Record only this replay thread's actual contention on the evidence inode."""
        metadata = os.fstat(descriptor)
        root_identity = (metadata.st_dev, metadata.st_ino)
        actual_flock = fcntl.flock

        def flock(target: int, operation: int) -> None:
            """Delegate the real lock operation before recording its kernel refusal."""
            try:
                actual_flock(target, operation)
            except BlockingIOError:
                if threading.current_thread() is self.thread:
                    target_metadata = os.fstat(target)
                    if (target_metadata.st_dev, target_metadata.st_ino) == root_identity:
                        self.root_contended.set()
                raise

        monkeypatch.setattr(fcntl, "flock", flock)

    def run(self) -> None:
        """Call the actual publisher with its retained inputs and one deadline."""
        self.entered.set()
        try:
            self.result = self.publisher.publish(
                self.state["command"],
                self.state["lease"],
                self.state["terminal"],
                deadline=time.monotonic() + 0.5,
            )
        except BaseException as error:
            self.error = error
        finally:
            self.finished.set()


def test_publisher_replay_requires_exclusion_on_the_actual_evidence_root(
    published_case: PublisherCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held evidence directory lease must refuse replay without implying corruption."""
    case = published_case
    assert case.service.handle(case.command)["status"] == "completed"
    state = _acknowledged_state(case)
    collected = case.collect(state["terminal"])
    assert collected["status"] == "verified_current" and collected["sourceCurrent"] is True
    references = {name: state["terminal"][name] for name in ("receipt", "logs", "artifacts")}
    replay = PrivateBuildPublisher(
        evidence_root=case.evidence_root, snapshot=case.snapshot, owner=case.owner
    )
    assert (
        replay.publish(
            state["command"], state["lease"], state["terminal"], deadline=time.monotonic() + 5
        )
        == references
    )
    before = _published_bytes(case.evidence_root)
    descriptor = _open_root(case.evidence_root)
    attempt = PublisherReplay(replay, state)
    completed_while_locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _root_is_held(case.evidence_root)
        attempt.observe_root_contention(descriptor, monkeypatch)
        attempt.thread.start()
        assert attempt.entered.wait(2), "the actual publisher replay did not enter"
        completed_while_locked = attempt.finished.wait(3)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        if attempt.thread.ident is not None:
            attempt.thread.join(5)
            assert not attempt.thread.is_alive(), "the actual publisher replay did not exit"

    # The same real replay must work after release even when the exclusion assertion fails.
    assert not _root_is_held(case.evidence_root)
    assert (
        replay.publish(
            state["command"], state["lease"], state["terminal"], deadline=time.monotonic() + 5
        )
        == references
    )
    assert _published_bytes(case.evidence_root) == before
    assert _acknowledged_state(case) == state
    assert completed_while_locked, "publisher replay exceeded its deadline while the root was held"
    assert isinstance(attempt.error, RuntimeError), (
        "actual publisher replay succeeded while another descriptor held the evidence-root lease"
    )
    assert attempt.root_contended.is_set(), (
        "publisher replay failed without actual flock contention on the evidence-root inode"
    )
    assert attempt.result is None


def test_acknowledged_supervisor_hands_off_actual_evidence_under_its_root_lease(
    job_build_case: JobBuildCase,
) -> None:
    """Collect actual bytes using authority derived from acknowledged supervisor history."""
    case = job_build_case
    http = case.http
    assert http is not None
    deadline = time.monotonic() + 10
    admission = case.call(case.owner.submit(deadline=deadline))
    assert admission["build"]["request"] == case.submission
    fact = case.publish()
    state = _acknowledged_state(case.publisher)
    observed = case.call(case.owner.status(deadline=deadline))
    assert observed["build"]["terminal"] == fact == state["terminal"]
    assert observed["collectionVerified"] is False
    assert [request["method"] for request in http.requests] == ["POST", "GET"]
    reference, expected = _expected_from_state(state)
    baseline = case.collect(deadline=time.monotonic() + 5)
    assert baseline["status"] == "verified_current" and baseline["sourceCurrent"] is True
    assert baseline["reference"] == reference and baseline["identity"] == expected
    before = _published_bytes(case.publisher.evidence_root)

    handoff = getattr(case.publisher.service, "result_handoff", None)
    assert callable(handoff), "the acknowledged supervisor has no trusted result-handoff callable"

    shutdown = threading.Event()
    deadline = time.monotonic() + 5
    with (
        file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True),
        handoff(copy.deepcopy(observed), deadline=deadline, shutdown=shutdown) as evidence,
    ):
        assert isinstance(evidence, FleetBuildEvidence)
        assert evidence.evidence_root == case.publisher.evidence_root
        assert evidence.reference == reference and evidence.expected == expected
        assert _root_is_held(evidence.evidence_root)
        collected = collect_build_result(
            evidence.evidence_root,
            reference=evidence.reference,
            expected=evidence.expected,
            source=case.publisher.source,
            snapshot_policy=case.publisher.policy,
            deadline=deadline,
        )
        assert collected == baseline
        assert _root_is_held(evidence.evidence_root)

    assert not shutdown.is_set()
    assert not _root_is_held(case.publisher.evidence_root)
    assert _published_bytes(case.publisher.evidence_root) == before
    assert _acknowledged_state(case.publisher) == state
    assert [request["method"] for request in http.requests] == ["POST", "GET"]
