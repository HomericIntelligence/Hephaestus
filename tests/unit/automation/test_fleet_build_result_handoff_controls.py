"""Keep acknowledged result authority stable through public handoff scopes.

Apply as tests/unit/automation/test_fleet_build_result_handoff_controls.py.
These proposed tests use actual recipe exit, publication, SDK observations,
journals, collectors and directory locks. Admission and allocation are fixtures.
The source lock is a fixture capability. It does not qualify production source
isolation, Slurm, Pyxis or a deployed worker. No run result is asserted here.
"""

from __future__ import annotations

import copy
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_contract import start_command, validate_command
from hephaestus.automation.fleet_build_jobs import FleetBuildEvidence, ResultHandoff
from hephaestus.automation.fleet_build_publication import PrivateBuildPublisher
from hephaestus.automation.fleet_build_supervisor import BuildSupervisor
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_fleet_build_job_fixture import (
    JobBuildCase,
    job_build_case as job_build_case,
)
from tests.unit.automation.test_fleet_build_owner_exit import _receive, _send, _writer_is_held
from tests.unit.automation.test_fleet_build_publication import PublicationExecutor
from tests.unit.automation.test_fleet_build_result_handoff import (
    _acknowledged_state,
    _expected_from_state,
    _published_bytes,
    _root_is_held,
)

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]
_MODULE = "tests.unit.automation.test_fleet_build_result_handoff_controls"


class AcknowledgedBuild:
    """Retain independent observations from one actual completed fixture build."""

    def __init__(self, case: JobBuildCase) -> None:
        """Require actual publication and SDK observation before testing the handoff."""
        self.case = case
        http = case.http
        assert http is not None
        deadline = time.monotonic() + 10
        admission = case.call(case.owner.submit(deadline=deadline))
        assert admission["build"]["request"] == case.submission
        fact = case.publish()
        self.state = _acknowledged_state(case.publisher)
        self.observed = case.call(case.owner.status(deadline=deadline))
        assert self.observed["build"]["terminal"] == fact == self.state["terminal"]
        assert self.observed["collectionVerified"] is False
        self.reference, self.expected = _expected_from_state(self.state)
        self.baseline = case.collect(deadline=time.monotonic() + 5)
        assert self.baseline["status"] == "verified_current"
        assert self.baseline["sourceCurrent"] is True
        assert self.baseline["reference"] == self.reference
        assert self.baseline["identity"] == self.expected
        self.history = _published_bytes(case.publisher.root / "state")
        self.bundle = _published_bytes(case.publisher.evidence_root)
        self.journal = case.publisher.service.journal.snapshot()
        self.status = case.publisher.service.status()
        self.claims = copy.deepcopy(case.publisher.client.claims)
        self.requests = copy.deepcopy(http.requests)
        assert [request["method"] for request in self.requests] == ["POST", "GET"]

    def handoff(
        self, *, shutdown: threading.Event | None = None
    ) -> AbstractContextManager[FleetBuildEvidence]:
        """Call only the public supervisor capability with the actual observed record."""
        handoff = getattr(self.case.publisher.service, "result_handoff", None)
        assert callable(handoff), "the acknowledged supervisor has no result-handoff callable"
        return cast(ResultHandoff, handoff)(
            copy.deepcopy(self.observed),
            deadline=time.monotonic() + 8,
            shutdown=shutdown if shutdown is not None else threading.Event(),
        )

    def collect(self, evidence: FleetBuildEvidence) -> Json:
        """Collect real source and result bytes while the actual evidence root is held."""
        assert isinstance(evidence, FleetBuildEvidence)
        assert evidence.evidence_root == self.case.publisher.evidence_root
        assert evidence.reference == self.reference and evidence.expected == self.expected
        assert _root_is_held(evidence.evidence_root)
        return collect_build_result(
            evidence.evidence_root,
            reference=evidence.reference,
            expected=evidence.expected,
            source=self.case.publisher.source,
            snapshot_policy=self.case.publisher.policy,
            deadline=time.monotonic() + 5,
        )

    def assert_unchanged(self) -> None:
        """Require unchanged actual history, authority, calls and open journal health."""
        case = self.case
        http = case.http
        assert http is not None
        case.publisher.service.journal.require_writable()
        assert case.publisher.service.journal.snapshot() == self.journal
        assert _writer_is_held(case.publisher.service.journal.directory)
        assert case.publisher.service.status() == self.status
        assert _acknowledged_state(case.publisher) == self.state
        assert _published_bytes(case.publisher.root / "state") == self.history
        assert _published_bytes(case.publisher.evidence_root) == self.bundle
        assert case.publisher.client.claims == self.claims
        assert http.requests == self.requests
        assert not case.publisher.service._shutdown.is_set()
        assert case.publisher.scheduler.cancels == []

    def collect_again(self) -> None:
        """Prove both root and state pin release by a fresh real handoff and collection."""
        assert not _root_is_held(self.case.publisher.evidence_root)
        with self.handoff() as evidence:
            assert self.collect(evidence) == self.baseline
        assert not _root_is_held(self.case.publisher.evidence_root)
        self.assert_unchanged()


def test_returned_identity_copies_cannot_change_later_handoffs_or_history(
    job_build_case: JobBuildCase,
) -> None:
    """Change only returned nested dictionaries, then collect from fresh authority."""
    observed = AcknowledgedBuild(job_build_case)
    with file_lock(
        job_build_case.root / "source-access.lock", blocking=False, require_exclusive=True
    ):
        with observed.handoff() as evidence:
            assert observed.collect(evidence) == observed.baseline
            evidence.expected["parent"]["claim"]["workspace"] = "/returned-copy-only"
            evidence.expected["policy"]["recipe"]["resources"]["wallSeconds"] = -1
            evidence.expected["snapshot"]["manifestDigest"] = "returned-copy-only"
            evidence.reference["id"] = "returned-copy-only"
            evidence.reference["digest"] = "returned-copy-only"
            assert _root_is_held(job_build_case.publisher.evidence_root)
        observed.assert_unchanged()
        observed.collect_again()


class HandoffBodyError(Exception):
    """Identify the caller's error independently of a handoff cleanup failure."""


def test_body_exception_releases_the_actual_root_and_allows_a_fresh_handoff(
    job_build_case: JobBuildCase,
) -> None:
    """Preserve the caller's exception while releasing a healthy root and state pin."""
    observed = AcknowledgedBuild(job_build_case)
    body_error = HandoffBodyError("the fixture consumer stopped after actual collection")
    with file_lock(
        job_build_case.root / "source-access.lock", blocking=False, require_exclusive=True
    ):
        with pytest.raises(HandoffBodyError) as raised, observed.handoff() as evidence:
            assert observed.collect(evidence) == observed.baseline
            raise body_error
        assert raised.value is body_error
        observed.collect_again()


def test_shutdown_at_scope_exit_refuses_success_and_releases_healthy_ownership(
    job_build_case: JobBuildCase,
) -> None:
    """Interrupt after actual collection and require the exit check to reject success."""
    observed = AcknowledgedBuild(job_build_case)
    shutdown = threading.Event()
    with file_lock(
        job_build_case.root / "source-access.lock", blocking=False, require_exclusive=True
    ):
        with pytest.raises(InterruptedError), observed.handoff(shutdown=shutdown) as evidence:
            assert observed.collect(evidence) == observed.baseline
            shutdown.set()
            assert _root_is_held(job_build_case.publisher.evidence_root)
        assert shutdown.is_set()
        observed.collect_again()


class PublishOnlyPublisher:
    """Expose only publication while delegating all result work to the actual publisher."""

    def __init__(self, publisher: PrivateBuildPublisher) -> None:
        """Retain the actual publisher without supplying a read capability."""
        self.publisher = publisher

    def publish(self, command: Json, lease: Json, terminal: Json, *, deadline: float) -> Json:
        """Publish actual evidence with the original inputs and deadline."""
        return self.publisher.publish(command, lease, terminal, deadline=deadline)


@contextmanager
def _publish_only_supervisor(case: JobBuildCase) -> Iterator[None]:
    """Construct the real supervisor with a structural publication-only capability."""
    publisher = case.publisher
    original = publisher.service
    original.close()
    replacement = BuildSupervisor(
        state_dir=publisher.root / "state",
        workspace_root=publisher.root / "workspaces",
        policy=publisher.command["payload"]["policy"],
        client=publisher.client,
        snapshot=publisher.snapshot,
        executor=PublicationExecutor(publisher.owner, publisher.root / "state"),
        publisher=PublishOnlyPublisher(publisher.publisher),
        claim_id_factory=lambda: "publisher-claim",
    )
    publisher.service = replacement
    try:
        yield
    finally:
        try:
            replacement.close()
        finally:
            publisher.service = original


def test_publication_only_capability_still_publishes_but_cannot_handoff(
    job_build_case: JobBuildCase,
) -> None:
    """Keep constructor and publication compatibility without inventing read authority."""
    with _publish_only_supervisor(job_build_case):
        observed = AcknowledgedBuild(job_build_case)
        with pytest.raises(RuntimeError), observed.handoff():
            pytest.fail("a publication-only capability supplied a result handoff")
        assert not _root_is_held(job_build_case.publisher.evidence_root)
        observed.assert_unchanged()
        assert job_build_case.collect(deadline=time.monotonic() + 5) == observed.baseline


def _mutations(service: BuildSupervisor, command: Json) -> dict[str, Callable[[], object]]:
    """Use a valid same-build cancellation as well as the real close and reconcile calls."""
    cancel = copy.deepcopy(command)
    cancel.update(operation="cancel", commandId="handoff-stop", idempotencyKey="handoff-stop-key")
    cancel["payload"]["stopStartCommandId"] = command["commandId"]
    assert start_command(validate_command(cancel, service.policy)) == command
    return {
        "close": service.close,
        "cancel": lambda: service.handle(cancel),
        "reconcile": service.reconcile,
    }


def _reentrant_calls(control: socket.socket, observed: AcknowledgedBuild) -> None:
    """Acknowledge each real reentrant refusal before the next bounded parent trigger."""
    case = observed.case
    actions = _mutations(case.publisher.service, case.publisher.command)
    with (
        file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True),
        observed.handoff() as evidence,
    ):
        assert observed.collect(evidence) == observed.baseline
        _send(control, {"kind": "ready", "root_held": _root_is_held(evidence.evidence_root)})
        for name, action in actions.items():
            assert _receive(control, timeout=3) == {"operation": name}
            error_name = ""
            try:
                action()
            except RuntimeError as error:
                error_name = type(error).__name__
            observed.assert_unchanged()
            _send(
                control,
                {
                    "kind": name,
                    "error": error_name,
                    "root_held": _root_is_held(evidence.evidence_root),
                },
            )
            assert error_name == "RuntimeError", f"{name} did not refuse the active read pin"
    with file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True):
        observed.collect_again()


def _reentrant_child(control: socket.socket, root: Path) -> None:
    """Own the real recipe, SDK, journals and HTTP server until every call returns."""
    case = JobBuildCase(root)
    try:
        _reentrant_calls(control, AcknowledgedBuild(case))
    finally:
        case.close()
    assert not case.loop_thread.is_alive() and case.loop.is_closed()
    assert case.client._client.is_closed and case.journal.snapshot()["closed"]
    assert all(child.poll() == 0 for child in case.publisher.scheduler.children.values())
    _send(control, {"kind": "closed"})


def _run_reentrant_child(root: Path) -> tuple[list[Json], Json]:
    """Bound each reentrant call and reap only the exact fixture-owning child."""
    parent, inherited = socket.socketpair()
    messages: list[Json] = []
    communication_error = ""
    stdout = stderr = ""
    forced = False
    try:
        child = subprocess.Popen(
            [sys.executable, "-B", "-u", "-m", _MODULE, str(inherited.fileno()), str(root)],
            pass_fds=(inherited.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        inherited.close()
        try:
            messages.append(_receive(parent, timeout=20))
            for operation in ("close", "cancel", "reconcile"):
                _send(parent, {"operation": operation})
                messages.append(_receive(parent, timeout=2))
            messages.append(_receive(parent, timeout=8))
            stdout, stderr = child.communicate(timeout=3)
        except (EOFError, OSError, subprocess.TimeoutExpired) as error:
            communication_error = repr(error)
        finally:
            if child.poll() is None:
                forced = True
                child.terminate()
                try:
                    stdout, stderr = child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    stdout, stderr = child.communicate(timeout=3)
            else:
                stdout, stderr = child.communicate(timeout=3)
    finally:
        parent.close()
        inherited.close()
    return messages, {
        "error": communication_error,
        "forced": forced,
        "exit": child.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def test_reentrant_close_cancel_and_reconcile_refuse_before_owner_mutation(tmp_path: Path) -> None:
    """Require prompt same-thread refusals under a real root lease and unchanged history."""
    messages, diagnostic = _run_reentrant_child(tmp_path)
    assert diagnostic["error"] == "" and diagnostic["forced"] is False, diagnostic
    assert diagnostic["exit"] == 0, diagnostic
    assert messages == [
        {"kind": "ready", "root_held": True},
        {"kind": "close", "error": "RuntimeError", "root_held": True},
        {"kind": "cancel", "error": "RuntimeError", "root_held": True},
        {"kind": "reconcile", "error": "RuntimeError", "root_held": True},
        {"kind": "closed"},
    ], diagnostic


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("this module requires its private control descriptor and fixture root")
    with socket.socket(fileno=int(sys.argv[1])) as child_control:
        _reentrant_child(child_control, Path(sys.argv[2]).resolve(strict=True))
