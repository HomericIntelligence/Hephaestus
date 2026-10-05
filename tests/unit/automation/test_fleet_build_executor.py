"""Exercise durable scheduler ownership with actual gated local child processes.

Scheduler identities are explicit fixtures. These tests do not invoke Slurm,
qualify an allocation or image, or establish Pyxis isolation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_contract import encoded
from hephaestus.automation.fleet_build_executor import SlurmStepOwner
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_snapshot import SnapshotPolicy

FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_build"
_LAUNCHER = """
import os, sys
sys.stdout.write('ready\\n')
sys.stdout.flush()
if sys.stdin.readline() != 'run\\n':
    raise SystemExit(73)
os.execv(sys.argv[1], [sys.argv[1], '--justfile', sys.argv[2], 'test-unit'])
"""


def inputs(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use actual producer recipe bytes with a clearly synthetic scheduler binding."""
    data = json.loads((FIXTURE / "controller.json").read_bytes())
    command = data["admission"]["command"]
    policy = command["payload"]["policy"]
    assert hashlib.sha256(encoded(policy)).hexdigest() == command["payload"]["policyDigest"]
    assert (
        policy["workspace"]["snapshotPolicyDigest"]
        == command["payload"]["snapshot"]["policyDigest"]
        == SnapshotPolicy(max_members=20, max_bytes=8192).digest
    )
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    for name, value in data["sourceFiles"].items():
        (workspace / name).write_bytes(value.encode())
    allocation = policy["allocation"]
    binding = {
        "schema": "hi/hephaestus/slurm-allocation/v1",
        "allocationId": allocation["id"],
        "generation": allocation["generation"],
        "qualificationReceiptDigest": allocation["qualificationReceiptDigest"],
        "cluster": "fixture-cluster",
        "jobId": "77001",
        "jobStart": "2026-09-13T00:00:00Z",
        "node": "fixture-node",
        "uid": os.getuid(),
        "bootId": "fixture-boot-incarnation",
    }
    lease = {
        "schema": "hi/hephaestus/build-lease/v1",
        "leaseId": "0123456789abcdef0123456789abcdef",
        "buildId": command["targetId"],
        "attempt": command["payload"]["attempt"],
        "commandId": command["commandId"],
        "workerId": allocation["workerId"],
        "allocationId": allocation["id"],
        "generation": allocation["generation"],
        "policyDigest": command["payload"]["policyDigest"],
        "snapshotDigest": command["payload"]["snapshot"]["manifestDigest"],
        "workspace": str(workspace),
        "policy": policy,
    }
    return lease, binding


def retained(root: Path) -> list[dict[str, Any]]:
    """Read actual flushed owner records at the external effect boundary."""
    path = root / "receipts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["value"] for line in path.read_bytes().splitlines()]


class ProcessScheduler:
    """A typed scheduler fixture owns real gated processes and exact observations."""

    def __init__(self, state: Path, binding: dict[str, Any]) -> None:
        self.state, self.binding = state, copy.deepcopy(binding)
        self.children: dict[str, subprocess.Popen[str]] = {}
        self.identities: dict[str, dict[str, Any]] = {}
        self.starts: list[str] = []
        self.releases: list[str] = []
        self.cancels: list[dict[str, Any]] = []
        self.drop_start_reply = False
        self.kernel_unknown = False
        self.wrong_identity = False

    def start(self, lease: dict[str, Any], nonce: str, *, deadline: float) -> dict[str, Any]:
        """Observe durable intent, then create a child that cannot run the recipe yet."""
        assert deadline > time.monotonic()
        assert any(
            row.get("phase") == "start_intent"
            and row.get("launchNonce") == nonce
            and row.get("lease") == lease
            for row in retained(self.state)
        )
        executable = shutil.which("just")
        assert executable is not None, "the supported Just executable is required"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LAUNCHER,
                executable,
                str(Path(lease["workspace"]) / "justfile"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": os.defpath},
            start_new_session=True,
        )
        self.children[nonce] = child
        self.starts.append(nonce)
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], min(2, deadline - time.monotonic()))[0]
        assert child.stdout.readline() == "ready\n"
        identity = {
            "schema": "hi/hephaestus/slurm-step/v1",
            "allocation": copy.deepcopy(self.binding),
            "leaseId": lease["leaseId"],
            "launchNonce": nonce,
            "stepId": str(len(self.starts) - 1),
            "stepStart": f"fixture-start-{child.pid}",
            "cgroup": f"fixture-cgroup-{child.pid}",
        }
        self.identities[nonce] = identity
        if self.drop_start_reply:
            raise ConnectionError("fixture lost reply after the real child became ready")
        value = copy.deepcopy(identity)
        if self.wrong_identity:
            value["allocation"]["jobStart"] = "different-incarnation"
        return value

    def find(self, nonce: str, *, deadline: float) -> list[dict[str, Any]]:
        """Find an exact retained nonce; this is a fixture, not scheduler accounting."""
        assert deadline > time.monotonic()
        return [copy.deepcopy(self.identities[nonce])] if nonce in self.identities else []

    def release(
        self,
        identity: dict[str, Any],
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: threading.Event,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        """Execute the real registered Just recipe only after exact durable ownership."""
        nonce = identity["launchNonce"]
        assert identity == self.identities[nonce]
        assert argv == ("just", "test-unit")
        assert any(
            row.get("phase") == "release_intent" and row.get("step") == identity
            for row in retained(self.state)
        )
        assert not shutdown.is_set()
        self.releases.append(nonce)
        child = self.children[nonce]
        stdout, stderr = child.communicate("run\n", timeout=min(3, deadline - time.monotonic()))
        assert len(stdout.encode()) + len(stderr.encode()) <= max_output_bytes
        return {
            "outcome": "completed" if child.returncode == 0 else "failed",
            "exitCode": child.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def cancel(self, identity: dict[str, Any], *, deadline: float) -> None:
        """Signal only the exact retained child, never its enclosing allocation."""
        assert deadline > time.monotonic()
        nonce = identity["launchNonce"]
        assert identity == self.identities[nonce]
        self.cancels.append(copy.deepcopy(identity))
        child = self.children[nonce]
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=2)

    def observe(self, identity: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Derive fixture emptiness from an owned process and retained identity."""
        assert deadline > time.monotonic()
        nonce = identity["launchNonce"]
        assert identity == self.identities[nonce]
        child = self.children[nonce]
        terminal = child.poll() is not None
        try:
            os.killpg(child.pid, 0)
            empty = False
        except ProcessLookupError:
            empty = True
        return {
            "step": copy.deepcopy(identity),
            "schedulerTerminal": terminal,
            "kernelEmpty": None if self.kernel_unknown else empty,
        }

    def close(self) -> None:
        """Reap the fixture's own processes after an assertion failure."""
        for child in self.children.values():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=2)


@contextmanager
def owner_fixture(
    tmp_path: Path,
) -> Iterator[tuple[SlurmStepOwner, ProcessScheduler, dict[str, Any]]]:
    """Keep owner and scheduler cleanup explicit on both RED and GREEN paths."""
    lease, binding = inputs(tmp_path)
    scheduler = ProcessScheduler(tmp_path / "owner", binding)
    owner = SlurmStepOwner(state_dir=scheduler.state, binding=binding, transport=scheduler)
    try:
        yield owner, scheduler, lease
    finally:
        try:
            owner.close()
        finally:
            scheduler.close()


def run(owner: SlurmStepOwner, lease: dict[str, Any]) -> dict[str, Any] | None:
    """Give one fixed recipe attempt a finite shared wall and output bound."""
    return owner.run(
        lease,
        argv=("just", "test-unit"),
        deadline=time.monotonic() + 5,
        shutdown=threading.Event(),
        max_output_bytes=65536,
    )


def test_fixture_really_gates_and_executes_the_producer_justfile(tmp_path: Path) -> None:
    """Positive fixture control proves a real Just child, separate from the new owner."""
    lease, binding = inputs(tmp_path)
    scheduler = ProcessScheduler(tmp_path / "owner", binding)
    journal = WorkerJournal(scheduler.state)
    try:
        journal.append(
            "build_step", {"phase": "start_intent", "launchNonce": "control", "lease": lease}
        )
        identity = scheduler.start(lease, "control", deadline=time.monotonic() + 5)
        assert scheduler.children["control"].poll() is None
        assert scheduler.releases == []
        journal.append("build_step", {"phase": "release_intent", "step": identity})
        result = scheduler.release(
            identity,
            argv=("just", "test-unit"),
            deadline=time.monotonic() + 5,
            shutdown=threading.Event(),
            max_output_bytes=65536,
        )
        assert result["stdout"] == "fixture build\n"
        assert result["exitCode"] == 0
        assert scheduler.observe(identity, deadline=time.monotonic() + 5)["kernelEmpty"] is True
    finally:
        journal.close()
        scheduler.close()


def test_owner_persists_exact_step_before_recipe_release_and_replays_once(tmp_path: Path) -> None:
    """A real output requires durable intent and step ownership before release."""
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        result = run(owner, lease)
        assert isinstance(result, dict), "the owner returned no execution observation"
        assert result["stdout"] == "fixture build\n"
        assert result["exitCode"] == 0
        assert run(owner, lease) == result
        assert len(scheduler.starts) == len(scheduler.releases) == 1
        assert owner.dispose(lease, deadline=time.monotonic() + 5) == {
            "leaseId": lease["leaseId"],
            "cleanup": "confirmed_empty",
        }


def test_lost_start_reply_restart_never_starts_or_releases_a_second_step(tmp_path: Path) -> None:
    """A real gated child survives a lost response; restart can only reconcile it."""
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        scheduler.drop_start_reply = True
        with pytest.raises(RuntimeError, match="reconciliation"):
            run(owner, lease)
        assert len(scheduler.starts) == 1
        nonce = scheduler.starts[0]
        assert scheduler.children[nonce].poll() is None
        owner.close()
        restarted = SlurmStepOwner(
            state_dir=scheduler.state, binding=scheduler.binding, transport=scheduler
        )
        try:
            with pytest.raises(RuntimeError, match="reconciliation"):
                run(restarted, lease)
            assert scheduler.starts == [nonce]
            assert scheduler.releases == []
            assert restarted.dispose(lease, deadline=time.monotonic() + 5) == {
                "leaseId": lease["leaseId"],
                "cleanup": "confirmed_empty",
            }
            assert scheduler.cancels == [scheduler.identities[nonce]]
            assert scheduler.children[nonce].poll() is not None
        finally:
            restarted.close()


def test_wrong_scheduler_incarnation_is_never_released(tmp_path: Path) -> None:
    """A reused job number cannot substitute for the operator-bound incarnation."""
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        scheduler.wrong_identity = True
        with pytest.raises(RuntimeError, match="reconciliation"):
            run(owner, lease)
        assert len(scheduler.starts) == 1
        assert scheduler.releases == []
        assert scheduler.cancels == []


def test_accounting_without_kernel_absence_cannot_confirm_disposal(tmp_path: Path) -> None:
    """An exited local child and scheduler terminal flag cannot invent node proof."""
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        result = run(owner, lease)
        assert isinstance(result, dict), "the owner returned no execution observation"
        scheduler.kernel_unknown = True
        with pytest.raises(RuntimeError, match="cleanup"):
            owner.dispose(lease, deadline=time.monotonic() + 5)
        scheduler.kernel_unknown = False
        assert owner.dispose(lease, deadline=time.monotonic() + 5) == {
            "leaseId": lease["leaseId"],
            "cleanup": "confirmed_empty",
        }


def test_cancelled_or_expired_start_has_no_scheduler_effect(tmp_path: Path) -> None:
    """The owner rejects admission before any submission or recipe release."""
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        stop = threading.Event()
        stop.set()
        with pytest.raises(InterruptedError):
            owner.run(
                lease,
                argv=("just", "test-unit"),
                deadline=time.monotonic() + 5,
                shutdown=stop,
                max_output_bytes=65536,
            )
        with pytest.raises(TimeoutError):
            owner.run(
                lease,
                argv=("just", "test-unit"),
                deadline=time.monotonic() - 1,
                shutdown=threading.Event(),
                max_output_bytes=65536,
            )
        assert scheduler.starts == scheduler.releases == scheduler.cancels == []


@pytest.mark.parametrize("required_sync", ["trusted-parent", "owner-directory", "intent-file"])
def test_owner_syncs_created_state_before_transport_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required_sync: str
) -> None:
    """Observe real descriptor synchronization before an actual gated child starts."""
    calls: list[dict[str, Any]] = []
    at_start: list[dict[str, Any]] = []
    nonces: list[str] = []
    state = tmp_path / "owner"
    journal = state / "receipts.jsonl"
    real_fsync = os.fsync

    def observe_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        entries = os.listdir(descriptor) if stat.S_ISDIR(metadata.st_mode) else []
        content = b""
        if journal.exists():
            expected = journal.stat()
            if identity == (expected.st_dev, expected.st_ino):
                content = os.pread(descriptor, metadata.st_size, 0)
        calls.append({"identity": identity, "entries": entries, "content": content})

    monkeypatch.setattr(os, "fsync", observe_fsync)
    with owner_fixture(tmp_path) as (owner, scheduler, lease):
        real_start = scheduler.start

        def observe_start(
            admitted: dict[str, Any], nonce: str, *, deadline: float
        ) -> dict[str, Any]:
            at_start.extend(copy.deepcopy(calls))
            nonces.append(nonce)
            return real_start(admitted, nonce, deadline=deadline)

        monkeypatch.setattr(scheduler, "start", observe_start)
        result = run(owner, lease)
        assert isinstance(result, dict)
        assert result["stdout"] == "fixture build\n" and result["exitCode"] == 0
        assert len(scheduler.starts) == len(scheduler.releases) == len(nonces) == 1
        assert owner.dispose(lease, deadline=time.monotonic() + 5)["cleanup"] == "confirmed_empty"
        assert scheduler.children[nonces[0]].poll() is not None

        paths = {
            "trusted-parent": tmp_path,
            "owner-directory": state,
            "intent-file": journal,
        }
        metadata = paths[required_sync].stat()
        identity = (metadata.st_dev, metadata.st_ino)
        observed = [call for call in at_start if call["identity"] == identity]
        if required_sync == "trusted-parent":
            completed = any(state.name in call["entries"] for call in observed)
        elif required_sync == "owner-directory":
            completed = any(
                {"writer.lock", "receipts.jsonl"} <= set(call["entries"]) for call in observed
            )
        else:
            completed = any(
                record["value"].get("phase") == "start_intent"
                and record["value"].get("launchNonce") == nonces[0]
                and record["value"].get("lease") == lease
                for call in observed
                for line in call["content"].splitlines()
                for record in [json.loads(line)]
            )
        assert completed, f"{required_sync} was not synchronized before transport.start"
