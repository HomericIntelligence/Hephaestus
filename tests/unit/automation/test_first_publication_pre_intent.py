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


def test_pre_intent_recovery_retains_a_non_rebase_resume_state() -> None:
    """Stable and legacy lanes keep their exact interrupted resume route.

    Physical receipt identity, not a per-run nonce, owns recovery. A non-rebase
    state therefore stays bound to the discovery result instead of being
    cleared as obsolete nonce state.
    """
    from hephaestus.automation.pipeline.stages.implementation.implementation import (
        _PUBLICATION_REBASE_RESUME,
        ImplementationStage,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    stage = ImplementationStage()
    item = WorkItem(repo="project", kind=ItemKind.ISSUE, issue=12, state="GATE")
    item.payload["first_publication_resume_state"] = "IMPLEMENT_WAIT"
    value = {"first_publication_pre_intent": True, "first_publication_rebase_candidate": None}

    retained = stage._retain_pre_intent_route(item, value, None)

    assert retained is True
    assert item.payload["first_publication_resume_state"] == "IMPLEMENT_WAIT"
    assert "first_publication_discovery_failed" not in item.payload
    assert item.payload[_PUBLICATION_REBASE_RESUME] == {
        "publication_resume_state": "IMPLEMENT_WAIT",
        "publication_manual_rebase": False,
    }


def test_pre_intent_route_still_rejects_an_invalid_handoff() -> None:
    """An invalid pending rebase handoff keeps its fail-closed contract."""
    import pytest

    from hephaestus.automation.pipeline.stages.implementation.implementation import (
        ImplementationStage,
    )
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    stage = ImplementationStage()
    item = WorkItem(repo="project", kind=ItemKind.ISSUE, issue=12, state="GATE")
    value = {
        "first_publication_pre_intent": True,
        "first_publication_rebase_candidate": "not-a-hash",
    }

    with pytest.raises(ValueError, match="pending rebase handoff is invalid"):
        stage._retain_pre_intent_route(item, value, None)
