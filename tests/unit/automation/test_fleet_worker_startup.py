"""Retain provider ownership across incomplete worker startup."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_provider import ProviderError
from hephaestus.automation.fleet_worker import FleetWorker

pytestmark = pytest.mark.precommit
FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_provider.py"


@pytest.fixture
def unstarted_worker(tmp_path: Path) -> Iterator[FleetWorker]:
    """Prepare a worker that can run only the deterministic protocol fixture."""
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    codex_home = tmp_path / "codex"
    codex_home.mkdir(mode=0o700)
    worker = FleetWorker(
        state_dir=tmp_path / "worker",
        workspace_root=workspace,
        codex_home=codex_home,
        worker_id="startup-worker",
        pool_id="fixture-pool",
        host_id="fixture-host",
        generation=1,
        capacity=2,
        provider_command=[sys.executable, "-u", str(FIXTURE)],
    )
    # The protocol fixture uses temporary storage and performs no model or tool work.
    worker.storage_guard = lambda: None
    try:
        yield worker
    finally:
        worker.close()


def test_start_records_uncertainty_before_provider_launch(
    unstarted_worker: FleetWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash during provider startup must leave a durable reconciliation marker."""
    worker = unstarted_worker
    original_start = worker.provider.start

    def observe_start() -> None:
        assert worker.journal.runtime_uncertain is True
        assert worker.journal.runtime_pid is None
        records = [
            json.loads(line)
            for line in (worker.journal.directory / "receipts.jsonl").read_text().splitlines()
        ]
        assert records[-1] == {"kind": "runtime", "value": {"pid": None, "uncertain": True}}
        original_start()

    monkeypatch.setattr(worker.provider, "start", observe_start)
    worker.start()
    assert worker.provider.process is not None
    assert worker.journal.runtime_pid == worker.provider.process.pid
    assert worker.journal.runtime_uncertain is False


@pytest.mark.parametrize("failure_point", ["provider-start", "pid-receipt"])
@pytest.mark.parametrize("cleanup", ["confirmed", "uncertain", "raises"])
def test_partial_start_persists_cleanup_outcome(
    unstarted_worker: FleetWorker,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    cleanup: str,
) -> None:
    """A failed start must retain uncertain ownership until cleanup is confirmed."""
    worker = unstarted_worker
    original_start = worker.provider.start
    original_append = worker.journal.append
    original_close = worker.provider.close

    def fail_after_launch() -> None:
        original_start()
        raise ProviderError("fixture_start_failure")

    def fail_pid_receipt(kind: str, value: dict[str, Any]) -> None:
        if kind == "runtime" and value.get("pid") is not None:
            raise OSError("fixture_pid_receipt_failure")
        original_append(kind, value)

    def observe_cleanup() -> bool:
        # The real fixture is stopped even when the test reports an uncertain result.
        assert original_close() is True
        if cleanup == "raises":
            raise ProviderError("fixture_cleanup_failure")
        return cleanup == "confirmed"

    if failure_point == "provider-start":
        monkeypatch.setattr(worker.provider, "start", fail_after_launch)
        failure: type[Exception] = ProviderError
        message = "fixture_start_failure"
    else:
        monkeypatch.setattr(worker.journal, "append", fail_pid_receipt)
        failure = OSError
        message = "fixture_pid_receipt_failure"
    monkeypatch.setattr(worker.provider, "close", observe_cleanup)

    with pytest.raises(failure, match=message):
        worker.start()
    assert worker.provider.process is not None
    state_dir = worker.journal.directory
    if cleanup == "raises":
        with pytest.raises(ProviderError, match="fixture_cleanup_failure"):
            worker.close()
    else:
        worker.close()

    retained = WorkerJournal(state_dir)
    try:
        assert retained.runtime_pid is None
        assert retained.runtime_uncertain is (cleanup != "confirmed")
        assert retained.sessions == {}
    finally:
        retained.close()


def test_failed_preflight_does_not_replace_retained_runtime_evidence(
    unstarted_worker: FleetWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that does not attempt startup must preserve the previous generation."""
    worker = unstarted_worker
    worker.journal.append("generation", {"generation": 2})
    worker.journal.append("runtime", {"pid": None, "uncertain": True})
    path = worker.journal.directory / "receipts.jsonl"
    before = path.read_bytes()
    monkeypatch.setattr(worker.provider, "close", lambda: True)

    with pytest.raises(RuntimeError, match="generation_change_requires_reconciliation"):
        worker.start()
    worker.close()
    assert path.read_bytes() == before
