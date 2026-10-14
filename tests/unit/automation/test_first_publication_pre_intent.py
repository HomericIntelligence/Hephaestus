"""Check retained source against independent implementation-start evidence."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.pipeline.test_worker_pool import _git
from tests.unit.automation.test_first_publication_process import _pool, _publication_job

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


@pytest.mark.parametrize(
    ("start", "valid", "ambiguous"),
    [
        ("missing", True, True),
        ("old", True, True),
        ("current", True, False),
        ("pending", True, False),
        ("dirty", True, False),
        ("malformed", False, None),
        ("foreign", False, None),
    ],
)
def test_discovery_requires_matching_start_without_publication_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    valid: bool,
    ambiguous: bool | None,
) -> None:
    """An unchanged start permits routing; an advanced H needs recovery."""
    mask = os.umask(0o022)
    pool = None
    try:
        job = _publication_job(tmp_path, monkeypatch)
        assert job.workspace is not None
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.repo)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        pool = _pool(root, monkeypatch)
        bound = replace(job, kwargs={**job.kwargs, "cwd": str(receipt.path)})
        path, identity = pool._initial_start_identity(bound)
        if start != "missing":
            result = pool._record_initial_start(
                bound,
                JobResult(
                    ok=True,
                    value={
                        "head_sha": (
                            job.kwargs["scope_history_base_sha"]
                            if start == "old"
                            else receipt.revision
                        )
                    },
                ),
                path,
                identity,
            )
            assert result.ok, result.error
            if start == "pending":
                path = path.rename(path.with_suffix(".pending.json"))
            elif start == "malformed":
                path.write_text("{}", encoding="utf-8")
            elif start == "foreign":
                payload = json.loads(path.read_bytes())
                payload["branch"] = "other-writer"
                path.write_text(json.dumps(payload), encoding="utf-8")
        if start == "dirty":
            (receipt.path / "local.txt").write_text("Pending content.\n", encoding="utf-8")
        content = path.read_bytes() if path.exists() else None
        status = _git(receipt.path, "status", "--porcelain")
        result = pool._run_git(
            GitJob(
                repo="project",
                expected_repository="example/project",
                op="discover_first_publication",
                timeout_s=60,
                kwargs={
                    "repo_root": str(root),
                    "issue_number": 9,
                    "branch": "writer",
                    "publication_discovery_request_id": "d" * 32,
                },
            )
        )
        assert result.ok is valid, result
        if valid:
            assert result.value["first_publication_candidate"] is None
            assert result.value.get("first_publication_pre_intent", False) is ambiguous
        else:
            assert result.value["failure_kind"] == "validation_runner"
        assert (path.read_bytes() if path.exists() else None) == content
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == receipt.revision
        assert _git(receipt.path, "status", "--porcelain") == status
        assert _git(root, "ls-remote", "origin", "refs/heads/writer") == ""
        assert not list((manager.state_dir / "first-publications").glob("9-*.json"))
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
