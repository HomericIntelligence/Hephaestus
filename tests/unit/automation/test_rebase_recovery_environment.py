"""Preserve typed environment failures during retained-source recovery."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_UNAVAILABLE_TOKEN,
    HostCapabilityReceipt,
)
from hephaestus.automation.pipeline.stages import JobRequest
from tests.unit.automation.test_rebase_recovery import (
    _abort_worker_case,
    _publication_validation_seams,
    _restart_publication_checks,
    _restart_publication_stage,
    _store,
)
from tests.unit.automation.test_source_worktree import _git


@pytest.mark.parametrize("failure", ["signing_configuration", "remote_authentication"])
def test_retained_rebase_preserves_environment_failure_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Keep the specific setup cause and prior evidence without another mutation."""
    with _abort_worker_case(
        tmp_path, monkeypatch, fallback=False, fault="clean", conflict=False, structural=True
    ) as case:
        _publication_validation_seams(case, "initial", monkeypatch)

        def unavailable(target: Any, *, deadline: Any) -> HostCapabilityReceipt:
            assert deadline.remaining() > 0
            assert target.source_head_sha == _git(case.binding.cwd, "rev-parse", "HEAD")
            assert target.source_head_sha != case.original
            return HostCapabilityReceipt(
                False,
                QUOTA_UNAVAILABLE_TOKEN,
                "backend",
                "scratch",
                "b" * 32,
                target=target,
                cleanup_state="not_started",
            )

        case.backend.preflight.side_effect = unavailable
        first = case.pool._run_git(case.job)
        assert not first.ok and first.value["failure_kind"] == "validation_runner", first
        store = _store(case.manager.common_dir)
        retained = store.read(1, case.job.capability_target.request_id)
        assert retained is not None and retained.phase == "pending_validation"
        binding = retained.resulting_workspace
        assert binding is not None and binding.revision != case.original
        assert retained.remote_head_sha == case.original
        assert case.starts == [True]
        case.pushes.assert_not_called()
        receipt = case.manager._require_receipt(1, SourceLane.IMPLEMENTATION)
        case.pool.shutdown()
        stage, item, ctx = _restart_publication_stage(case, "initial", case.original)
        pool, checks, remote_reads = _restart_publication_checks(
            case, "remote_a", monkeypatch, tmp_path
        )
        push = Mock(side_effect=AssertionError("An environment failure cannot publish."))
        monkeypatch.setattr(git_utils, "push_head_to_branch", push)
        capabilities = pool._host_capabilities
        assert capabilities is not None
        trusted_tool = worker_pool._trusted_gh_executable
        failed_boundaries: list[str] = []

        def tool(extra_path_root: Path | None = None) -> str | None:
            if "semantic" in checks:
                failed_boundaries.append("post_semantic_authentication")
                return None
            return trusted_tool(extra_path_root)

        try:
            discovery = stage.step(item, ctx)
            assert isinstance(discovery, JobRequest) and isinstance(discovery.job, GitJob)
            assert discovery.job.op == "discover_pending_rebase"
            discovered = pool._run_git(discovery.job)
            assert discovered.ok, discovered
            assert discovered.value["rebase_recovery_candidate"] == retained.request.request_id
            stage.on_job_done(item, discovered, ctx)
            submitted = stage.step(item, ctx)
            assert isinstance(submitted, JobRequest) and isinstance(submitted.job, GitJob)
            assert submitted.job.workspace == binding
            assert submitted.job.rebase_recovery_candidate == retained.request.request_id
            if failure == "signing_configuration":
                pool._host_capabilities = replace(capabilities, signing_provider=None)
            else:
                monkeypatch.setattr(worker_pool, "_trusted_gh_executable", tool)
            result = pool._run_git(submitted.job)
        finally:
            pool.shutdown()
        assert not result.ok, result
        assert case.starts == [True]
        push.assert_not_called()
        assert store.read(1, retained.request.request_id) == retained
        assert case.manager._require_receipt(1, SourceLane.IMPLEMENTATION) == receipt
        assert _git(binding.cwd, "rev-parse", "HEAD") == binding.revision
        assert _git(binding.cwd, "rev-parse", "HEAD^{tree}") == retained.resulting_tree_sha
        assert _git(binding.cwd, "status", "--porcelain") == ""
        assert result.value["source_workspace"] == binding.to_dict()
        assert result.value["source_receipt"] == receipt.to_dict()
        assert remote_reads
        if failure == "signing_configuration":
            assert checks == []
            assert result.error == "The signing provider is unavailable."
            assert failed_boundaries == []
        else:
            assert checks == ["quota", "structural", "semantic"]
            assert failed_boundaries == ["post_semantic_authentication"]
            assert result.error == "required GitHub executable is unavailable"
        assert result.value["failure_kind"] == failure
        if failure == "remote_authentication":
            assert result.value["capability_receipt"].target.source_head_sha == binding.revision
            execution = result.value["structural_execution_receipt"]
            assert execution.ok
            assert execution.value["head_sha"] == binding.revision
            assert execution.value["immutable_source"] is True
