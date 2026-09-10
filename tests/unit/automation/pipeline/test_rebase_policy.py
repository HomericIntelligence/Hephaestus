"""Check the host rebase admission policy."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.jobs import GitJob
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


@pytest.mark.parametrize("reason", [None, "", "behind", "dependency_sync"])
def test_rebase_requires_allowed_reason(
    tmp_path: Path, reason: str | None, worker_factory: Callable[[], WorkerPool]
) -> None:
    """An unapproved reason must not start Git work."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={"cwd": tmp_path, "rebase_reason": reason},
    )
    with patch.object(pool, "_authenticated_remote_revalidator") as remote:
        result = pool._git_rebase(job, record_source=MagicMock())
    assert not result.ok
    assert result.error == "rebase reason is not allowed"
    remote.assert_not_called()


def test_publication_refresh_does_not_rebase(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A changed remote head must leave local history unchanged."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="commit_push",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "writer_refresh": {
                "phase": "rebase",
                "source_sha": "a" * 40,
                "expected_remote_sha": "b" * 40,
            }
        },
    )
    with patch(f"{WP}.git_utils.run") as run:
        result = pool._refresh_writer_publication(job, tmp_path, "issue-branch")
    assert not result.ok
    assert result.value == {"writer_refresh_failure": "remote_changed"}
    run.assert_not_called()


@pytest.mark.parametrize(
    "reason,publish",
    [
        ("implementation_start", False),
        ("review_conflict", True),
        ("manual", True),
    ],
)
def test_admitted_rebase_uses_fetched_commit(
    tmp_path: Path,
    reason: str,
    publish: bool,
    worker_factory: Callable[[], WorkerPool],
) -> None:
    """Use one fetched commit and publish only when requested."""
    pool = worker_factory()
    from hephaestus.automation.pipeline.jobs import JobResult

    head, base, rewritten = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "rebase_reason": reason,
            "branch": "issue-branch",
            "publish_rebased_head": publish,
            "expected_remote_sha": head,
            "expected_head_sha": head,
        },
    )
    with (
        patch.object(pool, "_revalidate_review_conflict", return_value=None),
        patch.object(
            pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": base})
        ),
        patch.object(pool, "_sync_writer_to_expected_remote_head", return_value=None),
        patch.object(
            pool,
            "_read_publish_head",
            side_effect=[head, head, *([head] if reason == "review_conflict" else []), rewritten],
        ),
        patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(returncode=1)),
        patch(f"{WP}._required_git_signing_env", return_value={}),
        patch(f"{WP}.git_utils.rebase_worktree_onto", return_value=True) as rebase,
        patch(f"{WP}.git_utils.push_head_to_branch") as push,
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    assert result.ok
    assert result.value == {"rebased": True, "published": publish, "head_sha": rewritten}
    assert rebase.call_args.kwargs["base_sha"] == base
    assert push.call_count == int(publish)
    if publish:
        assert push.call_args.args[1] == head


def test_manual_conflict_aborts_before_agent_restart(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Return a pinned restart request after the host aborts a conflict."""
    from hephaestus.automation.pipeline.jobs import JobResult

    pool = worker_factory()
    head, base = "a" * 40, "b" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "rebase_reason": "manual",
            "expected_head_sha": head,
        },
    )
    with (
        patch.object(
            pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": base})
        ),
        patch.object(pool, "_read_publish_head", return_value=head),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(returncode=1)),
        patch(f"{WP}._required_git_signing_env", return_value={}),
        patch(f"{WP}.git_utils.rebase_worktree_onto", return_value=False) as rebase,
        patch(f"{WP}.git_utils.push_head_to_branch") as push,
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    assert result.error == "rebase conflict restart required"
    assert result.value == {"rebase_restart_required": True, "base_sha": base, "head_sha": head}
    assert rebase.call_args.kwargs["preserve_conflicts"] is False
    push.assert_not_called()


@pytest.mark.parametrize("moved", ["source", "base"])
def test_conflict_restart_rejects_changed_input(
    tmp_path: Path, moved: str, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A changed source or base must stop the replay."""
    from hephaestus.automation.pipeline.jobs import JobResult

    pool = worker_factory()
    head, base, changed = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "rebase_reason": "manual",
            "expected_head_sha": head,
            "expected_base_sha": base,
            "resolve_conflicts": True,
        },
    )
    with (
        patch.object(
            pool,
            "_git_fetch_main",
            return_value=JobResult(
                ok=True, value={"head_sha": changed if moved == "base" else base}
            ),
        ),
        patch.object(
            pool, "_read_publish_head", return_value=changed if moved == "source" else head
        ),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.rebase_worktree_onto") as rebase,
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    assert not result.ok
    rebase.assert_not_called()


def test_fetch_main_does_not_change_checkout(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Fetch only the main ref and read its fetched commit."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="fetch_main",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={"cwd": tmp_path},
    )
    with (
        patch.object(pool, "_authenticated_remote_git_configuration", return_value=({}, ())),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(stdout="a" * 40)) as run,
    ):
        result = pool._git_fetch_main(job)
    assert result.ok
    assert result.value == {"head_sha": "a" * 40}
    assert [c.args[0] for c in run.call_args_list] == [
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
    ]


def test_initial_conflict_continuation_does_not_publish(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Finish an initial local rebase without a remote branch probe or push."""
    pool = worker_factory()
    head, base, changed = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="continue_rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "branch": "issue-branch",
            "base_sha": base,
            "expected_remote_sha": head,
            "expected_head_sha": head,
            "publish_rebased_head": False,
            "conflict_paths": ("file.txt",),
            "conflict_snapshot": {"file.txt": "before"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": base,
        },
    )
    with (
        patch.object(pool, "_read_remote_branch_head") as remote,
        patch.object(pool, "_validate_rebase_conflict_edits", return_value=None),
        patch.object(pool, "_continue_rebase_process", return_value=None),
        patch.object(pool, "_select_rebase_policy", return_value=None),
        patch.object(pool, "_run_rebase_structural_validation", return_value=None),
        patch.object(pool, "_validate_rebased_tree", return_value=None),
        patch.object(pool, "_verify_rebased_commit_metadata", return_value=None),
        patch.object(pool, "_read_publish_head", return_value=changed),
        patch(f"{WP}.git_utils.push_head_to_branch") as push,
    ):
        result = pool._git_continue_rebase(job, record_source=MagicMock())
    assert result.ok
    assert result.value["published"] is False
    assert result.value["head_sha"] == changed
    remote.assert_not_called()
    push.assert_not_called()


@pytest.mark.parametrize("failure", [None, "tree_changed", "remote_changed", "audit_missing"])
def test_published_rebase_returns_separate_review_proof(
    tmp_path: Path,
    failure: str | None,
    worker_factory: Callable[[], WorkerPool],
) -> None:
    """A checked rebase keeps the old review head apart from the new head."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.review_audit import ReviewAudit

    pool = worker_factory()
    from hephaestus.automation.pipeline.github_jobs import (
        PublishRebaseReviewRequest,
        RebaseReviewInspected,
        RebaseReviewPublished,
    )

    def run(job: Any, *, shutdown: Event | None = None, deadline_s: float | None = None) -> object:
        del shutdown, deadline_s
        return (
            RebaseReviewPublished(job.request, True)
            if isinstance(job.request, PublishRebaseReviewRequest)
            else RebaseReviewInspected(job.request, True)
        )

    pool._github_job_runner = MagicMock(gh_timeout=60, run=run)
    head, base, rewritten, tree = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    audit = ReviewAudit("A", "Checks passed.", (), "", True, "GO")
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "rebase_reason": "review_conflict",
            "branch": "issue-branch",
            "pr_number": 8,
            "issue_number": 7,
            "publish_rebased_head": True,
            "expected_remote_sha": head,
            "reviewed_head_sha": head,
            "reviewed_base_sha": "e" * 40,
            "review_audit": None if failure == "audit_missing" else audit,
        },
    )
    with (
        patch.object(pool, "_revalidate_review_conflict", return_value=None),
        patch.object(
            pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": base})
        ),
        patch.object(pool, "_sync_writer_to_expected_remote_head", return_value=None),
        patch.object(pool, "_read_publish_head", side_effect=[head, head, head, rewritten]),
        patch.object(
            pool,
            "_read_remote_branch_head",
            return_value=head if failure == "remote_changed" else rewritten,
        ),
        patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(
            f"{WP}.git_utils.run",
            side_effect=lambda cmd, **kw: MagicMock(
                returncode=1 if "--is-ancestor" in cmd else 0,
                stdout=("f" * 40 if failure == "tree_changed" and "rev-parse" in cmd else tree)
                + "\n",
            ),
        ),
        patch(f"{WP}._required_git_signing_env", return_value={}),
        patch(f"{WP}.git_utils.rebase_worktree_onto", return_value=True),
        patch(f"{WP}.git_utils.push_head_to_branch"),
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    if failure is not None:
        assert not result.ok
        assert result.error
        return
    assert result.ok
    assert "retained_rebase_review_proof" in result.value
    proof = result.value["retained_rebase_review_proof"]
    assert proof.reviewed_head_sha == head
    assert proof.resulting_head_sha == rewritten
    assert proof.resulting_tree_sha == tree
    assert proof.target_base_sha == base


@pytest.mark.parametrize("publication_ok", [True, False])
def test_review_record_is_visible_before_the_rebase_push(
    tmp_path: Path,
    publication_ok: bool,
    worker_factory: Callable[[], WorkerPool],
) -> None:
    """A crash after push must leave a durable record for recovery."""
    from hephaestus.automation.pipeline.jobs import JobResult
    from hephaestus.automation.review_audit import ReviewAudit

    pool = worker_factory()
    events: list[str] = []

    def publish(
        job: Any, *, shutdown: Event | None = None, deadline_s: float | None = None
    ) -> object:
        del shutdown, deadline_s
        from hephaestus.automation.pipeline.github_jobs import (
            InspectRebaseReviewRequest,
            RebaseReviewInspected,
            RebaseReviewPublished,
        )

        if isinstance(job.request, InspectRebaseReviewRequest):
            return RebaseReviewInspected(job.request, True)
        events.append("record")
        return RebaseReviewPublished(job.request, publication_ok)

    pool._github_job_runner = MagicMock(gh_timeout=60, run=publish)
    head, base, rewritten, tree = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    job = GitJob(
        repo="repo",
        expected_repository="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "repo_root": str(tmp_path),
            "rebase_reason": "review_conflict",
            "branch": "issue-branch",
            "pr_number": 8,
            "issue_number": 7,
            "publish_rebased_head": True,
            "expected_remote_sha": head,
            "reviewed_head_sha": head,
            "reviewed_base_sha": "e" * 40,
            "review_audit": ReviewAudit("A", "Checks passed.", (), "", True, "GO"),
        },
    )
    with (
        patch.object(pool, "_revalidate_review_conflict", return_value=None),
        patch.object(
            pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": base})
        ),
        patch.object(pool, "_sync_writer_to_expected_remote_head", return_value=None),
        patch.object(pool, "_read_publish_head", side_effect=[head, head, head, rewritten]),
        patch.object(pool, "_read_remote_branch_head", return_value=rewritten),
        patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(
            f"{WP}.git_utils.run",
            side_effect=lambda cmd, **kw: MagicMock(
                returncode=1 if "--is-ancestor" in cmd else 0, stdout=tree + "\n"
            ),
        ),
        patch(f"{WP}._required_git_signing_env", return_value={}),
        patch(f"{WP}.git_utils.rebase_worktree_onto", return_value=True),
        patch(
            f"{WP}.git_utils.push_head_to_branch",
            side_effect=lambda *a, **kw: events.append("push"),
        ),
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    assert result.ok is publication_ok
    assert events == (["record", "push"] if publication_ok else ["record"])
