"""Check fresh admission before a retained publication can resume."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import git_utils
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import SigningConfigurationError
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.pipeline.conftest import FakeSigningProvider
from tests.unit.automation.pipeline.test_worker_pool import _git
from tests.unit.automation.test_first_publication_process import _pool, _publication_job

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Source ownership requires POSIX locks."),
]


def _retain_failed_publication(job: GitJob, patch: pytest.MonkeyPatch) -> Path:
    """Create real durable intent before a controlled transport failure."""
    root = Path(job.kwargs["repo_root"])
    pool = _pool(root, patch)
    run = git_utils.run

    def transport(argv: Any, **kwargs: Any) -> Any:
        if "push" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr="Controlled offline push.")
        return run(argv, **kwargs)

    try:
        with patch.context() as changes:
            changes.setattr(git_utils, "run", transport)
            result = pool._run_git(job)
        assert not result.ok
        assert result.value["publication_state"] == "remote_absent"
        manager = SourceWorkspaceManager(root, repository=job.repo)
        records = list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_bytes())["phase"] == "publication_intent"
        return records[0]
    finally:
        pool.shutdown()


def _prepare_start_record(pool: WorkerPool, job: GitJob, case: str) -> Path:
    """Use the start-record owner, then alter only the selected input."""
    assert job.workspace is not None
    bound = replace(job, kwargs={**job.kwargs, "cwd": str(job.workspace.cwd)})
    path, identity = pool._initial_start_identity(bound)
    assert not path.exists(), "The missing-record case must have no start evidence."
    if case.startswith("start-"):
        result = pool._record_initial_start(
            bound,
            JobResult(ok=True, value={"head_sha": job.kwargs["scope_history_base_sha"]}),
            path,
            identity,
        )
        assert result.ok, result.error
        value = json.loads(path.read_bytes())
        if case == "start-conflict":
            value["head_sha"] = job.workspace.revision
        elif case == "start-foreign":
            value["branch"] = "different-writer"
        elif case == "start-malformed":
            value = {}
        path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _observe_admission_transport(
    patch: pytest.MonkeyPatch, case: str, head: str
) -> list[tuple[str, ...]]:
    """Control failed Git observations without replacing recovery admission."""
    run = git_utils.run
    calls: list[tuple[str, ...]] = []

    def transport(argv: Any, **kwargs: Any) -> Any:
        command = tuple(argv)
        calls.append(command)
        if case == "main-fetch" and "fetch" in command:
            raise subprocess.CalledProcessError(128, argv, stderr="Controlled main fetch failure.")
        result = run(argv, **kwargs)
        if case == "main-invalid" and command[-1] == "FETCH_HEAD^{commit}":
            return subprocess.CompletedProcess(argv, 0, stdout="invalid\n", stderr="")
        if "merge-base" in command and "--all" in command:
            if case == "merge-ambiguous":
                return subprocess.CompletedProcess(argv, 0, stdout=f"{result.stdout}{head}\n")
            if case == "merge-invalid":
                return subprocess.CompletedProcess(argv, 0, stdout="invalid\n")
        return result

    patch.setattr(git_utils, "run", transport)
    return calls


def _reject_signing(
    self: FakeSigningProvider, cwd: Path, *, timeout: int, private_metadata: bool = False
) -> dict[str, str]:
    """Report a provider failure after the publication intent already exists."""
    raise SigningConfigurationError("Controlled recovery signing failure.")


def _prepare_metadata_gap(job: GitJob, key: Path, case: str) -> GitJob:
    """Make a real fixture commit with exactly one missing metadata field."""
    if case not in {"signature-missing", "dco-missing"}:
        return job
    assert job.workspace is not None
    manager = SourceWorkspaceManager(Path(job.kwargs["repo_root"]), repository=job.repo)
    worktree = job.workspace.cwd
    tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    with manager.implementation_local_commit(
        9, branch="writer", path=worktree, expected_binding=job.workspace
    ) as record:
        if case == "signature-missing":
            _git(worktree, "commit", "--amend", "--no-gpg-sign", "--no-edit")
        else:
            _git(
                worktree,
                "-c",
                "gpg.format=ssh",
                "-c",
                f"user.signingkey={key}",
                "commit",
                "--amend",
                "-S",
                "-m",
                "fix: prepare a fixture without DCO",
            )
        head = _git(worktree, "rev-parse", "HEAD")
        binding = record(head)
    raw = _git(worktree, "cat-file", "-p", head)
    assert ("\ngpgsig " in f"\n{raw}") is (case != "signature-missing")
    assert ("Signed-off-by:" in raw) is (case != "dco-missing")
    assert _git(worktree, "rev-parse", "HEAD^{tree}") == tree
    return replace(job, workspace=binding)


@pytest.mark.parametrize(
    ("case", "failure"),
    [
        ("missing", None),
        ("start-matching", None),
        ("start-conflict", "conflicts with the start record"),
        ("start-foreign", "initial implementation record does not match"),
        ("start-malformed", "initial implementation record does not match"),
        ("main-fetch", "Controlled main fetch failure"),
        ("main-invalid", "fetched main head is invalid"),
        ("merge-ambiguous", "merge base is ambiguous"),
        ("merge-invalid", "merge base is ambiguous"),
        ("base-nonancestor", "base is not an ancestor"),
        ("signing-unavailable", "Controlled recovery signing failure"),
        ("signature-missing", "signing metadata is unavailable"),
        ("dco-missing", "signing metadata is unavailable"),
    ],
)
def test_recovery_checks_fresh_start_and_main_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, failure: str | None
) -> None:
    """Reject failed admission without changing source, intent, or the remote."""
    mask = os.umask(0o022)
    pool = None
    try:
        job = _publication_job(tmp_path, monkeypatch)
        job = _prepare_metadata_gap(job, tmp_path / "signing-key", case)
        assert job.workspace is not None
        head = job.workspace.revision
        assert head is not None
        record_path = _retain_failed_publication(job, monkeypatch)
        if case == "base-nonancestor":
            retained = json.loads(record_path.read_bytes())
            unrelated = _git(
                job.workspace.cwd,
                "commit-tree",
                retained["tree_sha"],
                "-m",
                "Independent root commit",
            )
            assert unrelated != head
            retained["scope_base_sha"] = unrelated
            record_path.write_text(json.dumps(retained), encoding="utf-8")
        content = record_path.read_bytes()
        record = json.loads(content)
        root = Path(job.kwargs["repo_root"])
        manager = SourceWorkspaceManager(root, repository=job.repo)
        receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(receipt.path, "rev-parse", "HEAD^{tree}")
        pool = _pool(root, monkeypatch)
        start_path = _prepare_start_record(pool, job, case)
        start_content = start_path.read_bytes() if start_path.exists() else None
        recovery = replace(
            job,
            kwargs={
                **job.kwargs,
                "scope_history_base_sha": head,
                "publish_base_sha": head,
                "first_publication_candidate": record["operation_id"],
                "publication_recovery_request_id": "e" * 32,
            },
        )
        calls = _observe_admission_transport(monkeypatch, case, head)
        if case == "signing-unavailable":
            monkeypatch.setattr(FakeSigningProvider, "environment", _reject_signing)
        result = pool._run_git(recovery)
        assert result.ok is (failure is None), result
        pushes = [command for command in calls if "push" in command]
        if failure is not None:
            assert failure in f"{result.error}\n{result.stderr_tail}", result
            assert not pushes
            assert record_path.read_bytes() == content
            assert _git(root, "ls-remote", "origin", "refs/heads/writer") == ""
        else:
            assert len(pushes) == 1
            assert result.value["head_sha"] == head
            assert json.loads(record_path.read_bytes())["phase"] == "complete"
            assert _git(root, "ls-remote", "origin", "refs/heads/writer") == (
                f"{head}\trefs/heads/writer"
            )
        assert any("fetch" in command for command in calls)
        assert not any("commit" in command or "rebase" in command for command in calls)
        assert (start_path.read_bytes() if start_path.exists() else None) == start_content
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == head
        assert _git(receipt.path, "rev-parse", "HEAD^{tree}") == tree
        assert _git(receipt.path, "status", "--porcelain") == ""
    finally:
        if pool is not None:
            pool.shutdown()
        os.umask(mask)
