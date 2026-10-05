"""Require live finalization acknowledgment and independently read actual bytes.

The HTTP and scheduler identities are
controlled fixtures; execution, publication, collection and storage are real.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_receipts import read_fleet_build_receipt
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.io.utils import write_secure
from tests.unit.automation.test_fleet_build_jobs import PreparedCase, submitted_pool


@dataclass(frozen=True)
class CompletedBuild:
    """Retain an actual successful completion and its on-disk candidate."""

    case: PreparedCase
    result: JobResult
    path: Path
    payload: dict[str, Any]


@pytest.fixture
def completed_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CompletedBuild]:
    """Complete actual offload independently of the receipt reader."""
    case = PreparedCase(tmp_path)
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, result = completions.get(timeout=10)
            assert completed is handle
            assert result.ok is True, result.error
            assert result.value == case.collect(deadline=time.monotonic() + 5)
            (path,) = (case.build.root / "pipeline-receipts").glob("*.json")
            yield CompletedBuild(case, result, path, json.loads(path.read_text()))
    finally:
        case.close()


def actual_reader(result: JobResult) -> Callable[..., dict[str, Any]]:
    """Assert public completion behavior before accessing its closed reader."""
    assert result.fleet_receipt is not None, (
        "successful offload returned no live finalization acknowledgment"
    )
    return read_fleet_build_receipt


def test_successful_completion_reads_its_exact_finalized_collection(
    completed_build: CompletedBuild,
) -> None:
    """Read actual finalized bytes with the completion's live acknowledgment."""
    result = completed_build.result
    reader = actual_reader(result)
    assert reader(
        result,
        expected=copy.deepcopy(result.value["identity"]),
        deadline=time.monotonic() + 5,
    ) == completed_build.case.collect(deadline=time.monotonic() + 5)
    assert completed_build.payload["fleet_collection"] == result.value
    assert completed_build.payload["tested_patch_sha256"] is None


@pytest.mark.parametrize(
    "change",
    [
        "missing_ack",
        "untyped_ack",
        "failed",
        "interrupted",
        "value",
        "expected",
        "pending",
        "bytes",
        "link",
    ],
)
def test_reader_rejects_unacknowledged_or_changed_completion(
    completed_build: CompletedBuild, change: str
) -> None:
    """Refuse altered completion authority and changed private receipt bytes."""
    original = completed_build.result
    reader = actual_reader(original)
    result, expected = original, copy.deepcopy(original.value["identity"])
    if change == "missing_ack":
        result = replace(original, fleet_receipt=None)
    elif change == "untyped_ack":
        invalid_ack: Any = object()
        result = replace(original, fleet_receipt=invalid_ack)
    elif change == "failed":
        result = replace(original, ok=False, error="fixture_failed_completion")
    elif change == "interrupted":
        result = replace(original, interrupted=True)
    elif change == "value":
        value = copy.deepcopy(original.value)
        value["reference"]["digest"] = "0" * 64
        result = replace(original, value=value)
    elif change == "expected":
        expected["snapshot"]["manifestDigest"] = "0" * 64
    elif change == "pending":
        payload = {**completed_build.payload, "fleet_receipt_state": "pending"}
        write_secure(completed_build.path, json.dumps(payload))
    elif change == "bytes":
        write_secure(completed_build.path, completed_build.path.read_text() + " ")
    elif change == "link":
        moved = completed_build.path.with_suffix(".retained")
        completed_build.path.rename(moved)
        completed_build.path.symlink_to(moved)
    with pytest.raises((ValueError, RuntimeError)):
        reader(result, expected=expected, deadline=time.monotonic() + 5)


def test_another_actual_completion_acknowledgment_does_not_authorize_this_result(
    completed_build: CompletedBuild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject transplantation from another real successful completion."""
    original = completed_build.result
    reader = actual_reader(original)
    second_root = tmp_path / "other-completion"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, other = completions.get(timeout=10)
            assert completed is handle
            assert other.ok is True, other.error
            assert other.fleet_receipt is not None
            assert other.value["identity"] != original.value["identity"]
            transplanted = replace(original, fleet_receipt=other.fleet_receipt)
            with pytest.raises((ValueError, RuntimeError)):
                reader(
                    transplanted,
                    expected=original.value["identity"],
                    deadline=time.monotonic() + 5,
                )
    finally:
        case.close()


def test_selected_build_without_receipt_capability_refuses_before_submission(
    completed_build: CompletedBuild,
    tmp_path: Path,
) -> None:
    """Refuse missing Fleet evidence capability without dispatch or local work."""
    reader = actual_reader(completed_build.result)
    second_root = tmp_path / "no-receipt-capability"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    completions = CompletionQueue(maxsize=1)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        fleet_build_runner=case.runner(),
    )
    try:
        handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
        completed, result = completions.get(timeout=10)
        assert completed is handle
        assert result.ok is False and result.error
        assert result.fleet_receipt is None
        assert case.build.http is not None and case.build.http.requests == []
        assert case.build.publisher.scheduler.starts == []
        with pytest.raises((ValueError, RuntimeError)):
            reader(
                result,
                expected=completed_build.result.value["identity"],
                deadline=time.monotonic() + 5,
            )
    finally:
        pool.shutdown()
        case.close()


def test_shutdown_during_final_write_cannot_return_success_acknowledgment(
    completed_build: CompletedBuild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe a successful write without promoting interrupted completion."""
    reader = actual_reader(completed_build.result)
    second_root = tmp_path / "shutdown-build"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    observed: list[str] = []
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):

            def stop_after_write(path: Path, content: str) -> None:
                """Set the actual shutdown signal after real receipt publication."""
                write_secure(path, content)
                state = json.loads(content).get("fleet_receipt_state", "")
                observed.append(state)
                if state == "finalized":
                    pool._shutdown.set()

            monkeypatch.setattr(worker_pool, "write_secure", stop_after_write)
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, result = completions.get(timeout=10)
            assert completed is handle
            assert "finalized" in observed
            assert result.ok is False and result.interrupted
            assert result.fleet_receipt is None
            with pytest.raises((ValueError, RuntimeError)):
                reader(
                    result,
                    expected=case.collect(deadline=time.monotonic() + 5)["identity"],
                    deadline=time.monotonic() + 5,
                )
    finally:
        case.close()


def test_after_effect_and_failed_invalidation_never_supply_usable_acknowledgment(
    completed_build: CompletedBuild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain explicit storage uncertainty without issuing completion authority."""
    reader = actual_reader(completed_build.result)
    second_root = tmp_path / "second-build"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    writes: list[str] = []
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):

            def write_then_fail(path: Path, content: str) -> None:
                """Lose the final write reply and reject its corrective write."""
                state = json.loads(content).get("fleet_receipt_state", "")
                writes.append(state)
                if state == "failed":
                    raise OSError("fixture invalidation storage unavailable")
                write_secure(path, content)
                if state == "finalized":
                    raise OSError("fixture finalization reply lost after publication")

            monkeypatch.setattr(worker_pool, "write_secure", write_then_fail)
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, result = completions.get(timeout=10)
            assert completed is handle
            assert result.ok is False and result.error
            assert result.fleet_receipt is None
            assert writes == ["pending", "finalized", "failed"]
            (path,) = (case.build.root / "pipeline-receipts").glob("*.json")
            assert json.loads(path.read_text())["fleet_receipt_state"] == "finalized"
            with pytest.raises((ValueError, RuntimeError)):
                reader(
                    result,
                    expected=case.collect(deadline=time.monotonic() + 5)["identity"],
                    deadline=time.monotonic() + 5,
                )
            assert case.build.http is not None
            assert sum(row["method"] == "POST" for row in case.build.http.requests) == 1
            assert not any(row["path"].endswith("/cancel") for row in case.build.http.requests)
    finally:
        case.close()


def test_failed_final_directory_sync_never_issues_acknowledgment(
    completed_build: CompletedBuild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject finalization when its actual directory synchronization fails."""
    reader = actual_reader(completed_build.result)
    second_root = tmp_path / "durability-build"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    real_sync = os.fsync
    failed_sync: list[int] = []
    receipt_dir = second_root / "pipeline-receipts"
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):

            def reject_final_directory_sync(fd: int) -> None:
                """Identify the actual receipt directory before its sync failure."""
                metadata = os.fstat(fd)
                if receipt_dir.exists() and stat.S_ISDIR(metadata.st_mode):
                    directory = receipt_dir.stat()
                    if (metadata.st_dev, metadata.st_ino) == (directory.st_dev, directory.st_ino):
                        candidates = list(receipt_dir.glob("*.json"))
                        if (
                            candidates
                            and json.loads(candidates[0].read_text()).get("fleet_receipt_state")
                            == "finalized"
                        ):
                            failed_sync.append(fd)
                            raise OSError("fixture final directory sync failed")
                real_sync(fd)

            monkeypatch.setattr(os, "fsync", reject_final_directory_sync)
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, result = completions.get(timeout=10)
            assert completed is handle
            assert failed_sync
            assert result.ok is False and result.error
            assert result.fleet_receipt is None
            with pytest.raises((ValueError, RuntimeError)):
                reader(
                    result,
                    expected=case.collect(deadline=time.monotonic() + 5)["identity"],
                    deadline=time.monotonic() + 5,
                )
    finally:
        case.close()


def test_new_process_cannot_promote_retained_candidate_without_acknowledgment(
    completed_build: CompletedBuild,
) -> None:
    """Start a new interpreter and deny promotion of retained candidate bytes."""
    actual_reader(completed_build.result)
    script = """
import json
import sys
import time
sys.path.insert(0, sys.argv[1])
from hephaestus.automation.fleet_build_receipts import read_fleet_build_receipt
from hephaestus.automation.pipeline.jobs import JobResult
with open(sys.argv[2]) as stream:
    receipt = json.load(stream)
assert receipt['fleet_receipt_state'] == 'finalized'
result = JobResult(ok=True, value=receipt['fleet_collection'])
try:
    read_fleet_build_receipt(
        result,
        expected=result.value['identity'],
        deadline=time.monotonic() + 5,
    )
except (ValueError, RuntimeError):
    print('rejected_no_ack')
else:
    raise AssertionError('restart promoted an unacknowledged receipt')
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-c",
            script,
            str(Path(__file__).resolve().parents[3]),
            str(completed_build.path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "rejected_no_ack\n"


def test_copied_typed_acknowledgment_cannot_recreate_live_registration(
    completed_build: CompletedBuild,
) -> None:
    """Copy real acknowledgment fields and require the original live instance."""
    original = completed_build.result
    reader = actual_reader(original)
    acknowledgment = original.fleet_receipt
    assert acknowledgment is not None
    reconstructed = replace(acknowledgment)
    assert type(reconstructed) is type(acknowledgment)
    assert reconstructed is not acknowledgment
    assert vars(reconstructed) == vars(acknowledgment)
    candidate = replace(original, fleet_receipt=reconstructed)
    with pytest.raises((ValueError, RuntimeError)):
        reader(
            candidate,
            expected=original.value["identity"],
            deadline=time.monotonic() + 5,
        )
    assert reader(
        original,
        expected=original.value["identity"],
        deadline=time.monotonic() + 5,
    ) == completed_build.case.collect(deadline=time.monotonic() + 5)


def test_original_deadline_expiry_after_actual_final_write_cannot_issue_acknowledgment(
    completed_build: CompletedBuild,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expire the executing worker's original budget after actual final publication."""
    reader = actual_reader(completed_build.result)
    second_root = tmp_path / "expired-finalization"
    second_root.mkdir(mode=0o700)
    case = PreparedCase(second_root)
    real_monotonic = time.monotonic
    finalized = threading.Event()
    observed: list[str] = []
    try:
        with submitted_pool(case, monkeypatch) as (pool, completions, _writes):

            def clock_after_write() -> float:
                """Advance only this executing worker after its actual final write."""
                if finalized.is_set() and threading.current_thread().name.startswith(
                    "hephaestus-pipeline-worker"
                ):
                    assert len(case.source_deadlines) == 1
                    return case.source_deadlines[0] + 1
                return real_monotonic()

            def expire_after_write(path: Path, content: str) -> None:
                """Publish the real bytes before applying the deadline fault."""
                write_secure(path, content)
                state = json.loads(content).get("fleet_receipt_state", "")
                observed.append(state)
                if state == "finalized":
                    finalized.set()

            monkeypatch.setattr(time, "monotonic", clock_after_write)
            monkeypatch.setattr(worker_pool, "write_secure", expire_after_write)
            handle = pool.submit(case.job(), StageName.IMPLEMENTATION)
            completed, result = completions.get(timeout=10)
            assert completed is handle
            assert "finalized" in observed
            assert result.ok is False and result.error
            assert result.interrupted is False
            assert result.fleet_receipt is None
            with pytest.raises((ValueError, RuntimeError)):
                reader(
                    result,
                    expected=case.collect(deadline=time.monotonic() + 5)["identity"],
                    deadline=time.monotonic() + 5,
                )
            assert case.build.http is not None
            assert sum(row["method"] == "POST" for row in case.build.http.requests) == 1
            assert not any(row["path"].endswith("/cancel") for row in case.build.http.requests)
    finally:
        case.close()
