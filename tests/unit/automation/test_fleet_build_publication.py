"""Publish actual local recipe results and independently collect their bytes.

Fresh admission and allocation data are controlled fixtures. The original
controller export stays unchanged. These tests do not qualify Slurm or Pyxis.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_contract import digest, encoded
from hephaestus.automation.fleet_build_executor import SlurmStepOwner
from hephaestus.automation.fleet_build_publication import PrivateBuildPublisher
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot, BuildSupervisor
from hephaestus.automation.fleet_snapshot import SnapshotPolicy, export_snapshot
from tests.fixtures.just_requirement import requires_just
from tests.unit.automation.test_fleet_build_executor import ProcessScheduler, inputs
from tests.unit.automation.test_fleet_build_supervisor import FIXTURE, contract, records


def fresh_source(root: Path) -> Path:
    """Create a real private Git source with the original harmless recipe bytes."""
    source = root / "source"
    source.mkdir(mode=0o700)
    for name, data in contract()["sourceFiles"].items():
        (source / name).write_bytes(data.encode())
        (source / name).chmod(0o644)
    git = shutil.which("git")
    assert git is not None, "the existing Git executable is required"
    environment = {
        "PATH": os.defpath,
        "HOME": str(root),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for arguments in (
        ("init", "--quiet"),
        ("add", "justfile", "uv.lock"),
        ("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture source"),
    ):
        subprocess.run(
            [git, *arguments],
            cwd=source,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=True,
        )
    return source


class PublicationController:
    """Control claim/fact replies while checking actual local journal order."""

    def __init__(self, root: Path, command: dict[str, Any]) -> None:
        self.root, self.command = root, command
        self.claims: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.drop_reply = False
        self.observe_fact: Any = lambda _: None

    def claim_run(self, build_id: str, claim: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Derive a controlled grant for exactly the supplied fresh admission."""
        assert deadline > time.monotonic()
        assert build_id == self.command["targetId"]
        assert any(row.get("claim") == claim for row in records(self.root))
        self.claims.append(copy.deepcopy(claim))
        return {
            "command": copy.deepcopy(self.command),
            "grant": {
                "schema": "hi/fleet/build-grant/v1",
                "claim": copy.deepcopy(claim),
                "grantId": digest({"buildId": build_id, "claim": claim}),
                "authorizedAt": "fixture-authorization",
            },
        }

    def publish_fact(
        self, build_id: str, fact: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        """Observe the retained fact before a controlled successful or lost reply."""
        assert deadline > time.monotonic()
        assert build_id == self.command["targetId"]
        assert any(row.get("terminal") == fact for row in records(self.root))
        self.observe_fact(fact)
        self.facts.append(copy.deepcopy(fact))
        if self.drop_reply:
            self.drop_reply = False
            raise ConnectionError("fixture lost terminal reply after acceptance")
        return {"eventId": fact["eventId"]}


class ObservedScheduler(ProcessScheduler):
    """Retain copies of actual local process observations for an independent oracle."""

    def __init__(self, state: Path, binding: dict[str, Any]) -> None:
        super().__init__(state, binding)
        self.observations: list[dict[str, Any]] = []

    def observe(self, identity: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Use the real fixture process and process-group probe, then record it."""
        value = super().observe(identity, deadline=deadline)
        self.observations.append(copy.deepcopy(value))
        return value


class ArbitratingController(PublicationController):
    """Model the immutable ef39 controller's cancellation order without HTTP."""

    def __init__(self, root: Path, command: dict[str, Any]) -> None:
        super().__init__(root, command)
        self.cancel: dict[str, Any] | None = None
        self.accepted: dict[str, Any] | None = None
        self.attempted: list[dict[str, Any]] = []
        self.before_fact: Any = lambda _: None
        self.stale_reply = False

    def cancel_build(self) -> tuple[int, dict[str, Any] | None]:
        """Reject cancellation after terminal acceptance, or retain one stop identity."""
        if self.accepted is not None:
            return 409, None
        if self.cancel is None:
            self.cancel = copy.deepcopy(self.command)
            self.cancel.update(
                operation="cancel", commandId="publisher-stop", idempotencyKey="publisher-stop"
            )
            self.cancel["payload"]["stopStartCommandId"] = self.command["commandId"]
        return 202, copy.deepcopy(self.cancel)

    def publish_fact(
        self, build_id: str, fact: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        """Validate the current command before exact retained terminal replay."""
        self.attempted.append(copy.deepcopy(fact))
        self.before_fact(fact)
        current = self.cancel or self.command
        payload = current["payload"]
        allocation = payload["policy"]["allocation"]
        expected = {
            "commandId": current["commandId"],
            "attempt": payload["attempt"],
            "workerId": allocation["workerId"],
            "allocationId": allocation["id"],
            "generation": allocation["generation"],
            "policyDigest": payload["policyDigest"],
            "parametersDigest": payload["parametersDigest"],
            "snapshotDigest": payload["snapshot"]["manifestDigest"],
        }
        if any(encoded(fact[name]) != encoded(value) for name, value in expected.items()):
            if self.stale_reply:
                # This injected bad acknowledgement is not a valid controller outcome.
                return {"eventId": fact["eventId"]}
            raise RuntimeError("fixture controller 409: current fact identity differs")
        if self.accepted is not None:
            assert encoded(self.accepted) == encoded(fact), "terminal replay body changed"
        elif self.cancel is not None:
            assert fact["outcome"] == "cancelled" and fact["startFenced"] is True
        else:
            assert self.claims and fact["outcome"] != "cancelled"
        self.accepted = copy.deepcopy(fact)
        return super().publish_fact(build_id, fact, deadline=deadline)


class PublicationExecutor:
    """Connect the actual owner to the supervisor without claiming real isolation."""

    def __init__(self, owner: SlurmStepOwner, root: Path) -> None:
        self.owner, self.root = owner, root

    def prepare(self, lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Require actual restored fixture bytes and an already persisted grant."""
        assert deadline > time.monotonic()
        assert any(row.get("grant") is not None for row in records(self.root))
        for name, data in contract()["sourceFiles"].items():
            assert (Path(lease["workspace"]) / name).read_bytes() == data.encode()
        return copy.deepcopy(lease)

    def run(self, lease: dict[str, Any], **options: Any) -> dict[str, Any]:
        """Use the actual gated owner and fixed Just recipe."""
        return self.owner.run(lease, **options)

    def dispose(self, lease: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Use actual owned disposal with both fixture observations."""
        return self.owner.dispose(lease, deadline=deadline)


class PublisherCase:
    """Own all source, journals, processes and publisher inputs for one test."""

    def __init__(self, root: Path, *, original: bool = False) -> None:
        self.root = root
        self.source = fresh_source(root)
        self.policy = SnapshotPolicy(20, 8192)
        self.command = copy.deepcopy(contract()["admission"]["command"])
        artifact = FIXTURE / "snapshot"
        if not original:
            artifact = root / "snapshot"
            self.command["payload"]["snapshot"] = export_snapshot(
                self.source, artifact, reference="publisher-source", policy=self.policy
            )
            policy = self.command["payload"]["policy"]
            build_id = "build-" + digest(
                {"workspaceId": policy["workspace"]["id"], "idempotencyKey": "publisher-source"}
            )
            start_id = build_id + "-start"
            self.command.update(
                targetId=build_id,
                commandId=start_id,
                idempotencyKey=start_id,
                workerId=policy["allocation"]["workerId"],
                generation=policy["allocation"]["generation"],
            )
            self.command["payload"]["snapshotWorkspace"] = (
                f"{build_id}-attempt-{self.command['payload']['attempt']}"
            )
        self.snapshot = BuildSnapshot(artifact, self.policy)
        self.evidence_root = root / "evidence"
        self.evidence_root.mkdir(mode=0o700)
        _, binding = inputs(root)
        self.scheduler = ObservedScheduler(root / "step-owner", binding)
        self.client = PublicationController(root / "state", self.command)
        self.open()

    def open(self) -> None:
        """Open existing owners after a restart without touching process state."""
        self.owner = SlurmStepOwner(
            state_dir=self.scheduler.state,
            binding=self.scheduler.binding,
            transport=self.scheduler,
        )
        self.publisher = PrivateBuildPublisher(
            evidence_root=self.evidence_root, snapshot=self.snapshot, owner=self.owner
        )
        self.service = BuildSupervisor(
            state_dir=self.root / "state",
            workspace_root=self.root / "workspaces",
            policy=self.command["payload"]["policy"],
            client=self.client,
            snapshot=self.snapshot,
            executor=PublicationExecutor(self.owner, self.root / "state"),
            publisher=self.publisher,
            claim_id_factory=lambda: "publisher-claim",
        )

    def close_owners(self) -> None:
        """Close both journals without treating closure as a disposal observation."""
        try:
            self.service.close()
        finally:
            self.owner.close()

    def close(self) -> None:
        """Reap every real fixture process even when an assertion fails."""
        try:
            self.close_owners()
        finally:
            self.scheduler.close()

    def state(self) -> dict[str, Any]:
        """Read the actual final supervisor journal record."""
        return records(self.root / "state")[-1]

    def collect(self, fact: dict[str, Any]) -> dict[str, Any]:
        """Use the actual collector against the publisher's private receipt."""
        payload = self.command["payload"]
        lease = self.state()["lease"]
        return collect_build_result(
            self.evidence_root,
            reference={"id": fact["receipt"]["reference"], "digest": fact["receipt"]["digest"]},
            expected={
                "buildId": self.command["targetId"],
                "attempt": payload["attempt"],
                "commandId": self.command["commandId"],
                "leaseId": lease["leaseId"],
                "parent": payload["parent"],
                "policy": payload["policy"],
                "policyDigest": payload["policyDigest"],
                "snapshot": payload["snapshot"],
            },
            source=self.source,
            snapshot_policy=self.policy,
            deadline=time.monotonic() + 5,
        )


@pytest.fixture
def published_case(tmp_path: Path) -> Iterator[PublisherCase]:
    """Keep exact owner cleanup on both missing-behavior and successful paths."""
    case = PublisherCase(tmp_path)
    try:
        yield case
    finally:
        case.close()


@requires_just
def test_actual_owned_recipe_and_disposal_remain_a_positive_control(
    published_case: PublisherCase,
) -> None:
    """Separate real execution controls from the new publisher assertions."""
    case = published_case
    case.service.handle(case.command)
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    output = json.loads((case.scheduler.state / "output" / "result.json").read_bytes())
    assert output == {
        "outcome": "completed",
        "exitCode": 0,
        "stdout": "fixture build\n",
        "stderr": "",
    }
    assert case.scheduler.observations[-1]["schedulerTerminal"] is True
    assert case.scheduler.observations[-1]["kernelEmpty"] is True
    assert all(child.poll() == 0 for child in case.scheduler.children.values())


@requires_just
def test_owner_retains_exact_observation_and_result_across_restart(
    published_case: PublisherCase,
) -> None:
    """Keep actual observations instead of reconstructing them from a cleanup flag."""
    case = published_case
    case.service.handle(case.command)
    lease = case.state()["lease"]
    value = case.owner.evidence(lease, deadline=time.monotonic() + 5)
    assert isinstance(value, dict), "owner does not expose its retained result evidence"
    assert value == {
        "schema": "hi/hephaestus/build-step-result/v1",
        "lease": lease,
        "cleanup": case.scheduler.observations[-1],
        "result": {
            "outcome": "completed",
            "exitCode": 0,
            "stdout": "fixture build\n",
            "stderr": "",
        },
    }
    case.close_owners()
    case.open()
    observed = len(case.scheduler.observations)
    assert case.owner.evidence(lease, deadline=time.monotonic() + 5) == value
    assert len(case.scheduler.observations) == observed
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
@pytest.mark.parametrize("observation", ["exact", "unknown"])
def test_old_summary_requires_new_observation_without_execution(
    published_case: PublisherCase, observation: str
) -> None:
    """Old v1 journal records have no evidence to promote into a step receipt."""
    case = published_case
    case.service.handle(case.command)
    lease = case.state()["lease"]
    case.close_owners()
    journal = case.scheduler.state / "receipts.jsonl"
    legacy = [json.loads(line) for line in journal.read_bytes().splitlines()]
    for row in legacy:
        row["value"]["schema"] = "hi/hephaestus/build-step-owner/v1"
        row["value"].pop("cleanupObservation", None)
    journal.write_bytes(b"".join(encoded(row) + b"\n" for row in legacy))
    case.open()
    with pytest.raises(RuntimeError):
        case.owner.evidence(lease, deadline=time.monotonic() + 5)
    case.scheduler.kernel_unknown = observation == "unknown"
    if observation == "unknown":
        with pytest.raises(RuntimeError):
            case.owner.dispose(lease, deadline=time.monotonic() + 5)
        with pytest.raises(RuntimeError):
            case.owner.evidence(lease, deadline=time.monotonic() + 5)
    else:
        case.owner.dispose(lease, deadline=time.monotonic() + 5)
        value = case.owner.evidence(lease, deadline=time.monotonic() + 5)
        assert isinstance(value, dict)
        assert value["cleanup"] == case.scheduler.observations[-1]
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
def test_actual_publisher_bundle_collects_current_then_historical(
    published_case: PublisherCase,
) -> None:
    """Exercise export, recipe, publication and collection in one coherent path."""
    case = published_case
    assert case.service.handle(case.command)["status"] == "completed"
    fact = case.client.facts[-1]
    assert isinstance(fact["receipt"], dict), "terminal fact has no publisher receipt"
    assert isinstance(fact["artifacts"], dict)
    result = case.collect(fact)
    assert result["status"] == "verified_current"
    assert result["sourceCurrent"] is True
    assert result["outcome"] == "completed" and result["exitCode"] == 0
    assert result["artifacts"] == []
    (case.source / "new-test.py").write_bytes(b"assert False\n")
    historical = case.collect(fact)
    assert historical["status"] == "historical"
    assert historical["sourceCurrent"] is False
    assert historical["reference"] == result["reference"]
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
def test_original_controller_snapshot_is_not_replaced_by_fresh_fixture(tmp_path: Path) -> None:
    """Retain exact original producer compatibility as a distinct historical case."""
    before = hashlib.sha256((FIXTURE / "controller.json").read_bytes()).hexdigest()
    case = PublisherCase(tmp_path, original=True)
    try:
        assert case.service.handle(case.command)["status"] == "completed"
        fact = case.client.facts[-1]
        assert isinstance(fact["receipt"], dict), "original receipt was not published"
        assert case.collect(fact)["status"] == "historical"
        assert case.command == contract()["admission"]["command"]
        assert hashlib.sha256((FIXTURE / "controller.json").read_bytes()).hexdigest() == before
    finally:
        case.close()


@requires_just
@pytest.mark.parametrize("changed", ["result", "manifest", "lease", "policy", "generation"])
def test_publisher_refuses_changed_owner_or_admission_bytes(
    published_case: PublisherCase, changed: str
) -> None:
    """Bind publication to actual immutable result, snapshot, lease and policy bytes."""
    case = published_case
    case.service.handle(case.command)
    state = case.state()
    command, lease = copy.deepcopy(case.command), copy.deepcopy(state["lease"])
    if changed == "result":
        path = case.scheduler.state / "output" / "result.json"
        path.write_bytes(path.read_bytes().replace(b"fixture build", b"changed build"))
    elif changed == "manifest":
        path = case.snapshot.artifact / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "lease":
        lease["leaseId"] = "f" * 32
    elif changed == "generation":
        lease["generation"] = float(lease["generation"])
    else:
        command["payload"]["policy"]["allocation"]["qualificationReceiptDigest"] = "a" * 64
    facts = len(case.client.facts)
    with pytest.raises((ValueError, RuntimeError)):
        case.publisher.publish(command, lease, state["terminal"], deadline=time.monotonic() + 5)
    assert len(case.client.facts) == facts
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
def test_references_and_bundle_are_synchronized_before_fact(
    published_case: PublisherCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe real file and directory synchronization at actual publication."""
    case = published_case
    actual_fsync = os.fsync
    synced: set[tuple[int, int]] = set()
    observed: list[dict[str, Any]] = []

    def sync(descriptor: int) -> None:
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        synced.add((metadata.st_dev, metadata.st_ino))

    def before_fact(fact: dict[str, Any]) -> None:
        assert isinstance(fact["receipt"], dict), "fact arrived before its private receipt"
        bundle = case.evidence_root / fact["receipt"]["reference"]
        paths = [case.evidence_root, bundle, *bundle.iterdir()]
        assert {"receipt.json", "manifest.json", "stdout.txt", "stderr.txt"} == {
            path.name for path in bundle.iterdir()
        }
        for path in paths:
            metadata = path.stat()
            assert (metadata.st_dev, metadata.st_ino) in synced
            assert metadata.st_mode & 0o077 == 0
        raw = (bundle / "receipt.json").read_bytes()
        receipt = json.loads(raw)
        assert hashlib.sha256(raw).hexdigest() == fact["receipt"]["digest"]
        assert receipt["cleanup"] == case.scheduler.observations[-1]
        assert (bundle / "stdout.txt").read_bytes() == b"fixture build\n"
        assert (bundle / "stderr.txt").read_bytes() == b""
        assert (bundle / "manifest.json").read_bytes() == (
            case.snapshot.artifact / "manifest.json"
        ).read_bytes()
        observed.append(copy.deepcopy(fact))

    monkeypatch.setattr(os, "fsync", sync)
    case.client.observe_fact = before_fact
    assert case.service.handle(case.command)["status"] == "completed"
    assert len(observed) == len(case.client.facts) == 1


@requires_just
@pytest.mark.parametrize("failure", ["publisher", "controller"])
def test_failed_publication_retries_same_bundle_without_another_process(
    published_case: PublisherCase, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Retain terminal result and exact private references across both lost replies."""
    case = published_case
    original_publish = case.publisher.publish
    retained: list[dict[str, Any]] = []

    def publish(*arguments: Any, **options: Any) -> dict[str, Any] | None:
        value = original_publish(*arguments, **options)
        assert isinstance(value, dict), "publisher produced no retained references"
        retained.append(copy.deepcopy(value))
        raise ConnectionError("fixture lost publisher reply after its private write")

    if failure == "publisher":
        monkeypatch.setattr(case.publisher, "publish", publish)
    else:
        case.client.drop_reply = True
    assert case.service.handle(case.command)["status"] == "reconciliation_required"
    first = copy.deepcopy(case.state()["terminal"])
    assert first is not None
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    case.close_owners()
    case.open()
    assert case.service.handle(case.command)["status"] == "completed"
    fact = case.client.facts[-1]
    assert isinstance(fact["receipt"], dict)
    assert fact["eventId"] == first["eventId"]
    if failure == "publisher":
        assert len(retained) == 1
        assert fact["receipt"] == retained[0]["receipt"]
    else:
        assert case.client.facts[0] == case.client.facts[1]
    assert case.collect(fact)["status"] == "verified_current"
    assert (
        len(case.client.claims) == len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    )


@requires_just
@pytest.mark.parametrize("change", ["bytes", "symlink", "hardlink", "public"])
def test_changed_bundle_refuses_terminal_retry(published_case: PublisherCase, change: str) -> None:
    """Re-read private bytes after a lost reply before resending the retained fact."""
    case = published_case
    case.client.drop_reply = True
    assert case.service.handle(case.command)["status"] == "reconciliation_required"
    fact = case.client.facts[-1]
    assert isinstance(fact["receipt"], dict), "no exact publisher bundle was retained"
    target = case.evidence_root / fact["receipt"]["reference"] / "stdout.txt"
    if change == "bytes":
        target.write_bytes(b"changed private output\n")
    elif change == "public":
        target.chmod(0o644)
    else:
        outside = case.root / "borrowed-output"
        outside.write_bytes(target.read_bytes())
        outside.chmod(0o600)
        target.unlink()
        if change == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    case.close_owners()
    case.open()
    assert case.service.handle(case.command)["status"] == "reconciliation_required"
    assert len(case.client.facts) == len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
@pytest.mark.parametrize("change", ["step", "boolean", "missing"])
def test_retained_cleanup_observation_cannot_change_identity(
    published_case: PublisherCase, change: str
) -> None:
    """Versioned evidence must retain actual exact identity and typed observations."""
    case = published_case
    case.service.handle(case.command)
    case.close_owners()
    journal = case.scheduler.state / "receipts.jsonl"
    rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
    value = rows[-1]["value"]
    assert value["schema"] == "hi/hephaestus/build-step-owner/v2", (
        "evidence shape was not versioned"
    )
    assert isinstance(value.get("cleanupObservation"), dict), "actual observation was not retained"
    if change == "step":
        value["cleanupObservation"]["step"]["stepStart"] = "another-step-incarnation"
    elif change == "boolean":
        value["cleanupObservation"]["kernelEmpty"] = 1
    else:
        value.pop("cleanupObservation")
    journal.write_bytes(b"".join(encoded(row) + b"\n" for row in rows))
    with pytest.raises((ValueError, RuntimeError)):
        unexpected = SlurmStepOwner(
            state_dir=case.scheduler.state,
            binding=case.scheduler.binding,
            transport=case.scheduler,
        )
        unexpected.close()
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1


@requires_just
def test_cancel_before_start_has_no_fabricated_step_receipt(published_case: PublisherCase) -> None:
    """Preserve the existing no-start fence and distinguish absent execution evidence."""
    case = published_case
    cancel = copy.deepcopy(case.command)
    cancel.update(operation="cancel", commandId="publisher-stop", idempotencyKey="publisher-stop")
    cancel["payload"]["stopStartCommandId"] = case.command["commandId"]
    result = case.service.handle(cancel)
    assert result["status"] == "cancelled"
    assert result["collectionVerified"] is False
    assert case.client.facts[-1]["receipt"] is None
    assert case.client.facts[-1]["artifacts"] is None
    assert case.client.facts[-1]["startFenced"] is True
    assert not case.client.claims
    assert not case.scheduler.starts and not case.scheduler.releases
    assert not case.scheduler.observations


def use_arbitrating_controller(case: PublisherCase) -> ArbitratingController:
    """Inject only the controlled controller while keeping the real local owners."""
    client = ArbitratingController(case.root / "state", case.command)
    case.client = client
    case.service.client = client
    return client


def deliver_cancel(case: PublisherCase, client: ArbitratingController) -> dict[str, Any]:
    """Deliver exactly the stop which the controlled controller accepted."""
    status, cancel = client.cancel_build()
    assert status == 202 and cancel is not None
    case.service.handle(cancel)
    return cancel


def assert_completed_owner_is_private(case: PublisherCase) -> dict[str, Any]:
    """Require actual completed output and cleanup without changing its outcome."""
    assert case.state()["cleanup"] == "confirmed_empty"
    evidence = case.owner.evidence(case.state()["lease"], deadline=time.monotonic() + 5)
    assert isinstance(evidence, dict)
    assert evidence["result"] == {
        "outcome": "completed",
        "exitCode": 0,
        "stdout": "fixture build\n",
        "stderr": "",
    }
    assert evidence["cleanup"] == case.scheduler.observations[-1]
    assert evidence["cleanup"]["schedulerTerminal"] is True
    assert evidence["cleanup"]["kernelEmpty"] is True
    assert len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    assert all(child.poll() == 0 for child in case.scheduler.children.values())
    return evidence


@requires_just
def test_cancel_after_run_replays_incomplete_fact_without_changing_owner_output(
    published_case: PublisherCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep completed output private when cancellation wins before terminal retention."""
    case = published_case
    client = use_arbitrating_controller(case)
    run = case.service.executor.run
    stops: list[dict[str, Any]] = []

    def finish_then_cancel(*arguments: Any, **options: Any) -> dict[str, Any]:
        result = run(*arguments, **options)
        assert result["outcome"] == "completed" and result["exitCode"] == 0
        assert case.state()["terminal"] is None
        stops.append(deliver_cancel(case, client))
        return result

    monkeypatch.setattr(case.service.executor, "run", finish_then_cancel)
    client.drop_reply = True
    first = case.service.handle(case.command)
    assert first["status"] == "reconciliation_required"
    evidence = assert_completed_owner_is_private(case)
    assert len(stops) == 1
    assert len(client.facts) == 1, "confirmed cancellation fact was not delivered"
    fact = copy.deepcopy(client.facts[0])
    assert fact["commandId"] == stops[0]["commandId"]
    assert fact["outcome"] == "cancelled" and fact["exitCode"] is None
    assert fact["receipt"] is fact["logs"] is fact["artifacts"] is None
    assert fact["startFenced"] is True and first["collectionVerified"] is False
    assert not list(case.evidence_root.iterdir())
    case.close_owners()
    case.open()
    result = case.service.handle(stops[0])
    assert result["status"] == "cancelled" and result["collectionVerified"] is False
    assert client.facts == [fact, fact]
    assert assert_completed_owner_is_private(case) == evidence
    assert len(client.claims) == 1 and not list(case.evidence_root.iterdir())


def assert_reconciled_cancellation(
    case: PublisherCase, client: ArbitratingController, prior: dict[str, Any]
) -> dict[str, Any]:
    """Require separate durable history and an incomplete stop-bound terminal fact."""
    state = case.state()
    assert state["schema"] == "hi/hephaestus/build-attempt/v2"
    assert encoded(state["supersededTerminal"]) == encoded(prior)
    assert any(encoded(row.get("terminal")) == encoded(prior) for row in records(client.root))
    fact = state["terminal"]
    assert client.cancel is not None and fact["commandId"] == client.cancel["commandId"]
    assert fact["eventId"] != prior["eventId"]
    assert fact["outcome"] == "cancelled" and fact["exitCode"] is None
    assert fact["receipt"] is fact["logs"] is fact["artifacts"] is None
    assert fact["cleanup"] == "confirmed_empty" and fact["startFenced"] is True
    assert client.accepted == fact and case.service.status()["collectionVerified"] is False
    assert len(client.claims) == len(case.scheduler.starts) == len(case.scheduler.releases) == 1
    return fact


@requires_just
@pytest.mark.parametrize("reply", ["lost", "returned"])
def test_cancel_during_publisher_uncertainty_preserves_history_and_delivers_stop(
    published_case: PublisherCase, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """Keep old terminal bytes as history and reconcile the accepted stop on replay."""
    case = published_case
    client = use_arbitrating_controller(case)
    publish = case.publisher.publish
    retained: list[dict[str, Any]] = []

    def publish_then_cancel(*arguments: Any, **options: Any) -> dict[str, Any]:
        references = publish(*arguments, **options)
        assert isinstance(references, dict)
        retained.append(copy.deepcopy(case.state()["terminal"]))
        deliver_cancel(case, client)
        if reply == "returned":
            return references
        raise ConnectionError("fixture lost publisher reply after accepted cancellation")

    monkeypatch.setattr(case.publisher, "publish", publish_then_cancel)
    result = case.service.handle(case.command)
    assert result["status"] in ("reconciliation_required", "cancelled")
    evidence = assert_completed_owner_is_private(case)
    assert len(retained) == 1
    assert any(row.get("terminal") == retained[0] for row in records(client.root))
    assert retained[0]["receipt"] is None
    assert all(fact["outcome"] == "cancelled" for fact in client.attempted)
    bundle = case.evidence_root / ("result-" + case.state()["lease"]["leaseId"])
    before = {path.name: path.read_bytes() for path in bundle.iterdir()}
    assert json.loads(before["receipt.json"])["outcome"] == "completed"
    case.close_owners()
    case.open()
    assert client.cancel is not None
    result = case.service.handle(client.cancel)
    assert result["status"] == "cancelled", "accepted cancellation did not reconcile"
    fact = assert_reconciled_cancellation(case, client, retained[0])
    assert client.attempted == client.facts == [fact]
    assert {path.name: path.read_bytes() for path in bundle.iterdir()} == before
    assert assert_completed_owner_is_private(case) == evidence


@requires_just
@pytest.mark.parametrize("winner", ["cancel", "terminal", "cancel_stale_ack"])
def test_cancel_during_controller_uncertainty_preserves_exact_fact(
    published_case: PublisherCase, winner: str
) -> None:
    """Respect controller order without replacing a retained event or execution receipt."""
    case = published_case
    client = use_arbitrating_controller(case)

    def cancel_before_acceptance(_: dict[str, Any]) -> None:
        if client.cancel is None:
            deliver_cancel(case, client)

    if winner != "terminal":
        client.before_fact = cancel_before_acceptance
        client.stale_reply = winner == "cancel_stale_ack"
    else:
        client.drop_reply = True
    result = case.service.handle(case.command)
    assert result["status"] in ("reconciliation_required", "cancelled")
    evidence = assert_completed_owner_is_private(case)
    assert len(client.attempted) == 1
    fact = copy.deepcopy(client.attempted[0])
    assert any(row.get("terminal") == fact for row in records(client.root))
    assert fact["outcome"] == "completed" and isinstance(fact["receipt"], dict)
    if winner == "terminal":
        assert client.accepted == fact
        assert client.cancel_build() == (409, None)
    else:
        assert client.accepted is None and not client.facts
        assert case.state()["published"] is False, "old acknowledgement marked stop published"
    case.close_owners()
    case.open()
    result = case.service.handle(client.cancel or case.command)
    if winner == "terminal":
        assert result["status"] == "completed"
        assert encoded(case.state()["terminal"]) == encoded(fact)
        assert client.facts == client.attempted == [fact, fact]
    else:
        assert result["status"] == "cancelled", "controller-accepted stop did not reconcile"
        cancelled = assert_reconciled_cancellation(case, client, fact)
        assert client.attempted == [fact, cancelled] and client.facts == [cancelled]
    assert assert_completed_owner_is_private(case) == evidence


@requires_just
@pytest.mark.parametrize("change", ["identity", "body", "missing", "downgrade", "stop"])
def test_cancelled_history_refuses_damaged_state_or_transition(
    published_case: PublisherCase, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Reject changed history and stop identity before a restarted owner makes effects."""
    case = published_case
    client = use_arbitrating_controller(case)
    publish = case.publisher.publish

    def lose_reply_after_stop(*arguments: Any, **options: Any) -> dict[str, Any]:
        publish(*arguments, **options)
        deliver_cancel(case, client)
        raise ConnectionError("fixture lost publication reply")

    monkeypatch.setattr(case.publisher, "publish", lose_reply_after_stop)
    case.service.handle(case.command)
    case.close_owners()
    case.open()
    assert client.cancel is not None
    assert case.service.handle(client.cancel)["status"] == "cancelled"
    assert case.state()["schema"] == "hi/hephaestus/build-attempt/v2"
    case.close_owners()
    journal = case.root / "state" / "receipts.jsonl"
    rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
    state = rows[-1]["value"]
    if change == "identity":
        state["supersededTerminal"]["snapshotDigest"] = "a" * 64
    elif change == "body":
        state["supersededTerminal"]["logs"]["digest"] = "a" * 64
    elif change == "stop":
        state["cancel"].update(commandId="different-stop", idempotencyKey="different-stop")
        state["terminal"]["commandId"] = "different-stop"
        state["terminal"]["eventId"] = "build-" + digest(
            {"claim": state["claim"], "commandId": "different-stop", "outcome": "cancelled"}
        )
    else:
        state.pop("supersededTerminal")
        if change == "downgrade":
            state["schema"] = "hi/hephaestus/build-attempt/v1"
    journal.write_bytes(b"".join(encoded(row) + b"\n" for row in rows))
    effects = (len(client.claims), len(client.attempted), len(case.scheduler.starts))
    with pytest.raises((ValueError, RuntimeError)):
        case.open()
    assert (len(client.claims), len(client.attempted), len(case.scheduler.starts)) == effects
