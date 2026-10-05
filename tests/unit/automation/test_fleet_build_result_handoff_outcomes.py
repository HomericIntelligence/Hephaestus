"""Preserve real failed exits and refuse superseded completed result authority.

Admission, allocation and controller arbitration are controlled fixtures. The
recipe processes, snapshots, journals, publication, SDK HTTP and collector are
real. The fixture prepare check and source lease do not qualify production
isolation, Slurm or Pyxis.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_contract import digest
from hephaestus.automation.fleet_build_executor import SlurmStepOwner
from hephaestus.automation.fleet_build_jobs import FleetBuildJobContext, FleetBuildJobRunner
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot
from hephaestus.automation.fleet_snapshot import export_snapshot
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.utils.file_lock import file_lock
from tests.unit.automation.test_fleet_build_job_fixture import (
    JobBuildCase,
    job_build_case as job_build_case,
)
from tests.unit.automation.test_fleet_build_publication import (
    PublicationExecutor,
    assert_completed_owner_is_private,
    assert_reconciled_cancellation,
    deliver_cancel,
    use_arbitrating_controller,
)
from tests.unit.automation.test_fleet_build_result_handoff import (
    _expected_from_state,
    _published_bytes,
    _root_is_held,
)
from tests.unit.automation.test_fleet_build_result_handoff_recovery import _handoff
from tests.unit.automation.test_fleet_build_supervisor import records

pytestmark = pytest.mark.precommit
type Json = dict[str, Any]
_FAILED_RECIPE = b"test-unit:\n    @printf 'fixture failed build\\n'\n    @exit 7\n"


class FailedRecipeExecutor(PublicationExecutor):
    """Check the actual replacement source before using the unchanged real step owner."""

    def __init__(self, owner: SlurmStepOwner, root: Path, source_files: dict[str, bytes]) -> None:
        """Retain expected bytes from the fresh source, independent of restored output."""
        super().__init__(owner, root)
        self.source_files = dict(source_files)
        self.prepared: list[Json] = []

    def prepare(self, lease: Json, *, deadline: float) -> Json:
        """Require a durable grant and the new snapshot's exact restored bytes."""
        assert deadline > time.monotonic()
        assert any(row.get("grant") is not None for row in records(self.root))
        workspace = Path(lease["workspace"])
        assert workspace.is_absolute() and workspace.resolve(strict=True) == workspace
        for name, expected in self.source_files.items():
            assert (workspace / name).read_bytes() == expected
        self.prepared.append(copy.deepcopy(lease))
        return copy.deepcopy(lease)


def _use_failed_recipe(case: JobBuildCase, monkeypatch: pytest.MonkeyPatch) -> FailedRecipeExecutor:
    """Bind a fresh real snapshot and reopen the unused consumer before any admission."""
    publisher, http = case.publisher, case.http
    assert http is not None and http.requests == []
    old_client, old_journal = case.client, case.journal
    case.call(case._close_consumer())
    assert old_client._client.is_closed and old_journal.snapshot()["closed"]
    publisher.close_owners()
    (publisher.source / "justfile").write_bytes(_FAILED_RECIPE)
    source_files = {
        name: (publisher.source / name).read_bytes() for name in ("justfile", "uv.lock")
    }
    payload = publisher.command["payload"]
    prior_snapshot = copy.deepcopy(payload["snapshot"])
    recipe = payload["policy"]["recipe"]
    recipe["recipeDigest"] = hashlib.sha256(source_files["justfile"]).hexdigest()
    recipe["lockDigest"] = hashlib.sha256(source_files["uv.lock"]).hexdigest()
    payload["policyDigest"] = digest(payload["policy"])
    artifact = publisher.root / "failed-snapshot"
    payload["snapshot"] = export_snapshot(
        publisher.source, artifact, reference="publisher-source", policy=publisher.policy
    )
    assert payload["snapshot"]["manifestDigest"] != prior_snapshot["manifestDigest"]
    publisher.snapshot = BuildSnapshot(artifact, publisher.policy)
    publisher.open()
    executor = FailedRecipeExecutor(publisher.owner, publisher.root / "state", source_files)
    monkeypatch.setattr(publisher.service, "executor", executor)
    case._bind_http()
    case.call(case._open_consumer())
    assert case.loop_thread.is_alive() and case.http is http and http.requests == []
    return executor


def _owner_effects(case: JobBuildCase) -> Json:
    """Read actual execution and publication history without assuming a successful exit."""
    publisher = case.publisher
    scheduler = publisher.scheduler
    return {
        "history": _published_bytes(publisher.root / "state"),
        "step": _published_bytes(scheduler.state),
        "bundle": _published_bytes(publisher.evidence_root),
        "claims": copy.deepcopy(publisher.client.claims),
        "facts": copy.deepcopy(publisher.client.facts),
        "starts": list(scheduler.starts),
        "releases": list(scheduler.releases),
        "cancels": copy.deepcopy(scheduler.cancels),
        "observations": copy.deepcopy(scheduler.observations),
    }


def _observe_failed_fact(case: JobBuildCase, fact: Json) -> Json:
    """Expose only the actual accepted failed fact through the real SDK transport."""
    http = case.http
    assert http is not None and case.publisher.client.facts == [fact]
    assert fact["outcome"] == "failed"
    http.record["status"] = fact["outcome"]
    http.record["build"]["terminal"] = copy.deepcopy(fact)
    record = case.call(case.owner.status(deadline=time.monotonic() + 5))
    assert record["build"]["terminal"] == fact and record["collectionVerified"] is False
    # Idempotent submit replay returns the current accepted record, not the initial state.
    http.data["admission"]["record"] = copy.deepcopy(record)
    return record


def _collect_failed(case: JobBuildCase, record: Json, state: Json) -> Json:
    """Collect the real failed bundle under the public supervisor's root lease."""
    reference, expected = _expected_from_state(state)
    deadline = time.monotonic() + 5
    with (
        file_lock(case.root / "source-access.lock", blocking=False, require_exclusive=True),
        _handoff(case, record, deadline=deadline, shutdown=threading.Event()) as evidence,
    ):
        assert evidence.reference == reference and evidence.expected == expected
        assert _root_is_held(evidence.evidence_root)
        result = collect_build_result(
            evidence.evidence_root,
            reference=evidence.reference,
            expected=evidence.expected,
            source=case.publisher.source,
            snapshot_policy=case.publisher.policy,
            deadline=deadline,
        )
    assert not _root_is_held(case.publisher.evidence_root)
    return result


def _refuse_success_in_actual_runner(case: JobBuildCase, collected: Json) -> None:
    """Let the real job adapter collect again and require its unsuccessful result."""
    command = case.publisher.command
    repository = command["payload"]["policy"]["workspace"]["repository"]

    @contextmanager
    def source_lease(*, deadline: float, shutdown: threading.Event) -> Iterator[None]:
        """Retain actual fixture source exclusion within the caller's budget."""
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
        timeout_s=8,
        expected_head_sha=case.submission["snapshot"]["baseCommit"],
        descr="fixture_failed_recipe",
        fleet_context_id="job-context",
    )
    with ExitStack() as resources:
        result = runner.run(
            job, deadline=time.monotonic() + 8, shutdown=threading.Event(), resources=resources
        )
        assert result.value == collected
        assert result.ok is False and result.interrupted is False
        assert result.error == "fleet_build_not_current_success"
        assert _root_is_held(case.publisher.evidence_root)
    assert not _root_is_held(case.publisher.evidence_root)


def test_actual_failed_recipe_handoff_preserves_exit_and_cannot_promote_success(
    job_build_case: JobBuildCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use newly bound real recipe bytes and preserve the actual nonzero child exit."""
    case = job_build_case
    executor = _use_failed_recipe(case, monkeypatch)
    admission = case.call(case.owner.submit(deadline=time.monotonic() + 5))
    assert admission["build"]["request"] == case.submission
    assert case.publisher.service.handle(case.publisher.command)["status"] == "failed"
    state = case.publisher.state()
    assert state["published"] is True and state["reason"] == "" and state["cancel"] is None
    assert state["cleanup"] == "confirmed_empty"
    scheduler = case.publisher.scheduler
    assert len(scheduler.starts) == len(scheduler.releases) == len(executor.prepared) == 1
    child = scheduler.children[scheduler.starts[0]]
    exit_code = child.poll()
    assert type(exit_code) is int and exit_code != 0
    fact = copy.deepcopy(state["terminal"])
    assert fact["outcome"] == "failed" and fact["exitCode"] == exit_code
    owned = case.publisher.owner.evidence(state["lease"], deadline=time.monotonic() + 5)
    assert owned["result"]["stdout"] == "fixture failed build\n"
    assert owned["result"]["outcome"] == "failed" and owned["result"]["exitCode"] == exit_code
    assert owned["cleanup"] == scheduler.observations[-1]
    assert owned["cleanup"]["schedulerTerminal"] is True
    assert owned["cleanup"]["kernelEmpty"] is True
    record = _observe_failed_fact(case, fact)
    before = _owner_effects(case)
    collected = _collect_failed(case, record, state)
    assert collected["status"] == "verified_current" and collected["sourceCurrent"] is True
    assert collected["outcome"] == "failed" and collected["exitCode"] == exit_code
    _refuse_success_in_actual_runner(case, collected)
    assert _owner_effects(case) == before
    assert case.publisher.service.status()["collectionVerified"] is False
    http = case.http
    assert http is not None
    submissions = [
        (request["path"], request["body"])
        for request in http.requests
        if request["method"] == "POST"
    ]
    assert submissions == [("/v1/fleet/build-jobs/submit", case.submission)] * 2
    assert not any(request["path"].endswith("/cancel") for request in http.requests)


def test_accepted_cancellation_keeps_superseded_bundle_but_refuses_its_public_handoff(
    job_build_case: JobBuildCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep actual completed bytes private when cancellation wins before acknowledgment."""
    case = job_build_case
    publisher = case.publisher
    client = use_arbitrating_controller(publisher)
    admission = case.call(case.owner.submit(deadline=time.monotonic() + 5))
    prior_facts: list[Json] = []

    def cancel_before_acceptance(fact: Json) -> None:
        """Deliver the controller's actual accepted stop after private result publication."""
        if client.cancel is None:
            assert publisher.state()["terminal"] == fact
            assert fact["outcome"] == "completed" and isinstance(fact["receipt"], dict)
            prior_facts.append(copy.deepcopy(fact))
            deliver_cancel(publisher, client)

    monkeypatch.setattr(client, "before_fact", cancel_before_acceptance)
    result = publisher.service.handle(publisher.command)
    assert result["status"] in ("reconciliation_required", "cancelled")
    assert len(prior_facts) == 1
    prior = prior_facts[0]
    assert client.attempted == [prior] and client.accepted is None and client.facts == []
    assert publisher.state()["published"] is False
    owned = assert_completed_owner_is_private(publisher)
    private_result = publisher.collect(prior)
    assert private_result["outcome"] == "completed" and private_result["exitCode"] == 0
    bundle = _published_bytes(publisher.evidence_root)
    publisher.close_owners()
    publisher.open()
    assert client.cancel is not None
    assert publisher.service.handle(client.cancel)["status"] == "cancelled"
    cancelled = assert_reconciled_cancellation(publisher, client, prior)
    assert publisher.state()["published"] is True
    assert client.attempted == [prior, cancelled] and client.facts == [cancelled]
    assert assert_completed_owner_is_private(publisher) == owned
    assert _published_bytes(publisher.evidence_root) == bundle

    # Present actual obsolete bytes as stale input; the controller never accepted this fact.
    obsolete = copy.deepcopy(admission)
    obsolete["status"] = prior["outcome"]
    obsolete["build"]["terminal"] = copy.deepcopy(prior)
    before = _owner_effects(case)
    with (
        pytest.raises(RuntimeError),
        _handoff(case, obsolete, deadline=time.monotonic() + 5, shutdown=threading.Event()),
    ):
        pytest.fail("a superseded completed receipt supplied a public result handoff")
    assert _owner_effects(case) == before
    assert not _root_is_held(publisher.evidence_root)
    assert publisher.collect(prior) == private_result
    assert publisher.service.status()["collectionVerified"] is False
