"""Require automatic exact-lease cleanup after an actual recipe deadline failure.

Admission, allocation and preparation checks are fixtures. The recipe process, FIFO,
deadline, snapshot, SDK, journals and scheduler cleanup operations are real.
This test does not claim a confirmed timed-out terminal, production isolation,
Slurm, Pyxis or remote execution. Explicit reconciliation is a cleanup control.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import select
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_contract import digest
from hephaestus.automation.fleet_build_jobs import FleetBuildJobContext, FleetBuildJobRunner
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
from hephaestus.automation.fleet_snapshot import export_snapshot
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_fleet_build_executor import retained
from tests.unit.automation.test_fleet_build_job_fixture import JobBuildCase
from tests.unit.automation.test_fleet_build_result_handoff import _root_is_held
from tests.unit.automation.test_fleet_build_result_handoff_outcomes import FailedRecipeExecutor
from tests.unit.automation.test_fleet_build_result_handoff_recovery import _handoff

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]
_MODULE = "tests.unit.automation.test_fleet_build_timeout_cleanup"
_OUTPUT_LOCK = threading.Lock()


def _report(kind: str, **observation: Any) -> None:
    """Write one actual fixture observation without mixing watcher and caller output."""
    with _OUTPUT_LOCK:
        print(json.dumps({"timeoutFixture": kind, **observation}), flush=True)


def _timeout_recipe(case: JobBuildCase, fifo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Export real waiting recipe bytes and reopen the unused consumer before admission."""
    publisher, http = case.publisher, case.http
    assert http is not None and http.requests == []
    old_client, old_journal = case.client, case.journal
    case.call(case._close_consumer())
    assert old_client._client.is_closed and old_journal.snapshot()["closed"]
    publisher.close_owners()
    program = (
        "import os, signal; "
        f"descriptor = os.open({str(fifo)!r}, os.O_WRONLY); "
        "os.write(descriptor, b'R'); os.close(descriptor); signal.pause()"
    )
    recipe = f"test-unit:\n    @{shlex.quote(sys.executable)} -c {shlex.quote(program)}\n"
    (publisher.source / "justfile").write_bytes(recipe.encode())
    source_files = {
        name: (publisher.source / name).read_bytes() for name in ("justfile", "uv.lock")
    }
    payload = publisher.command["payload"]
    policy = payload["policy"]["recipe"]
    policy["recipeDigest"] = hashlib.sha256(source_files["justfile"]).hexdigest()
    policy["lockDigest"] = hashlib.sha256(source_files["uv.lock"]).hexdigest()
    policy["resources"]["wallSeconds"] = 2
    payload["policyDigest"] = digest(payload["policy"])
    artifact = publisher.root / "timeout-snapshot"
    payload["snapshot"] = export_snapshot(
        publisher.source, artifact, reference="publisher-source", policy=publisher.policy
    )
    publisher.snapshot = BuildSnapshot(artifact, publisher.policy)
    publisher.open()
    monkeypatch.setattr(
        publisher.service,
        "executor",
        FailedRecipeExecutor(publisher.owner, publisher.root / "state", source_files),
    )
    case._bind_http()
    case.call(case._open_consumer())
    assert http.requests == []


class TimeoutWatch:
    """Observe actual FIFO readiness and fence only this child and its owned recipe."""

    def __init__(self, case: JobBuildCase, descriptor: int) -> None:
        """Keep the real scheduler available if the control thread stops responding."""
        self.case, self.descriptor = case, descriptor
        self.ready = threading.Event()
        self.done = threading.Event()
        self.fired = threading.Event()
        self.thread = threading.Thread(target=self.run, name="fixture-timeout-watchdog")

    def run(self) -> None:
        """Use a positive FIFO event, then bounded exact-Popen cleanup on watchdog expiry."""
        if select.select([self.descriptor], [], [], 5)[0]:
            value = os.read(self.descriptor, 2)
            if value == b"R":
                self.ready.set()
                _report("recipe-ready")
            else:
                _report("readiness-error", value=repr(value))
        else:
            _report("readiness-timeout")
        if self.done.wait(12):
            return
        self.fired.set()
        scheduler = self.case.publisher.scheduler
        try:
            # This fixture method holds exact Popen objects and reaps only their owned groups.
            scheduler.close()
            _report(
                "watchdog-cleanup",
                exits={nonce: child.poll() for nonce, child in scheduler.children.items()},
            )
        except BaseException as error:
            _report("watchdog-cleanup-error", error=repr(error))
        finally:
            # The outer test reaps this child and rejects watchdog exit as test evidence.
            os._exit(73)

    def finish(self) -> None:
        """Join the observer only after ordinary fixture cleanup has completed."""
        self.done.set()
        self.thread.join(6)
        assert not self.thread.is_alive(), "the exact timeout watchdog did not exit"
        assert not self.fired.is_set(), "the watchdog replaced ordinary fixture cleanup"


class ReleaseObservation:
    """Delegate actual scheduler start and release while retaining their exact observations."""

    def __init__(self, case: JobBuildCase) -> None:
        """Retain the supplied operation deadline without replacing a clock or result."""
        self.case = case
        self.deadline: float | None = None
        self.timeout: subprocess.TimeoutExpired | None = None
        self.expired = False
        self.returned = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap only actual scheduler calls and preserve each original return or exception."""
        scheduler = self.case.publisher.scheduler
        actual_start, actual_release = scheduler.start, scheduler.release

        def start(lease: Json, nonce: str, *, deadline: float) -> Json:
            """Report the retained exact process identity before its recipe is released."""
            identity = actual_start(lease, nonce, deadline=deadline)
            child = scheduler.children[nonce]
            assert child.poll() is None
            _report("recipe-owned", pid=child.pid, nonce=nonce, identity=identity)
            return identity

        def release(
            identity: Json,
            *,
            argv: tuple[str, ...],
            deadline: float,
            shutdown: threading.Event,
            max_output_bytes: int,
        ) -> Json:
            """Retain only a real communicate timeout and re-raise the same exception."""
            self.deadline = deadline
            assert 0 < deadline - time.monotonic() < 3
            try:
                result = actual_release(
                    identity,
                    argv=argv,
                    deadline=deadline,
                    shutdown=shutdown,
                    max_output_bytes=max_output_bytes,
                )
            except subprocess.TimeoutExpired as error:
                self.timeout = error
                self.expired = time.monotonic() >= deadline
                _report("actual-timeout", deadline_expired=self.expired, timeout=error.timeout)
                raise
            self.returned = True
            return result

        monkeypatch.setattr(scheduler, "start", start)
        monkeypatch.setattr(scheduler, "release", release)


def _expired_job(case: JobBuildCase, deadline: float) -> None:
    """Use the actual adapter and expired original budget without extending job eligibility."""
    command = case.publisher.command
    repository = command["payload"]["policy"]["workspace"]["repository"]

    @contextmanager
    def source_lease(*, deadline: float, shutdown: threading.Event) -> Iterator[None]:
        """Keep an actual fixture lease if the product reaches source acquisition."""
        assert deadline > time.monotonic() and not shutdown.is_set()
        with file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True):
            yield

    context = FleetBuildJobContext(
        owner=case.owner,
        repository=repository,
        source=case.publisher.source,
        submission=copy.deepcopy(case.submission),
        parent=copy.deepcopy(command["payload"]["parent"]),
        snapshot=case.publisher.snapshot,
        source_lease=source_lease,
        result_handoff=case.publisher.service.result_handoff,
    )
    runner = FleetBuildJobRunner(loop=case.loop, contexts={"job-context": context})
    job = BuildTestJob(
        repo=repository,
        cwd=case.publisher.source,
        argv=("just", "test-unit"),
        timeout_s=2,
        expected_head_sha=case.submission["snapshot"]["baseCommit"],
        descr="fixture_expired_recipe",
        fleet_context_id="job-context",
    )
    with ExitStack() as resources:
        result = runner.run(job, deadline=deadline, shutdown=threading.Event(), resources=resources)
    assert result.ok is False and result.error == "timeout"
    assert result.value is None and result.fleet_receipt is None


def _observe_before_reconcile(case: JobBuildCase) -> Json:
    """Read automatic cleanup before any explicit recovery or fixture teardown occurs."""
    publisher, scheduler = case.publisher, case.publisher.scheduler
    state = publisher.state()
    assert len(scheduler.starts) == len(scheduler.releases) == 1
    nonce = scheduler.starts[0]
    identity = scheduler.identities[nonce]
    step = retained(scheduler.state)[-1]
    observation = scheduler.observe(identity, deadline=time.monotonic() + 2)
    return {
        "cleanup": state["cleanup"],
        "step_cleanup": step["cleanup"],
        "retained_observation": copy.deepcopy(step.get("cleanupObservation")),
        "actual_observation": observation,
        "child_exit": scheduler.children[nonce].poll(),
        "cancels": copy.deepcopy(scheduler.cancels),
        "terminal": state["terminal"],
        "published": state["published"],
    }


def _reconcile_control(case: JobBuildCase) -> None:
    """Perform real exact-lease disposal and keep the unresolved exit visibly unsuccessful."""
    publisher, scheduler = case.publisher, case.publisher.scheduler
    result = publisher.service.reconcile()
    state = publisher.state()
    assert result["status"] == "reconciliation_required" and result["collectionVerified"] is False
    assert state["cleanup"] == "confirmed_empty"
    assert state["terminal"] is None and state["published"] is False and state["cancel"] is None
    nonce = scheduler.starts[0]
    observed = scheduler.observe(scheduler.identities[nonce], deadline=time.monotonic() + 2)
    assert observed["schedulerTerminal"] is True and observed["kernelEmpty"] is True
    assert scheduler.children[nonce].poll() is not None
    assert retained(scheduler.state)[-1]["cleanupObservation"] == observed
    assert publisher.client.facts == [] and len(publisher.client.claims) == 1
    assert len(scheduler.starts) == len(scheduler.releases) == 1
    assert not list(publisher.evidence_root.iterdir())
    with pytest.raises(RuntimeError):
        publisher.owner.evidence(state["lease"], deadline=time.monotonic() + 2)
    record = case.call(case.owner.status(deadline=time.monotonic() + 3))
    with (
        pytest.raises(RuntimeError),
        _handoff(case, record, deadline=time.monotonic() + 3, shutdown=threading.Event()),
    ):
        pytest.fail("cleanup without an exit result supplied an eligible handoff")
    assert not _root_is_held(publisher.evidence_root)
    _report("reconcile-control", cleanup=state["cleanup"], observation=observed)


def _exercise(case: JobBuildCase, watch: TimeoutWatch, release: ReleaseObservation) -> Json:
    """Separate actual deadline qualification from the later automatic-cleanup assertion."""
    admission = case.call(case.owner.submit(deadline=time.monotonic() + 5))
    assert admission["build"]["request"] == case.submission
    _report("handle-entered")
    result = case.publisher.service.handle(case.publisher.command)
    assert watch.ready.wait(1), "the actual recipe did not acknowledge FIFO readiness"
    assert release.timeout is not None and release.expired and not release.returned
    deadline = release.deadline
    assert deadline is not None and time.monotonic() >= deadline
    assert result["status"] == "reconciliation_required" and result["collectionVerified"] is False
    automatic = _observe_before_reconcile(case)
    _report("automatic-observation", **automatic)
    _reconcile_control(case)
    _expired_job(case, deadline)
    http = case.http
    assert http is not None
    assert [request["method"] for request in http.requests] == ["POST", "GET"]
    return automatic


def _child(root: Path) -> None:
    """Close every ordinary resource before reporting a body result to the outer test."""
    case = JobBuildCase(root)
    descriptor: int | None = None
    watch: TimeoutWatch | None = None
    phase = "setup"
    try:
        fifo = root / "recipe-ready.fifo"
        os.mkfifo(fifo, mode=0o600)
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
        with pytest.MonkeyPatch.context() as patches:
            _timeout_recipe(case, fifo, patches)
            watch = TimeoutWatch(case, descriptor)
            release = ReleaseObservation(case)
            release.install(patches)
            watch.thread.start()
            phase = "body-or-control"
            _exercise(case, watch, release)
    except BaseException as error:
        _report("fixture-failure", phase=phase, error=repr(error))
        raise
    finally:
        try:
            try:
                case.publisher.service.reconcile()
            finally:
                case.close()
        except BaseException as error:
            _report("cleanup-failure", error=repr(error))
            raise
        finally:
            try:
                if watch is not None and watch.thread.ident is not None:
                    watch.finish()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    assert case.loop.is_closed() and not case.loop_thread.is_alive()
    assert case.client._client.is_closed and case.journal.snapshot()["closed"]
    assert all(child.poll() is not None for child in case.publisher.scheduler.children.values())
    _report("closed")


def _run_child(root: Path) -> tuple[list[Json], Json]:
    """Reap only this exact fixture process before the parent assesses any body behavior."""
    child = subprocess.Popen(
        [sys.executable, "-B", "-u", "-m", _MODULE, str(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    forced = timed_out = False
    stdout = stderr = ""
    try:
        try:
            stdout, stderr = child.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        if child.poll() is None:
            forced = True
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate(timeout=3)
    events = [
        json.loads(line) for line in stdout.splitlines() if line.startswith('{"timeoutFixture":')
    ]
    return events, {
        "exit": child.returncode,
        "forced": forced,
        "timed_out": timed_out,
        "stderr": stderr,
    }


def test_actual_deadline_failure_cleans_exact_lease_before_explicit_reconcile(
    tmp_path: Path,
) -> None:
    """Require automatic cleanup only after setup, real expiry, control and reaping pass."""
    events, diagnostic = _run_child(tmp_path)
    kinds = [event["timeoutFixture"] for event in events]
    assert diagnostic["exit"] == 0 and not diagnostic["forced"] and not diagnostic["timed_out"], (
        events,
        diagnostic,
    )
    assert (
        kinds.count("recipe-owned")
        == kinds.count("recipe-ready")
        == kinds.count("actual-timeout")
        == 1
    )
    assert kinds[-1] == "closed" and kinds.count("reconcile-control") == 1, events
    automatic = [event for event in events if event["timeoutFixture"] == "automatic-observation"]
    assert len(automatic) == 1, events
    before = automatic[0]
    assert before["terminal"] is None and before["published"] is False
    # This is the intended RED. All observations above precede successful explicit cleanup.
    assert before["cleanup"] == before["step_cleanup"] == "confirmed_empty", before
    assert before["retained_observation"] == before["actual_observation"], before
    assert before["actual_observation"]["schedulerTerminal"] is True, before
    assert before["actual_observation"]["kernelEmpty"] is True, before
    assert before["child_exit"] is not None, before


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("this module requires its exact private fixture root")
    _child(Path(sys.argv[1]).resolve(strict=True))
