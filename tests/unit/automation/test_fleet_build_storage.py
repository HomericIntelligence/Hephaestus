"""Observe real storage ordering and failed-owner resource release.

These are local syscall and controlled-process tests, not power-loss or Slurm
experiments. Controller grants and isolation metadata remain explicit fixtures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import fleet_build_executor
from hephaestus.automation.fleet_build_contract import encoded
from hephaestus.automation.fleet_build_supervisor import BuildSnapshot, BuildSupervisor
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_snapshot import SnapshotPolicy
from tests.unit.automation.test_fleet_build_executor import ProcessScheduler, inputs
from tests.unit.automation.test_fleet_build_supervisor import (
    FIXTURE,
    Controller,
    FixtureExecutor,
    contract,
    make_service,
)


@pytest.mark.parametrize(
    "required_sync", ["state-names", "workspace-names", "claim-file", "workspace-binding-file"]
)
def test_supervisor_syncs_nested_storage_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required_sync: str
) -> None:
    """Bind every created directory edge and file sync to the first grant request."""
    state = tmp_path / "journal-parent" / "state"
    workspace = tmp_path / "workspace-parent" / "workspaces"
    journal = state / "receipts.jsonl"
    binding = workspace / ".build-owner"
    observed: list[dict[str, Any]] = []
    at_claim: list[dict[str, Any]] = []
    real_fsync = os.fsync

    def observe_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        entries = os.listdir(descriptor) if stat.S_ISDIR(metadata.st_mode) else []
        content = b""
        for path in (journal, binding):
            if path.exists():
                expected = path.stat()
                if identity == (expected.st_dev, expected.st_ino):
                    content = os.pread(descriptor, metadata.st_size, 0)
        observed.append({"identity": identity, "entries": entries, "content": content})

    client = Controller(state)
    executor = FixtureExecutor(state)
    actual_claim = client.claim_run

    def observe_claim(build_id: str, claim: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        at_claim.extend(copy.deepcopy(observed))
        return actual_claim(build_id, claim, deadline=deadline)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(client, "claim_run", observe_claim)
    with BuildSupervisor(
        state_dir=state,
        workspace_root=workspace,
        policy=contract()["admission"]["command"]["payload"]["policy"],
        client=client,
        snapshot=BuildSnapshot(FIXTURE / "snapshot", SnapshotPolicy(20, 8192)),
        executor=executor,
        claim_id_factory=lambda: contract()["claimRequest"]["claimId"],
    ) as service:
        result = service.handle(contract()["admission"]["command"])
        assert result["status"] == "completed" and result["collectionVerified"] is False
        assert len(client.claims) == len(executor.started) == len(executor.disposed) == 1

    if required_sync in ("state-names", "workspace-names"):
        leaf = state if required_sync == "state-names" else workspace
        names = {"writer.lock", "receipts.jsonl"} if leaf == state else {".build-owner"}
        edges = [(tmp_path, {leaf.parent.name}), (leaf.parent, {leaf.name}), (leaf, names)]
        missing = []
        for directory, entries in edges:
            metadata = directory.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            if not any(
                call["identity"] == identity and entries <= set(call["entries"])
                for call in at_claim
            ):
                missing.append(str(directory.relative_to(tmp_path)))
        assert not missing, f"directory names were not synchronized before claim_run: {missing}"
    else:
        path = journal if required_sync == "claim-file" else binding
        metadata = path.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        contents = [call["content"] for call in at_claim if call["identity"] == identity]
        if required_sync == "claim-file":
            assert any(
                json.loads(line)["value"].get("claim") == client.claims[0]
                for content in contents
                for line in content.splitlines()
            ), "the exact claim was not synchronized before claim_run"
        else:
            assert (
                encoded({"schema": "hi/hephaestus/build-owner/v1", "stateDir": str(state)})
                in contents
            ), "workspace binding bytes were not synchronized before claim_run"


def _writer_available(path: Path) -> bool:
    """Acquire and release a real replacement journal writer."""
    try:
        replacement = WorkerJournal(path)
    except RuntimeError:
        return False
    replacement.close()
    return True


@pytest.mark.parametrize("failure", ["directory-fsync", "directory-exit"])
def test_failed_step_owner_initialization_releases_actual_journal_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Retain real journal objects to require explicit file and lock release."""
    _, binding = inputs(tmp_path)
    state = tmp_path / "owner"
    scheduler = ProcessScheduler(state, binding)
    opened: list[WorkerJournal] = []
    owner = None
    failed = False
    injected = False
    actual_fsync, actual_stat = os.fsync, os.stat

    def open_journal(path: Path) -> WorkerJournal:
        journal = WorkerJournal(path)
        opened.append(journal)
        return journal

    def fail_fsync(descriptor: int) -> None:
        nonlocal injected
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        if opened:
            expected = state.stat()
            if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
                injected = True
                raise OSError("controlled directory synchronization failure")

    def fail_directory_exit(*args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal injected
        result = actual_stat(*args, **kwargs)
        if opened and not injected and args[0] == state.name and kwargs.get("dir_fd") is not None:
            injected = True
            raise OSError("controlled descriptor-context exit failure")
        return result

    try:
        with monkeypatch.context() as changes:
            changes.setattr(fleet_build_executor, "WorkerJournal", open_journal)
            if failure == "directory-fsync":
                changes.setattr(os, "fsync", fail_fsync)
            else:
                changes.setattr(os, "stat", fail_directory_exit)
            try:
                owner = fleet_build_executor.SlurmStepOwner(
                    state_dir=state, binding=binding, transport=scheduler
                )
            except OSError:
                failed = True
        assert scheduler.starts == scheduler.releases == scheduler.cancels == []
        assert opened, "the fixture never obtained a real journal writer"
        assert injected and failed, "owner initialization did not propagate the storage failure"
        assert _writer_available(state), "failed constructor retained the journal writer lock"
    finally:
        if owner is not None:
            owner.close()
        for journal in opened:
            journal.close()
        scheduler.close()


@pytest.mark.parametrize("mode", ["output-file", "output-name", "directory-failure"])
def test_output_storage_is_synchronized_before_terminal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Observe real output synchronization before a terminal reference can escape."""
    state = tmp_path / "state"
    output = state / "output.json"
    calls: list[dict[str, Any]] = []
    at_publication: list[dict[str, Any]] = []
    injected = False
    actual_fsync = os.fsync

    def observe_fsync(descriptor: int) -> None:
        nonlocal injected
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        entries = os.listdir(descriptor) if stat.S_ISDIR(metadata.st_mode) else []
        calls.append({"identity": identity, "entries": entries, "size": metadata.st_size})
        if mode == "directory-failure" and output.exists():
            expected = state.stat()
            if identity == (expected.st_dev, expected.st_ino):
                injected = True
                raise OSError("controlled output-directory synchronization failure")

    client = Controller(state)
    executor = FixtureExecutor(state)
    actual_publish = client.publish_fact

    def observe_publish(build_id: str, fact: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        at_publication.extend(copy.deepcopy(calls))
        return actual_publish(build_id, fact, deadline=deadline)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(client, "publish_fact", observe_publish)
    with make_service(tmp_path, client, executor) as service:
        result = service.handle(contract()["admission"]["command"])
        actual_output = json.loads(output.read_bytes())
        assert actual_output == {"stdout": "controlled child\n", "stderr": ""}
        assert len(executor.started) == len(executor.disposed) == 1
        if mode == "directory-failure":
            replay = service.handle(contract()["admission"]["command"])
            assert {
                "injected": injected,
                "status": result["status"],
                "replay": replay["status"],
                "facts": len(client.facts),
                "starts": len(executor.started),
            } == {
                "injected": True,
                "status": "reconciliation_required",
                "replay": "reconciliation_required",
                "facts": 0,
                "starts": 1,
            }
        else:
            assert result["status"] == "completed" and len(client.facts) == 1
            assert (
                client.facts[0]["logs"]["digest"]
                == hashlib.sha256(encoded(actual_output)).hexdigest()
            )
            path = output if mode == "output-file" else state
            metadata = path.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            matching = [call for call in at_publication if call["identity"] == identity]
            if mode == "output-file":
                completed = any(call["size"] == metadata.st_size for call in matching)
            else:
                completed = any(output.name in call["entries"] for call in matching)
            assert completed, f"{mode} was not synchronized before publish_fact"


def test_retained_workspace_binding_is_resynchronized_before_grant_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry a reported binding-sync failure using the same bytes and one child."""
    state = tmp_path / "state"
    binding = tmp_path / "workspaces" / ".build-owner"
    client = Controller(state)
    executor = FixtureExecutor(state)
    actual_fsync = os.fsync
    interrupted = False
    synced = False
    before_claim: list[bool] = []

    def fail_binding_sync(descriptor: int) -> None:
        nonlocal interrupted
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        if binding.exists():
            expected = binding.stat()
            if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
                interrupted = True
                raise OSError("controlled binding-file synchronization failure")

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fail_binding_sync)
        with pytest.raises(OSError, match="controlled binding-file"):
            make_service(tmp_path, client, executor)
    assert interrupted and client.claims == executor.started == []
    assert binding.read_bytes() == encoded(
        {"schema": "hi/hephaestus/build-owner/v1", "stateDir": str(state)}
    )

    def observe_retry_sync(descriptor: int) -> None:
        nonlocal synced
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        expected = binding.stat()
        if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
            synced = True

    actual_claim = client.claim_run

    def observe_retry_claim(
        build_id: str, claim: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        before_claim.append(synced)
        return actual_claim(build_id, claim, deadline=deadline)

    monkeypatch.setattr(os, "fsync", observe_retry_sync)
    monkeypatch.setattr(client, "claim_run", observe_retry_claim)
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "completed"
    assert len(client.claims) == len(executor.started) == len(executor.disposed) == 1
    assert before_claim == [True], "retained binding bytes were not synchronized before claim_run"


def test_retained_terminal_journal_is_resynchronized_before_publication_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume a reported append interruption without treating it as power loss."""
    state = tmp_path / "state"
    journal = state / "receipts.jsonl"
    client = Controller(state)
    executor = FixtureExecutor(state)
    actual_fsync = os.fsync
    interrupted = False
    synced = False
    before_publication: list[bool] = []

    def interrupt_terminal_sync(descriptor: int) -> None:
        nonlocal interrupted
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not journal.exists():
            return
        expected = journal.stat()
        if (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino):
            return
        lines = os.pread(descriptor, metadata.st_size, 0).splitlines()
        if lines and json.loads(lines[-1])["value"].get("terminal") is not None:
            interrupted = True
            # Report interruption after the real syscall. This is not hardware proof.
            raise KeyboardInterrupt("controlled terminal-append synchronization interruption")

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", interrupt_terminal_sync)
        with (
            pytest.raises(KeyboardInterrupt, match="controlled terminal-append"),
            make_service(tmp_path, client, executor) as service,
        ):
            service.handle(contract()["admission"]["command"])
    assert interrupted and client.facts == []
    assert len(executor.started) == len(executor.disposed) == 1
    terminal = json.loads(journal.read_bytes().splitlines()[-1])["value"]
    assert terminal["terminal"] is not None and terminal["published"] is False

    def observe_retry_sync(descriptor: int) -> None:
        nonlocal synced
        actual_fsync(descriptor)
        metadata = os.fstat(descriptor)
        expected = journal.stat()
        if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
            synced = True

    actual_publish = client.publish_fact

    def observe_retry_publish(
        build_id: str, fact: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        before_publication.append(synced)
        return actual_publish(build_id, fact, deadline=deadline)

    monkeypatch.setattr(os, "fsync", observe_retry_sync)
    monkeypatch.setattr(client, "publish_fact", observe_retry_publish)
    with make_service(tmp_path, client, executor) as service:
        assert service.handle(contract()["admission"]["command"])["status"] == "completed"
    assert len(client.claims) == len(executor.started) == len(executor.disposed) == 1
    assert client.facts == [terminal["terminal"]]
    assert before_publication == [True], "retained journal bytes were not synchronized before retry"
