"""Check durable first-start and fresh conflict admission."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.jobs import GitJob, JobResult
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool

WP = "hephaestus.automation.pipeline.worker_pool"


@pytest.fixture
def worker_factory(tmp_path: Path) -> Iterator[Callable[[], WorkerPool]]:
    """Create test workers and close them after the test."""
    workers: list[WorkerPool] = []

    def create() -> WorkerPool:
        worker = WorkerPool(
            size=1,
            shutdown=Event(),
            completion_q=CompletionQueue(),
            lock_dir=tmp_path / "worker-locks",
        )
        workers.append(worker)
        return worker

    try:
        yield create
    finally:
        for worker in workers:
            worker.shutdown(mark_interrupted=False)


def test_first_start_survives_a_new_worker_process(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A stored start must prevent another main fetch after restart."""
    state_dir = tmp_path / "host-state"
    state_dir.mkdir()
    manager = SimpleNamespace(
        state_dir=state_dir, repository_identity="test/repo:local", common_dir=tmp_path
    )
    head = "a" * 40
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "issue_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "implementation_start",
            "expected_head_sha": head,
        },
    )
    with (
        patch(f"{WP}.SourceWorkspaceManager", return_value=manager),
        patch(f"{WP}.WorktreeManager.git_metadata_lock_path", return_value=tmp_path / "git.lock"),
        patch.object(WorkerPool, "_read_publish_head", return_value=head),
        patch.object(
            WorkerPool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": head})
        ) as fetch,
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(returncode=0, stdout="issue-branch\n")),
    ):
        created = JobResult(
            ok=True,
            value={
                "path": str(tmp_path),
                "impl_source_revision": head,
                "fresh_branch_created": True,
            },
        )
        creation_job = GitJob(
            repo="repo",
            expected_repository="test/repo",
            op="create_worktree",
            timeout_s=60,
            deadline_s=monotonic() + 60,
            kwargs={**job.kwargs, "branch_name": "issue-branch"},
        )
        worker_factory()._record_fresh_initial_creation(creation_job, created)
        first = worker_factory()._git_rebase(job, record_source=MagicMock())
        second = worker_factory()._git_rebase(job, record_source=MagicMock())
    assert first.ok and second.ok
    assert second.value["implementation_started"] is True
    assert fetch.call_count == 1


def test_review_conflict_needs_fresh_admission_after_fetch(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A missing live GitHub reader must prevent replay."""
    pool = worker_factory()
    pool._github_job_runner = None
    head = "a" * 40
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "pr_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "review_conflict",
            "publish_rebased_head": True,
            "expected_remote_sha": head,
        },
    )
    with (
        patch.object(pool, "_sync_writer_to_expected_remote_head", return_value=None),
        patch.object(pool, "_read_publish_head", return_value=head),
        patch.object(
            pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": "b" * 40})
        ),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(returncode=1)),
        patch(f"{WP}._required_git_signing_env", return_value={}),
        patch(f"{WP}.git_utils.rebase_worktree_onto", return_value=True) as rebase,
        patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
        patch(f"{WP}.git_utils.push_head_to_branch"),
    ):
        result = pool._git_rebase(job, record_source=MagicMock())
    assert not result.ok
    rebase.assert_not_called()


def test_initial_continuation_records_start_before_return(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Successful local conflict resolution must save the first-start record."""
    import json

    state = tmp_path / "host-state"
    state.mkdir()
    manager = SimpleNamespace(
        state_dir=state, repository_identity="test/repo:local", common_dir=tmp_path
    )
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="continue_rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "issue_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "implementation_start",
        },
    )
    with (
        patch(f"{WP}.SourceWorkspaceManager", return_value=manager),
        patch(f"{WP}.WorktreeManager.git_metadata_lock_path", return_value=tmp_path / "git.lock"),
        patch.object(
            WorkerPool,
            "_git_continue_rebase_once",
            return_value=JobResult(ok=True, value={"head_sha": "a" * 40, "published": False}),
        ),
    ):
        result = worker_factory()._git_continue_rebase(job, record_source=MagicMock())
    assert result.ok
    assert result.value["implementation_started"] is True
    assert (
        json.loads((state / "7-implementation-start.json").read_text())["branch"] == "issue-branch"
    )


def test_initial_record_rejects_other_branch(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A different branch cannot reuse another branch's start record."""
    import json

    state = tmp_path / "host-state"
    state.mkdir()
    (state / "7-implementation-start.json").write_text(
        json.dumps(
            {
                "format": 1,
                "repository": "test/repo",
                "repository_identity": "test/repo:local",
                "issue_number": 7,
                "branch": "other-branch",
                "head_sha": "a" * 40,
            }
        )
    )
    manager = SimpleNamespace(
        state_dir=state, repository_identity="test/repo:local", common_dir=tmp_path
    )
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "issue_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "implementation_start",
            "expected_head_sha": "a" * 40,
        },
    )
    with (
        patch(f"{WP}.SourceWorkspaceManager", return_value=manager),
        patch(f"{WP}.WorktreeManager.git_metadata_lock_path", return_value=tmp_path / "git.lock"),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(stdout="issue-branch\n")),
        patch.object(WorkerPool, "_git_fetch_main") as fetch,
    ):
        result = worker_factory()._git_rebase(job, record_source=MagicMock())
    assert not result.ok
    fetch.assert_not_called()


def test_live_conflict_requires_same_base_and_exclusive_go() -> None:
    """Reject stale bases, revoked GO, and a clean branch behind main."""
    from hephaestus.automation.pipeline.github_jobs import InspectRebaseConflictRequest
    from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

    request = InspectRebaseConflictRequest("test/repo", 7, "a" * 40, "b" * 40)
    state = {
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "baseRefOid": "b" * 40,
        "baseRefName": "main",
        "autoMergeRequest": None,
    }
    for change in ("none", "base", "head", "go", "behind", "late_base", "late_go"):
        github = MagicMock()
        first = dict(state)
        last = dict(state)
        if change in ("base", "head"):
            first["baseRefOid" if change == "base" else "headRefOid"] = "c" * 40
        if change == "late_base":
            last["baseRefOid"] = "c" * 40
        github.gh_pr_state.side_effect = [first, last]
        github.pr_has_implementation_state_label.side_effect = [
            (change != "go", False),
            (change != "late_go", False),
        ]
        github.gh_pr_merge_readiness.return_value = {
            **state,
            "mergeable": "MERGEABLE" if change == "behind" else "CONFLICTING",
            "mergeStateStatus": "BEHIND" if change == "behind" else "DIRTY",
        }
        receipt = PipelineGitHubJobRunner._inspect_rebase_conflict(request, github)
        assert receipt.admitted is (change == "none"), change
        github.edit_labels.assert_not_called()
        github.merge_pr.assert_not_called()


def test_legacy_branch_without_start_provenance_does_not_rebase(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Unknown implementation history must stop before a base fetch."""
    state = tmp_path / "host-state"
    state.mkdir()
    manager = SimpleNamespace(
        state_dir=state, repository_identity="test/repo:local", common_dir=tmp_path
    )
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "issue_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "implementation_start",
            "expected_head_sha": "a" * 40,
        },
    )
    with (
        patch(f"{WP}.SourceWorkspaceManager", return_value=manager),
        patch(f"{WP}.WorktreeManager.git_metadata_lock_path", return_value=tmp_path / "git.lock"),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(stdout="issue-branch\n")),
        patch.object(
            WorkerPool,
            "_git_rebase_once",
            return_value=JobResult(ok=True, value={"head_sha": "a" * 40}),
        ) as replay,
    ):
        result = worker_factory()._git_rebase(job, record_source=MagicMock())
    assert not result.ok
    assert result.value == {"initial_implementation_ambiguous": True}
    replay.assert_not_called()


def test_fresh_creation_requires_local_remote_and_owner_absence(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A transport failure or existing owner cannot prove a first start."""
    pool = worker_factory()
    manager = MagicMock(repo_root=tmp_path)
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="create_worktree",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={"record_initial_creation": True, "issue_number": 7, "branch_name": "issue"},
    )
    for case in ("fresh", "local", "remote", "transport", "owner"):
        manager._read_receipt.return_value = MagicMock(generation=2) if case == "owner" else None
        with (
            patch.object(pool, "_authenticated_remote_git_configuration", return_value=({}, ())),
            patch(
                f"{WP}.git_utils.run",
                side_effect=[
                    MagicMock(returncode=0 if case == "local" else 1),
                    MagicMock(
                        returncode=1 if case == "transport" else 0,
                        stdout="a refs/heads/issue" if case == "remote" else "",
                    ),
                ],
            ),
        ):
            assert pool._fresh_initial_creation(job, manager) is (case == "fresh")


def test_manual_local_rebase_records_explicit_recovery(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Manual replay permits a later first-start check without another rebase."""
    state = tmp_path / "host-state"
    state.mkdir()
    manager = SimpleNamespace(
        state_dir=state, repository_identity="test/repo:local", common_dir=tmp_path
    )
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "issue_number": 7,
            "branch": "issue-branch",
            "rebase_reason": "manual",
            "publish_rebased_head": False,
            "expected_head_sha": "a" * 40,
        },
    )
    with (
        patch(f"{WP}.SourceWorkspaceManager", return_value=manager),
        patch(f"{WP}.WorktreeManager.git_metadata_lock_path", return_value=tmp_path / "git.lock"),
        patch.object(
            WorkerPool,
            "_git_rebase_once",
            return_value=JobResult(ok=True, value={"head_sha": "a" * 40}),
        ),
    ):
        pool = worker_factory()
        result = pool._git_rebase(job, record_source=MagicMock())
        assert pool._completed_initial_start(job)
    assert result.ok
    assert result.value["implementation_started"] is True


def test_linked_worktrees_have_separate_fetch_head(tmp_path: Path) -> None:
    """A fetch in another worktree must not replace the captured FETCH_HEAD."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str, cwd: Path = root) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-qm",
        "test: seed",
    )
    linked = tmp_path / "linked"
    git("worktree", "add", "--detach", str(linked), "HEAD")
    root_fetch = Path(git("rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"))
    linked_fetch = Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD", cwd=linked)
    )
    assert root_fetch != linked_fetch
    root_fetch.write_text("first fetch\n")
    linked_fetch.write_text("second fetch\n")
    assert root_fetch.read_text() == "first fetch\n"
