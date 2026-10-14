"""Exercise one consumer against real controller and source-artifact fixtures.

The controller allocation is synthetic. The injected executor runs only a fixed
Python test child; it neither executes the recipe nor qualifies OS isolation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_snapshot import SnapshotPolicy, restore_snapshot
from hephaestus.utils.helpers import run_subprocess

FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_build"


def contract() -> dict[str, Any]:
    """Read the actual exported controller input without regenerating its hashes."""
    return json.loads((FIXTURE / "controller.json").read_bytes())


def records(state: Path) -> list[dict[str, Any]]:
    """Observe durable bytes at the external effect boundary."""
    path = state / "receipts.jsonl"
    return [json.loads(line)["value"] for line in path.read_bytes().splitlines()]


class Controller:
    """Replay the real grant response with controlled denial and lost replies."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.claims: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.response = contract()["grantResponse"]
        self.failure: Exception | None = None
        self.drop_once = False

    def claim_run(self, build_id: str, claim: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        assert build_id == contract()["admission"]["command"]["targetId"]
        assert deadline > time.monotonic()
        assert any(value.get("claim") == claim for value in records(self.state))
        self.claims.append(copy.deepcopy(claim))
        assert claim == contract()["claimRequest"]
        if self.failure:
            raise self.failure
        if self.drop_once:
            self.drop_once = False
            raise ConnectionError("synthetic committed grant response was lost")
        return copy.deepcopy(self.response)

    def publish_fact(
        self, build_id: str, fact: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        assert deadline > time.monotonic()
        assert build_id == contract()["admission"]["command"]["targetId"]
        assert any(value.get("terminal") == fact for value in records(self.state))
        self.facts.append(copy.deepcopy(fact))
        return {"eventId": fact["eventId"]}


class FixtureExecutor:
    """Controlled real child; not a production Linux/container executor."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.prepared: list[dict[str, Any]] = []
        self.started: list[dict[str, Any]] = []
        self.disposed: list[dict[str, Any]] = []

    def prepare(self, lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        assert deadline > time.monotonic()
        assert any(
            value.get("grant") == contract()["grantResponse"]["grant"]
            for value in records(self.state)
        )
        workspace = Path(lease["workspace"])
        for name, data in contract()["sourceFiles"].items():
            assert (workspace / name).read_bytes() == data.encode()
        assert not self.state.is_relative_to(workspace)
        self.prepared.append(copy.deepcopy(lease))
        return copy.deepcopy(lease)

    def run(
        self,
        lease: dict[str, Any],
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: Any,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        import sys

        assert argv == ("just", "test-unit")
        assert any(
            value.get("phase") == "starting" and value.get("lease") == lease
            for value in records(self.state)
        )
        assert max_output_bytes == 65536
        self.started.append(copy.deepcopy(lease))
        result = run_subprocess(
            [sys.executable, "-c", "print('controlled child')"],
            cwd=lease["workspace"],
            env={"PATH": os.defpath},
            timeout=min(5, deadline - time.monotonic()),
            check=False,
            shutdown=shutdown,
            max_output_bytes=max_output_bytes,
            track_process_group=True,
            log_on_error=False,
        )
        return {
            "outcome": "completed",
            "exitCode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def dispose(self, lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        assert deadline > time.monotonic()
        self.disposed.append(copy.deepcopy(lease))
        return {"leaseId": lease["leaseId"], "cleanup": "confirmed_empty"}


def make_service(root: Path, client: Controller, executor: FixtureExecutor | None):
    """Load the new service inside each test so absence is a behavioral RED."""
    from hephaestus.automation.fleet_build_supervisor import BuildSnapshot, BuildSupervisor

    root.mkdir(mode=0o700, exist_ok=True)
    return BuildSupervisor(
        state_dir=root / "state",
        workspace_root=root / "workspaces",
        policy=contract()["admission"]["command"]["payload"]["policy"],
        client=client,
        snapshot=BuildSnapshot(FIXTURE / "snapshot", SnapshotPolicy(20, 8192)),
        executor=executor,
        claim_id_factory=lambda: contract()["claimRequest"]["claimId"],
    )


def test_actual_producer_artifact_and_recipe_bytes_are_a_valid_control(tmp_path: Path) -> None:
    """The positive input really restores and matches independent producer hashes."""
    parent = tmp_path / "owned"
    parent.mkdir(mode=0o700)
    data = contract()
    command = data["admission"]["command"]
    restored = restore_snapshot(
        FIXTURE / "snapshot",
        parent / "restored",
        commitment=command["payload"]["snapshot"],
        policy=SnapshotPolicy(20, 8192),
    )
    for name, field in (("justfile", "recipeDigest"), ("uv.lock", "lockDigest")):
        actual = (restored / name).read_bytes()
        assert actual == data["sourceFiles"][name].encode()
        assert hashlib.sha256(actual).hexdigest() == command["payload"]["policy"]["recipe"][field]
    for name, expected in (
        ("policy", command["payload"]["policyDigest"]),
        ("grant", data["grantResponse"]["grant"]["grantId"]),
    ):
        assert hashlib.sha256(data["digestInputs"][name].encode()).hexdigest() == expected


def test_grant_and_start_are_durable_before_one_controlled_child(tmp_path: Path) -> None:
    """Observe persisted identities at callbacks and replay without another child."""
    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(contract()["admission"]["command"])
        assert result["status"] == "completed"
        assert result["collectionVerified"] is False
        assert service.handle(contract()["admission"]["command"]) == result
        assert len(executor.started) == len(executor.disposed) == len(client.facts) == 1
        fact = client.facts[0]
        assert fact["cleanup"] == "confirmed_empty"
        assert (
            fact["snapshotDigest"]
            == contract()["admission"]["command"]["payload"]["snapshot"]["manifestDigest"]
        )
        assert "collectionVerified" not in fact
        assert fact["exitCode"] == 0
        assert service.status() == result


@pytest.mark.parametrize(
    "mode", ("denied", "wrong-command", "wrong-claim", "float-generation", "digest")
)
def test_ungranted_or_mismatched_response_never_starts(tmp_path: Path, mode: str) -> None:
    """Valid restored input cannot compensate for absent or altered run authority."""
    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    if mode == "denied":
        client.failure = PermissionError("synthetic parent stopped first")
    elif mode == "wrong-command":
        client.response["command"]["targetId"] = "different-build"
    elif mode == "wrong-claim":
        client.response["grant"]["claim"]["claimId"] = "different-claim"
    elif mode == "float-generation":
        client.response["grant"]["claim"]["generation"] = 1.0
    else:
        client.response["grant"]["grantId"] = "0" * 64
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(contract()["admission"]["command"])
        assert result["status"] == "reconciliation_required"
        assert executor.prepared == executor.started == client.facts == []


def test_lost_grant_reply_restarts_with_the_same_persisted_claim(tmp_path: Path) -> None:
    """A real journal restart reuses the only claim before its first process."""
    client = Controller(tmp_path / "state")
    client.drop_once = True
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
    assert executor.started == []
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "completed"
    assert client.claims == [contract()["claimRequest"], contract()["claimRequest"]]
    assert len(executor.started) == 1


def test_no_isolation_executor_has_no_default_host_fallback(tmp_path: Path) -> None:
    """Missing deployment capability cannot turn fixed argv into host execution."""
    client = Controller(tmp_path / "state")
    with pytest.raises(ValueError, match="isolation"):
        make_service(tmp_path, client, None)
    assert client.claims == client.facts == []


def test_cancel_before_delivery_persists_a_restart_safe_no_start_fence(tmp_path: Path) -> None:
    """A real first cancel reaches neither grant nor recipe, even after restart."""
    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    cancel = contract()["cancelResponse"]["command"]
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(cancel)
        assert result["status"] == "cancelled"
        assert service.handle(contract()["admission"]["command"]) == result
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "cancelled"
        assert service.handle(cancel)["status"] == "cancelled"
    assert client.claims == executor.prepared == executor.started == []
    assert len(client.facts) == 1
    fact = client.facts[0]
    assert fact["commandId"] == cancel["commandId"]
    assert fact["startFenced"] is True
    assert fact["exitCode"] is None and fact["cleanup"] == "confirmed_empty"


def test_different_journal_cannot_share_the_live_workspace_owner(tmp_path: Path) -> None:
    """Separate state directories do not bypass the same allocation's workspace lease."""
    from hephaestus.automation.fleet_build_supervisor import BuildSnapshot, BuildSupervisor

    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor):
        with pytest.raises((ValueError, RuntimeError), match=r"owner|writer|lease"):
            BuildSupervisor(
                state_dir=tmp_path / "other-state",
                workspace_root=tmp_path / "workspaces",
                policy=contract()["admission"]["command"]["payload"]["policy"],
                client=client,
                snapshot=BuildSnapshot(FIXTURE / "snapshot", SnapshotPolicy(20, 8192)),
                executor=executor,
            )
    assert client.claims == executor.started == []


def test_cancel_during_the_real_grant_callback_cannot_start(tmp_path: Path) -> None:
    """A grant callback crossing the local fence retains the granted historical identity."""
    import threading

    entered, release = threading.Event(), threading.Event()
    client = Controller(tmp_path / "state")
    original = client.claim_run

    def blocked(build_id: str, claim: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        result = original(build_id, claim, deadline=deadline)
        entered.set()
        assert release.wait(3)
        return result

    client.claim_run = blocked  # type: ignore[method-assign]
    executor = FixtureExecutor(tmp_path / "state")
    service = make_service(tmp_path, client, executor)
    thread = threading.Thread(target=lambda: service.handle(contract()["admission"]["command"]))
    thread.start()
    try:
        assert entered.wait(2)
        assert service.handle(contract()["cancelResponse"]["command"])["status"] == "cancelling"
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert service.status()["status"] == "cancelled"
        assert executor.prepared == executor.started == []
        assert client.facts[0]["commandId"] == contract()["cancelRequest"]["commandId"]
    finally:
        release.set()
        thread.join(4)
        service.close()


class LiveFixtureExecutor(FixtureExecutor):
    """Use a real fixed Python child, and observe its owned process disappearance."""

    def __init__(self, state: Path) -> None:
        super().__init__(state)
        import threading

        self.pid_file = state.parent / "child-pid"
        self.shutdown: Any = threading.Event()
        self.thread: Any = None
        self.lost = False
        self.observations = 0

    def _child(self, lease: dict[str, Any], deadline: float) -> dict[str, Any]:
        import sys

        script = (
            "import os,pathlib,time; "
            f"pathlib.Path({str(self.pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        try:
            result = run_subprocess(
                [sys.executable, "-c", script],
                env={"PATH": os.defpath},
                cwd=lease["workspace"],
                timeout=min(5, deadline - time.monotonic()),
                shutdown=self.shutdown,
                track_process_group=True,
                max_output_bytes=65536,
                log_on_error=False,
            )
            return {
                "outcome": "completed",
                "exitCode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except InterruptedError:
            return {"outcome": "cancelled", "exitCode": None, "stdout": "", "stderr": ""}

    def run(
        self,
        lease: dict[str, Any],
        *,
        argv: tuple[str, ...],
        deadline: float,
        shutdown: Any,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        import threading

        assert argv == ("just", "test-unit") and max_output_bytes == 65536
        self.started.append(copy.deepcopy(lease))
        self.pid_file = Path(lease["workspace"]) / "controlled-child-pid"
        self.shutdown = shutdown
        if self.lost:
            self.thread = threading.Thread(target=self._child, args=(lease, deadline))
            self.thread.start()
            self.wait_started()
            raise ConnectionError("synthetic reply lost after actual owned child start")
        return self._child(lease, deadline)

    def wait_started(self) -> None:
        end = time.monotonic() + 2
        while not self.pid_file.exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert self.pid_file.exists()
        os.kill(int(self.pid_file.read_text()), 0)

    def dispose(self, lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        self.shutdown.set()
        if self.thread is not None:
            self.thread.join(max(0, deadline - time.monotonic()))
            assert not self.thread.is_alive()
        if self.pid_file.exists():
            end = min(deadline, time.monotonic() + 2)
            while time.monotonic() < end:
                try:
                    os.kill(int(self.pid_file.read_text()), 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("controlled owned child is still alive")
        return super().dispose(lease, deadline=deadline)

    def emergency_stop(self) -> None:
        self.shutdown.set()
        if self.thread is not None:
            self.thread.join(6)


def test_uncertain_actual_start_is_not_repeated_and_can_be_cleaned(tmp_path: Path) -> None:
    """Lose the actual process-start reply, reopen its journal and retain cleanup ownership."""
    client = Controller(tmp_path / "state")
    executor = LiveFixtureExecutor(tmp_path / "state")
    executor.lost = True
    service = make_service(tmp_path, client, executor)
    try:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
        executor.wait_started()
        service.close()
        service = make_service(tmp_path, client, executor)
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
        assert len(executor.started) == 1
        assert service.reconcile().get("cleanup") == "confirmed_empty"
        assert len(executor.disposed) == 1
        assert client.facts == []  # cleanup cannot invent an exit result after uncertain start
    finally:
        executor.emergency_stop()
        service.close()


def test_cancel_stops_the_actual_active_child_before_terminal_fact(tmp_path: Path) -> None:
    """The real process disappears before the exact cancellation fact is published."""
    import threading

    client = Controller(tmp_path / "state")
    executor = LiveFixtureExecutor(tmp_path / "state")
    service = make_service(tmp_path, client, executor)
    thread = threading.Thread(target=lambda: service.handle(contract()["admission"]["command"]))
    thread.start()
    try:
        executor.wait_started()
        assert service.handle(contract()["cancelResponse"]["command"])["status"] == "cancelling"
        thread.join(4)
        assert not thread.is_alive()
        assert service.status()["status"] == "cancelled"
        assert len(executor.disposed) == len(client.facts) == 1
        assert client.facts[0]["outcome"] == "cancelled"
        assert client.facts[0]["commandId"] == contract()["cancelRequest"]["commandId"]
    finally:
        executor.emergency_stop()
        thread.join(6)
        service.close()


def test_lost_terminal_reply_replays_exact_fact_without_another_child(tmp_path: Path) -> None:
    """A real persisted terminal is resent after its first publication reply is lost."""
    client = Controller(tmp_path / "state")
    original = client.publish_fact

    def lost_once(build_id: str, fact: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        response = original(build_id, fact, deadline=deadline)
        if len(client.facts) == 1:
            raise ConnectionError("reply lost after terminal acceptance")
        return response

    client.publish_fact = lost_once  # type: ignore[method-assign]
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "completed"
    assert len(executor.started) == len(executor.disposed) == 1
    assert len(client.facts) == 2 and client.facts[0] == client.facts[1]


def test_unconfirmed_disposal_never_publishes_and_reconciles_exact_lease(tmp_path: Path) -> None:
    """An exited real child does not make a wrong cleanup receipt authoritative."""
    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    original = executor.dispose

    def wrong(lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        result = original(lease, deadline=deadline)
        return {**result, "leaseId": "another-owner"}

    executor.dispose = wrong  # type: ignore[method-assign]
    with make_service(tmp_path, client, executor) as service:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
        assert client.facts == [] and len(executor.started) == 1
        executor.dispose = original  # type: ignore[method-assign]
        assert service.reconcile().get("cleanup") == "confirmed_empty"
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
        assert len(executor.started) == 1 and client.facts == []
        assert executor.disposed[0] == executor.disposed[1]


@pytest.mark.parametrize(
    "field",
    [
        "lease_workspace",
        "lease_attempt",
        "claim_generation",
        "published_type",
        "phase",
        "terminal_outcome",
        "terminal_command",
        "cleanup_type",
    ],
)
def test_corrupt_retained_authority_is_rejected_before_external_effects(
    tmp_path: Path, field: str
) -> None:
    """Corrupt an actual durable checkpoint through the journal API, never invent a run."""
    from hephaestus.automation.fleet_journal import WorkerJournal

    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "completed"
    with_journal = WorkerJournal(tmp_path / "state")
    try:
        retained = copy.deepcopy(
            [r["value"] for r in with_journal.records if r["kind"] == "build"][-1]
        )
        if field == "lease_workspace":
            retained["lease"]["workspace"] = str(tmp_path / "unowned")
        elif field == "lease_attempt":
            retained["lease"]["attempt"] = 1.0
        elif field == "claim_generation":
            retained["claim"]["generation"] = 1.0
        elif field == "published_type":
            retained["published"] = "true"
        elif field == "phase":
            retained["phase"] = "unrecognized"
        elif field == "terminal_outcome":
            retained["terminal"]["outcome"] = "unobserved-success"
        elif field == "terminal_command":
            retained["terminal"]["commandId"] = "unowned-command"
        else:
            retained["cleanup"] = True
        with_journal.append("build", retained)
    finally:
        with_journal.close()
    before = copy.deepcopy(
        (client.claims, client.facts, executor.prepared, executor.started, executor.disposed)
    )
    with pytest.raises((ValueError, RuntimeError), match=r"build|retained|lease|grant"):
        make_service(tmp_path, client, executor)
    assert before == (
        client.claims,
        client.facts,
        executor.prepared,
        executor.started,
        executor.disposed,
    )


class BoundedFixtureExecutor(LiveFixtureExecutor):
    """Observe an actual fixed child timeout or combined-output limit."""

    overflow = False
    timeout_observed = False
    limit_observation: dict[str, Any] | None = None

    def _child(self, lease: dict[str, Any], deadline: float) -> dict[str, Any]:
        import subprocess
        import sys

        from hephaestus.utils.helpers import SubprocessOutputLimitExceeded

        script = (
            "import os,pathlib,time; "
            f"pathlib.Path({str(self.pid_file)!r}).write_text(str(os.getpid())); "
            + ("print('x'*70000,flush=True); time.sleep(30)" if self.overflow else "time.sleep(30)")
        )
        try:
            result = run_subprocess(
                [sys.executable, "-c", script],
                env={"PATH": os.defpath},
                cwd=lease["workspace"],
                timeout=min(0.3, deadline - time.monotonic()),
                shutdown=self.shutdown,
                track_process_group=True,
                max_output_bytes=65536,
                log_on_error=False,
            )
        except SubprocessOutputLimitExceeded as error:
            self.limit_observation = {
                "limit": error.limit,
                "bytes": len(error.stdout.encode()) + len(error.stderr.encode()),
            }
            raise
        except subprocess.TimeoutExpired as error:
            self.timeout_observed = True
            assert not self.overflow
            return {
                "outcome": "timed_out",
                "exitCode": None,
                "stdout": error.stdout or "",
                "stderr": error.stderr or "",
            }
        raise AssertionError(f"fixed bounded child unexpectedly exited: {result.returncode}")


def test_actual_timeout_requires_owned_disposal_before_typed_fact(tmp_path: Path) -> None:
    """A measured fixture deadline and absent owned process support a timeout fact."""
    client = Controller(tmp_path / "state")
    executor = BoundedFixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(contract()["admission"]["command"])
    assert executor.timeout_observed is True
    assert len(executor.started) == len(executor.disposed) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(executor.pid_file.read_text()), 0)
    assert result["status"] == "timed_out"
    assert len(client.facts) == 1
    assert client.facts[0]["outcome"] == "timed_out"
    assert client.facts[0]["exitCode"] is None


def test_actual_output_overflow_retains_uncertainty_without_public_output(tmp_path: Path) -> None:
    """The actual process helper bounds output; no invented exit is reported."""
    client = Controller(tmp_path / "state")
    executor = BoundedFixtureExecutor(tmp_path / "state")
    executor.overflow = True
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(contract()["admission"]["command"])
        assert result["status"] == "reconciliation_required"
        assert "stdout" not in result and "stderr" not in result
        assert service.reconcile().get("cleanup") == "confirmed_empty"
    assert len(executor.started) == len(executor.disposed) == 1
    assert client.facts == []
    assert executor.timeout_observed is False
    assert executor.limit_observation == {"limit": 65536, "bytes": 65536}
    with pytest.raises(ProcessLookupError):
        os.kill(int(executor.pid_file.read_text()), 0)


def test_lost_terminal_reply_cannot_publish_changed_private_output(tmp_path: Path) -> None:
    """Replay verifies actual private output bytes before sending their retained digest."""
    client = Controller(tmp_path / "state")
    original = client.publish_fact

    def lost(build_id: str, fact: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        original(build_id, fact, deadline=deadline)
        raise ConnectionError("actual fact delivered; reply lost")

    client.publish_fact = lost  # type: ignore[method-assign]
    executor = FixtureExecutor(tmp_path / "state")
    with make_service(tmp_path, client, executor) as service:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
    (tmp_path / "state" / "output.json").write_text('{"stdout":"changed","stderr":""}')
    client.publish_fact = original  # type: ignore[method-assign]
    with make_service(tmp_path, client, executor) as service:
        assert (
            service.handle(contract()["admission"]["command"])["status"]
            == "reconciliation_required"
        )
    assert len(executor.started) == 1 and len(client.facts) == 1


@pytest.mark.parametrize("phase", ["grant_pending", "granted"])
def test_retained_early_cleanup_without_cancel_is_rejected_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Reject one changed cleanup marker in an actual pre-execution checkpoint."""
    from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
    from hephaestus.automation.fleet_journal import WorkerJournal

    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    command = contract()["admission"]["command"]
    restore_calls: list[dict[str, Any]] = []

    def unavailable_restore(
        snapshot: BuildSnapshot,
        commitment: dict[str, Any],
        destination: Path,
        *,
        deadline: float,
    ) -> Path:
        assert snapshot.artifact == FIXTURE / "snapshot"
        assert commitment == command["payload"]["snapshot"]
        assert destination == tmp_path / "workspaces" / command["payload"]["snapshotWorkspace"]
        assert deadline > time.monotonic()
        restore_calls.append(copy.deepcopy(commitment))
        raise ConnectionError("controlled restore interruption before workspace creation")

    with monkeypatch.context() as patch:
        if phase == "grant_pending":
            client.failure = ConnectionError("controlled grant unavailable")
        else:
            patch.setattr(BuildSnapshot, "restore", unavailable_restore)
        with make_service(tmp_path, client, executor) as service:
            assert service.handle(command)["status"] == "reconciliation_required"

    assert len(client.claims) == 1
    assert client.facts == executor.prepared == executor.started == executor.disposed == []
    assert len(restore_calls) == (1 if phase == "granted" else 0)
    checkpoint = records(client.state)[-1]
    assert checkpoint["phase"] == phase
    assert checkpoint["grant"] == (
        contract()["grantResponse"]["grant"] if phase == "granted" else None
    )
    assert checkpoint["claim"] == contract()["claimRequest"]
    assert checkpoint["lease"] is None and checkpoint["cancel"] is None
    assert checkpoint["cleanup"] is None and checkpoint["terminal"] is None

    changed = copy.deepcopy(checkpoint)
    changed["cleanup"] = "confirmed_empty"
    journal = WorkerJournal(client.state)
    try:
        journal.append("build", changed)
    finally:
        journal.close()
    assert records(client.state)[-1] == changed
    client.failure = None
    before = copy.deepcopy(
        (client.claims, client.facts, executor.prepared, executor.started, executor.disposed)
    )

    with (
        pytest.raises(ValueError, match=r"build|retained|cleanup"),
        make_service(tmp_path, client, executor),
    ):
        pass

    assert before == (
        client.claims,
        client.facts,
        executor.prepared,
        executor.started,
        executor.disposed,
    )
    assert not (tmp_path / "workspaces" / command["payload"]["snapshotWorkspace"]).exists()


def test_interrupted_early_cancel_cleanup_recovers_exact_fact_without_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a real cleanup append when the next terminal append is interrupted."""
    from hephaestus.automation.fleet_journal import WorkerJournal

    class JournalInterruptionError(BaseException):
        """Stop one persistence operation without the service's exception recovery."""

    client = Controller(tmp_path / "state")
    executor = FixtureExecutor(tmp_path / "state")
    cancel = contract()["cancelResponse"]["command"]
    start = contract()["admission"]["command"]
    interrupted: list[dict[str, Any]] = []
    original_append = WorkerJournal.append

    def interrupt_terminal(journal: WorkerJournal, kind: str, value: dict[str, Any]) -> None:
        if kind == "build" and value.get("phase") == "terminal":
            checkpoint = records(journal.directory)[-1]
            assert checkpoint["phase"] == "grant_pending"
            assert checkpoint["command"] == start
            assert checkpoint["claim"] == contract()["claimRequest"]
            assert checkpoint["cancel"] == cancel
            assert checkpoint["cleanup"] == "confirmed_empty"
            assert checkpoint["grant"] is None and checkpoint["lease"] is None
            assert checkpoint["terminal"] is None and checkpoint["published"] is False
            interrupted.append(
                {
                    "checkpoint": copy.deepcopy(checkpoint),
                    "terminal": copy.deepcopy(value["terminal"]),
                }
            )
            raise JournalInterruptionError
        original_append(journal, kind, value)

    with monkeypatch.context() as patch:
        patch.setattr(WorkerJournal, "append", interrupt_terminal)
        with (
            make_service(tmp_path, client, executor) as service,
            pytest.raises(JournalInterruptionError),
        ):
            service.handle(cancel)

    assert len(interrupted) == 1
    assert records(client.state)[-1] == interrupted[0]["checkpoint"]
    assert client.claims == client.facts == []
    assert executor.prepared == executor.started == executor.disposed == []
    expected_fact = interrupted[0]["terminal"]
    assert expected_fact == {**contract()["terminalFact"], "eventId": expected_fact["eventId"]}

    with make_service(tmp_path, client, executor) as service:
        result = service.handle(start)
        assert result["status"] == "cancelled"
        assert result["cleanup"] == "confirmed_empty"
        assert result["collectionVerified"] is False
        assert service.handle(cancel) == service.handle(start) == result

    assert client.claims == []
    assert executor.prepared == executor.started == executor.disposed == []
    assert client.facts == [expected_fact]
    assert records(client.state)[-1]["terminal"] == expected_fact
    assert records(client.state)[-1]["published"] is True
    assert not (tmp_path / "workspaces" / start["payload"]["snapshotWorkspace"]).exists()
