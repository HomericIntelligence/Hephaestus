"""Keep durable failure evidence when no quota provider is configured."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_UNAVAILABLE_TOKEN,
    CapabilityRequestTarget,
    HostCapabilityRead,
    HostCapabilityReceipt,
    WorkerCapabilities,
)
from hephaestus.automation.pipeline.jobs import HostCapabilityJob
from hephaestus.automation.pipeline.rebase_adr_policy import HEPHAESTUS_ADR_REBASE_POLICY
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.pipeline.test_worker_git_source_binding import (
    _supply_registered_worktree_listing,
)
from tests.unit.automation.test_source_worktree import _repository

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


@pytest.mark.parametrize("phase", ["pr_review", "rebase"])
@pytest.mark.parametrize("storage_fails", [False, True], ids=["stored", "storage-error"])
def test_missing_quota_provider_retains_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, storage_fails: bool
) -> None:
    """Return a durable backend failure or preserve its separate storage error."""
    mask = os.umask(0o022)
    pool = None
    try:
        root, _, head = _repository(tmp_path, origin_repository="example/project")
        manager = SourceWorkspaceManager(root, repository="example/project")
        lane = SourceLane.REVIEW if phase == "pr_review" else SourceLane.IMPLEMENTATION
        binding = manager.prepare(42, lane, head, branch=None if phase == "pr_review" else "writer")
        _supply_registered_worktree_listing(root, monkeypatch)
        target = CapabilityRequestTarget(
            "example/project",
            42,
            99,
            root,
            binding.cwd,
            head,
            phase,
            "scratch",
            "f" * 32,
            workspace=binding,
            generation=1,
        )
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            lock_dir=tmp_path / "locks",
            host_capabilities=WorkerCapabilities(None, "missing-provider-boundary"),
            rebase_policy_selector=lambda repo: HEPHAESTUS_ADR_REBASE_POLICY,
        )
        execution = Mock(side_effect=AssertionError("Missing quota cannot start execution."))
        monkeypatch.setattr(pool, "_run_immutable_build_test", execution)
        monkeypatch.setattr(pool, "_run_rebase_structural_validation", execution)
        if storage_fails:
            monkeypatch.setattr(
                "hephaestus.automation.host_capabilities._write_receipt",
                Mock(side_effect=OSError("Controlled receipt storage failure.")),
            )
        if phase == "pr_review":
            result = pool._run_host_capability(
                HostCapabilityJob(target.repository, target, 60, deadline_s=time.monotonic() + 60)
            )
            assert isinstance(result.value, HostCapabilityRead)
            receipt = result.value.receipt
        else:
            job = GitJob(
                target.repository,
                "rebase",
                60,
                workspace=binding,
                capability_target=target,
            )
            with git_utils.operation_deadline(time.monotonic() + 60):
                structural_result = pool._rebase_structural_receipts(job, binding, head)
            assert structural_result is not None
            result = structural_result
            receipt = result.value["capability_receipt"]
        assert not result.ok
        assert isinstance(receipt, HostCapabilityReceipt)
        assert not receipt.available
        assert (receipt.token, receipt.failed_step) == (QUOTA_UNAVAILABLE_TOKEN, "backend")
        assert receipt.target.request == target
        assert receipt.target.source_head_sha == head
        assert receipt.cleanup_state == "not_started"
        execution.assert_not_called()
        path = (
            root
            / "build/.issue_implementer/host-capability-receipts"
            / f"{receipt.receipt_id}.json"
        )
        if storage_fails:
            assert receipt.persistence_error == "Controlled receipt storage failure."
            assert receipt.persistence_exception_type == "OSError"
            assert not path.exists()
        else:
            assert path.is_file(), "Missing-provider failure must have a durable receipt."
            stored = json.loads(path.read_bytes())
            assert stored["receipt"] == json.loads(json.dumps(asdict(receipt), default=str))
            assert stored["target"] == json.loads(json.dumps(asdict(receipt.target), default=str))
            assert not receipt.persistence_error
            assert path.stat().st_mode & 0o777 == 0o600
        assert manager._require_receipt(42, lane).revision == head
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
