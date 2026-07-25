"""Completion-channel saturation and signal-wake safety contracts (#2399)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.jobs import JobResult
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _coordinator(tmp_path: Path) -> tuple[Coordinator, FakeWorkerPool]:
    """Return a one-slot coordinator with a synchronous worker-pool stand-in."""
    pool = FakeWorkerPool()
    coordinator = Coordinator(
        PipelineConfig(
            org="org",
            repos=["repo-a"],
            max_workers=1,
            parallel_repos=1,
            projects_dir=tmp_path,
        ),
        github=FakeStageGitHub(),
        pool=pool,
        install_signals=False,
    )
    return coordinator, pool


def test_signal_wake_never_blocks_on_a_full_completion_queue(tmp_path: Path) -> None:
    """Signal wake-up is a latch, not a queue write that can deadlock a handler."""
    coordinator, _pool = _coordinator(tmp_path)
    occupied = (object(), JobResult(ok=True, value="already queued"))
    coordinator.completion_q.put_nowait(occupied)
    callback = threading.Thread(target=coordinator._wake_completion_wait)

    callback.start()
    callback.join(timeout=0.2)
    blocked = callback.is_alive()
    try:
        still_queued = coordinator.completion_q.get_nowait()
    finally:
        if callback.is_alive():
            callback.join(timeout=1)

    assert not blocked
    assert still_queued is occupied
    assert coordinator.completion_q.empty()
    assert coordinator._completion_wakeup.is_set()


def test_completion_saturation_exits_failed_without_claiming_signal_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internal completion fault is exit 1, never an exit-130 cancellation."""
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda _repos, _issues, _prs: [])
    coordinator, pool = _coordinator(tmp_path)
    coordinator._completion_saturation.set()

    assert coordinator.run() == 1
    assert coordinator._fatal is True
    assert not coordinator.shutdown.is_set()
    assert pool.shutdown_calls == 1
