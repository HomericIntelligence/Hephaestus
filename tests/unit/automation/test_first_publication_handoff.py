"""Reject changed recovery inputs after real publication discovery."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.worker_pool import _RemoteGitAuthenticationError
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.pipeline.test_worker_pool import _git
from tests.unit.automation.test_first_publication_admission import _retain_failed_publication
from tests.unit.automation.test_first_publication_process import _pool, _publication_job

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


@pytest.mark.parametrize(
    ("case", "failure"),
    [
        ("missing", "recovery candidate changed"),
        ("replaced", "recovery candidate changed"),
        ("ordinary-replay", "first publication requires admitted recovery"),
        ("main-authentication", "Controlled main authentication failure"),
    ],
)
def test_discovery_does_not_replace_fresh_recovery_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, failure: str
) -> None:
    """Preserve retained source when the handoff or fresh authentication fails."""
    mask = os.umask(0o022)
    pool = None
    try:
        job = _publication_job(tmp_path, monkeypatch)
        assert job.workspace is not None
        root = Path(job.kwargs["repo_root"])
        record_path = _retain_failed_publication(job, monkeypatch)
        record = json.loads(record_path.read_bytes())
        manager = SourceWorkspaceManager(root, repository=job.repo)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(receipt.path, "rev-parse", "HEAD^{tree}")
        pool = _pool(root, monkeypatch)
        discovered = pool._run_git(
            GitJob(
                repo=job.repo,
                op="discover_first_publication",
                timeout_s=60,
                kwargs={
                    "repo_root": str(root),
                    "issue_number": 9,
                    "branch": "",
                    "publication_discovery_request_id": "d" * 32,
                },
            )
        )
        assert discovered.ok, discovered.error
        assert discovered.value["first_publication_candidate"] == record["operation_id"]
        assert discovered.value["source_workspace"] == job.workspace.to_dict()
        evidence_path = record_path
        if case == "missing":
            evidence_path = record_path.rename(record_path.with_suffix(".retained"))
        elif case == "replaced":
            replacement = {**record, "operation_id": "a" * 32}
            assert replacement["operation_id"] != record["operation_id"]
            evidence_path = record_path.rename(record_path.with_name(f"9-{'a' * 32}.json"))
            evidence_path.write_text(json.dumps(replacement), encoding="utf-8")
        content = evidence_path.read_bytes()
        recovery = replace(
            job,
            kwargs={
                **job.kwargs,
                "first_publication_candidate": discovered.value["first_publication_candidate"],
                "publication_recovery_request_id": "e" * 32,
            },
        )
        if case == "ordinary-replay":
            recovery = job
        run = git_utils.run
        calls: list[tuple[str, ...]] = []

        def transport(argv: Any, **kwargs: Any) -> Any:
            command = tuple(argv)
            calls.append(command)
            assert not any(part in command for part in ("push", "commit", "rebase"))
            if case == "main-authentication" and "fetch" in command:
                raise _RemoteGitAuthenticationError("Controlled main authentication failure.")
            return run(argv, **kwargs)

        monkeypatch.setattr(git_utils, "run", transport)
        result = pool._run_git(recovery)
        assert not result.ok
        assert failure in (result.error or ""), result
        if case == "main-authentication":
            assert result.value["failure_kind"] == "remote_authentication"
            assert any("fetch" in command for command in calls)
        assert evidence_path.read_bytes() == content
        if case == "missing":
            assert not record_path.exists()
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == job.workspace.revision
        assert _git(receipt.path, "rev-parse", "HEAD^{tree}") == tree
        assert _git(receipt.path, "status", "--porcelain") == ""
        assert _git(root, "ls-remote", "origin", "refs/heads/writer") == ""
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
