"""Check the host rebase admission policy."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool

WP = "hephaestus.automation.pipeline.worker_pool"


@pytest.mark.parametrize("reason", [None, "", "behind", "dependency_sync"])
def test_rebase_requires_allowed_reason(tmp_path: Path, reason: str | None) -> None:
    """An unapproved reason must not start Git work."""
    pool = object.__new__(WorkerPool)
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        kwargs={"cwd": tmp_path, "rebase_reason": reason},
    )
    with patch.object(pool, "_authenticated_remote_revalidator") as remote:
        result = pool._git_rebase(job)
    assert not result.ok
    assert result.error == "rebase reason is not allowed"
    remote.assert_not_called()


def test_publication_refresh_does_not_rebase(tmp_path: Path) -> None:
    """A changed remote head must leave local history unchanged."""
    pool = object.__new__(WorkerPool)
    job = GitJob(
        repo="test/repo",
        op="commit_push",
        timeout_s=60,
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
) -> None:
    """Use one fetched commit and publish only when requested."""
    pool = object.__new__(WorkerPool)
    from hephaestus.automation.pipeline.jobs import JobResult

    head, base, rewritten = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
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
        result = pool._git_rebase_once(job)
    assert result.ok
    assert result.value == {"rebased": True, "published": publish, "head_sha": rewritten}
    assert rebase.call_args.kwargs["base_sha"] == base
    assert push.call_count == int(publish)
    if publish:
        assert push.call_args.args[1] == head


def test_manual_conflict_aborts_before_agent_restart(tmp_path: Path) -> None:
    """Return a pinned restart request after the host aborts a conflict."""
    from hephaestus.automation.pipeline.jobs import JobResult

    pool = object.__new__(WorkerPool)
    head, base = "a" * 40, "b" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
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
        result = pool._git_rebase_once(job)
    assert result.error == "rebase conflict restart required"
    assert result.value == {"rebase_restart_required": True, "base_sha": base, "head_sha": head}
    assert rebase.call_args.kwargs["preserve_conflicts"] is False
    push.assert_not_called()


@pytest.mark.parametrize("moved", ["source", "base"])
def test_conflict_restart_rejects_changed_input(tmp_path: Path, moved: str) -> None:
    """A changed source or base must stop the replay."""
    from hephaestus.automation.pipeline.jobs import JobResult

    pool = object.__new__(WorkerPool)
    head, base, changed = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
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
        result = pool._git_rebase_once(job)
    assert not result.ok
    rebase.assert_not_called()


def test_fetch_main_does_not_change_checkout(tmp_path: Path) -> None:
    """Fetch only the main ref and read its fetched commit."""
    pool = object.__new__(WorkerPool)
    job = GitJob(repo="test/repo", op="fetch_main", timeout_s=60, kwargs={"cwd": tmp_path})
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


def test_initial_conflict_continuation_does_not_publish(tmp_path: Path) -> None:
    """Finish an initial local rebase without a remote branch probe or push."""
    pool = object.__new__(WorkerPool)
    head, base, changed = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="continue_rebase",
        timeout_s=60,
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
        result = pool._git_continue_rebase(job)
    assert result.ok
    assert result.value["published"] is False
    assert result.value["head_sha"] == changed
    remote.assert_not_called()
    push.assert_not_called()
