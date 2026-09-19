"""Keep durable quota evidence across fresh stage and worker instances."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.host_capabilities import HdiutilQuotaBackend
from hephaestus.automation.pipeline.host_capabilities import HostCapabilityRead, WorkerCapabilities
from hephaestus.automation.pipeline.jobs import BuildTestJob, HostCapabilityJob
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.pipeline.stages.pr_review_verification import _host_verification_specs
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


@pytest.fixture(autouse=True)
def protected_parent_permissions() -> Iterator[None]:
    """Create protected fixture parents and restore the process umask."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_fresh_stage_and_worker_probe_before_releasing_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_ctx: Any, make_work_item: Any
) -> None:
    """Retry at the same head without reusing the failed process receipt."""
    root, _, head = _repository(tmp_path, origin_repository="example/project")
    manager = SourceWorkspaceManager(root, repository="example/project")
    binding = manager.prepare(42, SourceLane.REVIEW, head)
    _supply_registered_worktree_listing(root, monkeypatch)
    ctx = make_ctx(org="example")
    item = make_work_item(repo="project", issue=42, pr=99, state="REVIEW_CHECKOUT_WAIT")
    item.payload["pr_review_cycle"] = item.attempts.get("implement", 0)
    comment = Mock()
    monkeypatch.setattr(ctx.github, "upsert_issue_comment", comment)
    attempts = dict(item.attempts)
    mutations = list(ctx.github.mutation_log)
    labels = dict(item.labels_cache)
    spec = _host_verification_specs(["hephaestus/automation/pipeline/worker_pool.py"])[0]
    retained_path = None
    retained_bytes = b""
    previous_request = None
    for attempt in range(2):
        stage = PrReviewStage()
        if attempt:
            assert stage.on_enter(item, ctx) is None
            assert "host_capability_result" not in item.payload
            assert "host_capability_request" not in item.payload
            manager = SourceWorkspaceManager(root, repository="example/project")
            binding = manager.prepare(42, SourceLane.REVIEW, head)
        item.worktree = str(binding.cwd)
        item.payload.update(
            reviewed_pr_head_sha=head,
            reviewed_pr_proof_generation=attempt + 1,
            host_verification_workspace=binding,
        )
        submitted = stage._submit_host_verification(item, ctx, spec)
        assert isinstance(submitted, JobRequest)
        assert isinstance(submitted.job, HostCapabilityJob)
        assert submitted.job.target.request_id != previous_request
        previous_request = submitted.job.target.request_id
        runner = Mock(
            return_value=subprocess.CompletedProcess((), 1 - attempt, b"", b"quota probe")
        )
        provider = HdiutilQuotaBackend(command_runner=runner, host_probe=lambda: ("darwin", True))
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            lock_dir=tmp_path / "locks",
            host_capabilities=WorkerCapabilities(provider, f"restart-{attempt}"),
        )
        try:
            runner.assert_not_called()
            assert "host_verification_pending" not in item.payload
            result = pool._run_host_capability(submitted.job)
            assert isinstance(result.value, HostCapabilityRead)
            receipt = result.value.receipt
            assert receipt is not None
            assert receipt.available is bool(attempt)
            assert result.ok is bool(attempt)
            assert not receipt.cached
            assert runner.call_count == (1 if not attempt else 3)
            path = (
                root
                / "build/.issue_implementer/host-capability-receipts"
                / f"{receipt.receipt_id}.json"
            )
            stored = json.loads(path.read_bytes())
            assert stored["target"]["source_head_sha"] == head
            assert stored["target"]["request"]["request_id"] == previous_request
            stage.on_job_done(item, result, ctx)
            item.state = submitted.on_done_state
            outcome = stage.step(item, ctx)
            if not attempt:
                assert isinstance(outcome, StageOutcome)
                assert outcome.disposition is Disposition.BLOCKED
                assert receipt.failed_step == "create"
                assert "quota probe" in comment.call_args.args[-1]
                retained_path, retained_bytes = path, path.read_bytes()
            else:
                assert isinstance(outcome, JobRequest)
                assert isinstance(outcome.job, BuildTestJob)
                assert outcome.job.expected_head_sha == head
                assert outcome.job.cwd == binding.cwd
                assert outcome.job.immutable_source is True
                assert retained_path is not None and retained_path != path
                assert retained_path.read_bytes() == retained_bytes
            assert item.attempts == attempts
            assert item.labels_cache == labels
            assert ctx.github.mutation_log == mutations
            assert manager._require_receipt(42, SourceLane.REVIEW).revision == head
        finally:
            pool.shutdown()
        del pool, provider, stage
