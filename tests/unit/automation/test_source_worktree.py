"""Lifecycle tests for deterministic source-reading worktrees."""

from __future__ import annotations

import queue
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import implementation_writer, worktree_manager
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.git_cleanup import run_cleanup_job
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages import (
    Disposition,
    ImplementationStage,
    StageContext,
    StageOutcome,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
    SourceWorkspaceRecoveryKind,
)
from hephaestus.automation.worktree_manager import (
    ImplementationWriterAuthority,
    WorktreeCreationReceiptError,
    WorktreeManager,
)
from hephaestus.utils.file_lock import LockUnavailableError, file_lock


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_many_preparations_reuse_exactly_two_named_worktrees(tmp_path: Path) -> None:
    """Retries and rounds reuse the two deterministic lane paths."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")

    for _ in range(5):
        impl = manager.prepare(42, SourceLane.IMPLEMENTATION, second)
        review = manager.prepare(42, SourceLane.REVIEW, first)

    assert impl.cwd.name == "auto-42-impl"
    assert review.cwd.name == "auto-42-review"
    paths = {
        Path(line.removeprefix("worktree ")).name
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }
    assert paths == {"repository", "auto-42-impl", "auto-42-review"}
    assert _git(repo, "branch", "--format=%(refname:short)").splitlines() == ["main"]


def test_review_rebinds_same_path_to_exact_revision(tmp_path: Path) -> None:
    """A changed review head rebinds in place without a review branch."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, first)

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.cwd == original.cwd
    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "HEAD") == second
    assert _git(rebound.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_review_rebinds_when_receipt_revision_does_not_match_physical_head(
    tmp_path: Path,
) -> None:
    """A clean detached lane is reusable only after proving its physical HEAD."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, second)
    _git(original.cwd, "reset", "--hard", first)

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "HEAD") == second


def test_review_rebinds_when_physical_checkout_is_attached(tmp_path: Path) -> None:
    """A review receipt cannot hide an attached physical checkout."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(9, SourceLane.REVIEW, second)
    _git(original.cwd, "switch", "-c", "wrong-review-branch")

    rebound = manager.prepare(9, SourceLane.REVIEW, second)

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_implementation_rebinds_when_physical_branch_is_wrong(tmp_path: Path) -> None:
    """An attached lane is reusable only on its expected physical branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )
    _git(original.cwd, "switch", "-c", "wrong-implementation-branch")

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )

    assert rebound.generation == original.generation + 1
    assert _git(rebound.cwd, "symbolic-ref", "HEAD") == "refs/heads/expected-implementation-branch"


def test_implementation_rebinds_clean_stale_branch_to_exact_revision(
    tmp_path: Path,
) -> None:
    """A clean implementation lane moves its branch to the requested revision."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="expected-implementation-branch",
    )

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="expected-implementation-branch",
    )

    physical_revision = _git(rebound.cwd, "rev-parse", "HEAD")
    assert rebound.cwd == original.cwd
    assert rebound.generation == original.generation + 1
    assert physical_revision == second
    assert rebound.revision == physical_revision
    assert _git(rebound.cwd, "symbolic-ref", "HEAD") == "refs/heads/expected-implementation-branch"


def test_implementation_preserves_branch_held_by_another_worktree(
    tmp_path: Path,
) -> None:
    """A branch that another worktree holds causes a safe typed failure."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    original = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="expected-implementation-branch",
    )
    _git(repo, "worktree", "remove", str(original.cwd))
    holder = tmp_path / "branch-holder"
    _git(repo, "worktree", "add", str(holder), "expected-implementation-branch")

    with pytest.raises(
        SourceWorkspaceError,
        match="source workspace branch could not be synchronized safely",
    ):
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="expected-implementation-branch",
        )

    assert _git(holder, "rev-parse", "HEAD") == first
    assert not original.cwd.exists()


def test_implementation_preserves_inactive_unowned_branch(tmp_path: Path) -> None:
    """A branch without a lane receipt cannot be reset during preparation."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    _git(repo, "branch", "unowned-implementation-branch", first)

    with pytest.raises(
        SourceWorkspaceError,
        match="source workspace branch is not owned by this lane",
    ):
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="unowned-implementation-branch",
        )

    assert _git(repo, "rev-parse", "unowned-implementation-branch") == first
    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_claim_implementation_writer_records_the_controlled_checkout(
    tmp_path: Path,
) -> None:
    """A deterministic, clean writer can become its source lane."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        binding = manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=authority,
            handoff=handoff,
        )

    rebound = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="writer-branch",
    )
    assert binding.cwd == writer
    assert binding.revision == second
    assert rebound == binding


def test_direct_writer_replaces_owned_detached_source_before_rebinding(
    tmp_path: Path,
) -> None:
    """A direct writer can replace its exact owned detached predecessor."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            base_sha=second,
            remote_branch_reserved=True,
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

    rebound = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="writer-branch",
    )
    assert writer == predecessor.cwd
    assert binding.revision == second
    assert rebound == binding


def test_direct_writer_replaces_owned_attached_source_before_rebinding(
    tmp_path: Path,
) -> None:
    """A direct writer can replace an exact owned attached predecessor."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with source_manager.implementation_writer_handoff(9) as handoff:
        source_manager.authorize_direct_implementation_writer_transition(
            9,
            branch="writer-branch",
            base_sha=second,
            handoff=handoff,
        )
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            base_sha=second,
            remote_branch_reserved=True,
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

    assert writer == predecessor.cwd
    assert _git(repo, "rev-parse", "old-writer-branch") == first
    assert binding.revision == second
    assert (
        source_manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )
        == binding
    )


def test_direct_worker_preserves_existing_writer_without_receipt(tmp_path: Path) -> None:
    """A direct worker rejects an existing writer that has no receipt."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = source_manager.path_for(9, SourceLane.IMPLEMENTATION)
    writer.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(writer), first)
    assert not source_manager._receipt_path(9, SourceLane.IMPLEMENTATION).exists()

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "base_sha": second,
        },
    )

    with (
        patch.object(
            pool,
            "_prepare_direct_scope_worktree",
            return_value=(second, "writer-branch"),
        ),
        patch.object(
            pool,
            "_authenticated_remote_git_configuration",
            return_value=({}, ()),
        ),
        patch.object(WorktreeManager, "create_worktree", autospec=True) as create_worktree,
    ):
        pool.submit(job, StageName.REPO)
        _, result = completion_q.get(timeout=10)

    create_worktree.assert_not_called()
    assert result.ok is False
    assert result.error == (
        "source_workspace_ownership_unavailable: implementation writer predecessor is unproven"
    )
    assert isinstance(result.value, dict)
    recovery = result.value["source_workspace_recovery"]
    assert recovery["kind"] == "unproven_predecessor"
    assert recovery["item_number"] == 9
    assert recovery["path"] == str(writer)
    assert recovery["manual_action"]
    assert writer.exists()
    assert _git(writer, "rev-parse", "HEAD") == first
    assert _git(writer, "branch", "--show-current") == ""


@pytest.mark.parametrize("receipt_kind", ["malformed", "unreadable", "symlink"])
def test_direct_writer_receipt_read_failure_has_unproven_recovery(
    tmp_path: Path, receipt_kind: str
) -> None:
    """A receipt that cannot be read preserves the writer with recovery guidance."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    if receipt_kind == "malformed":
        receipt_path.write_text("not-json\n", encoding="utf-8")
    elif receipt_kind == "unreadable":
        receipt_path.write_bytes(b"\xff\n")
    else:
        replacement = tmp_path / "replacement-receipt.json"
        replacement.write_text("{}\n", encoding="utf-8")
        receipt_path.unlink()
        receipt_path.symlink_to(replacement)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR
    assert recovery.item_number == 9
    assert recovery.path == predecessor.cwd.resolve()
    assert recovery.receipt_path == receipt_path.absolute()
    assert "Inspect and preserve" in recovery.manual_action
    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == "refs/heads/old-writer-branch"


@pytest.mark.parametrize(
    "probe_error",
    [
        SourceWorkspaceError("probe failed"),
        OSError("probe failed"),
        subprocess.SubprocessError("probe failed"),
    ],
    ids=["workspace-error", "os-error", "subprocess-error"],
)
def test_direct_writer_probe_failure_has_unproven_recovery(
    tmp_path: Path, probe_error: Exception
) -> None:
    """A failed physical predecessor probe preserves the writer with recovery guidance."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)

    with manager.implementation_writer_handoff(9) as handoff:
        with (
            patch.object(
                manager,
                "_is_dirty",
                side_effect=probe_error,
            ),
            pytest.raises(SourceWorkspaceError) as captured,
        ):
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.UNPROVEN_PREDECESSOR
    assert recovery.item_number == 9
    assert recovery.path == predecessor.cwd.resolve()
    assert recovery.receipt_path == receipt_path.resolve()
    assert "Inspect and preserve" in recovery.manual_action
    assert predecessor.cwd.exists()


@pytest.mark.parametrize("receipt_kind", ["malformed", "unreadable", "symlink"])
def test_direct_worker_serializes_unproven_recovery_for_unreadable_receipt(
    tmp_path: Path, receipt_kind: str
) -> None:
    """A direct worker returns durable recovery when its predecessor receipt cannot be read."""
    repo, first, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = source_manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = source_manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    if receipt_kind == "malformed":
        receipt_path.write_text("not-json\n", encoding="utf-8")
    elif receipt_kind == "unreadable":
        receipt_path.write_bytes(b"\xff\n")
    else:
        replacement = tmp_path / "replacement-receipt.json"
        replacement.write_text("{}\n", encoding="utf-8")
        receipt_path.unlink()
        receipt_path.symlink_to(replacement)

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "base_sha": second,
        },
    )

    with (
        patch.object(
            pool,
            "_prepare_direct_scope_worktree",
            return_value=(second, "writer-branch"),
        ),
        patch.object(
            pool,
            "_authenticated_remote_git_configuration",
            return_value=({}, ()),
        ),
        patch.object(WorktreeManager, "create_worktree", autospec=True) as create_worktree,
    ):
        pool.submit(job, StageName.REPO)
        _, result = completion_q.get(timeout=10)

    create_worktree.assert_not_called()
    assert result.ok is False
    assert result.error == (
        "source_workspace_ownership_unavailable: implementation writer predecessor is unproven"
    )
    assert isinstance(result.value, dict)
    recovery = result.value["source_workspace_recovery"]
    assert recovery["kind"] == "unproven_predecessor"
    assert recovery["item_number"] == 9
    assert recovery["path"] == str(predecessor.cwd)
    assert recovery["receipt_path"] == str(receipt_path)
    assert "Inspect and preserve" in recovery["manual_action"]
    assert predecessor.cwd.exists()

    item = WorkItem(
        repo="example/project",
        kind=ItemKind.ISSUE,
        issue=9,
        stage=StageName.IMPLEMENTATION,
        state="WORKTREE_WAIT",
        branch="writer-branch",
    )
    context = StageContext(
        config=PipelineConfig(org="example", repos=["project"]),
        org="example",
        dry_run=False,
        github=cast(Any, MagicMock()),
        paths=SimpleNamespace(repo_root=repo),
    )
    stage = ImplementationStage()
    stage.on_job_done(item, result, context)
    item.state = "DIRTY_DECISION_WAIT"

    outcome = stage.step(item, context)

    assert outcome == StageOutcome(
        Disposition.FINISH_FAIL,
        f"source_workspace_ownership:unproven_predecessor: {recovery['manual_action']}",
    )
    assert item.payload["source_workspace_recovery"] == recovery


def test_direct_writer_preserves_attached_source_with_durable_obligations(
    tmp_path: Path,
) -> None:
    """A stale attached writer with obligations stays preserved."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    manager.add_obligation(9, SourceLane.IMPLEMENTATION, "durable-cleanup")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(
            SourceWorkspaceError,
            match="predecessor has durable obligations",
        ) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    recovery = captured.value.recovery
    assert recovery is not None
    assert recovery.kind is SourceWorkspaceRecoveryKind.DURABLE_OBLIGATIONS
    assert "owning pipeline" in recovery.manual_action
    assert predecessor.cwd.exists()
    assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == ("refs/heads/old-writer-branch")
    receipt = manager._read_receipt(9, SourceLane.IMPLEMENTATION)
    assert receipt is not None
    assert receipt.obligations == ("durable-cleanup",)


def test_direct_writer_checkout_change_after_authorization_has_recovery(
    tmp_path: Path,
) -> None:
    """A changed predecessor remains preserved with a typed recovery action."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        original_validate = type(handoff)._validate_direct_transition

        def validate_then_change(
            active_handoff: Any,
            **kwargs: Any,
        ) -> None:
            original_validate(active_handoff, **kwargs)
            _git(predecessor.cwd, "reset", "--hard", second)

        with patch.object(
            type(handoff),
            "_validate_direct_transition",
            autospec=True,
            side_effect=validate_then_change,
        ):
            with pytest.raises(WorktreeCreationReceiptError) as captured:
                worktree_manager.create_worktree(
                    9,
                    "writer-branch",
                    base_sha=second,
                    remote_branch_reserved=True,
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_writer_handoff=handoff,
                )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "implementation writer direct transition is invalid"


@pytest.mark.parametrize("mutation", ["dirty", "branch", "revision"])
def test_direct_writer_preserves_checkout_changed_after_transition_consumption(
    tmp_path: Path, mutation: str
) -> None:
    """A predecessor change after transition consumption prevents its removal."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        original_consume = type(handoff)._consume_direct_transition

        def consume_then_change(active_handoff: Any, **kwargs: Any) -> object:
            evidence = original_consume(active_handoff, **kwargs)
            if mutation == "dirty":
                (predecessor.cwd / "pending-change").write_text("preserve\n", encoding="utf-8")
            elif mutation == "branch":
                _git(predecessor.cwd, "switch", "-c", "unexpected-branch")
            else:
                _git(predecessor.cwd, "reset", "--hard", second)
            return evidence

        with patch.object(
            type(handoff),
            "_consume_direct_transition",
            autospec=True,
            side_effect=consume_then_change,
        ):
            with pytest.raises(WorktreeCreationReceiptError) as captured:
                writer_manager.create_worktree(
                    9,
                    "writer-branch",
                    base_sha=second,
                    remote_branch_reserved=True,
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_writer_handoff=handoff,
                )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    if mutation == "dirty":
        assert (predecessor.cwd / "pending-change").read_text(encoding="utf-8") == "preserve\n"
    elif mutation == "branch":
        assert _git(predecessor.cwd, "symbolic-ref", "HEAD") == "refs/heads/unexpected-branch"
    else:
        assert _git(predecessor.cwd, "rev-parse", "HEAD") == second


def test_direct_writer_failed_branch_probe_has_recovery(tmp_path: Path) -> None:
    """A failed predecessor branch probe returns a typed recovery action."""
    repo, first, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(repo, "push", "origin", "main:writer-branch")
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first, branch="old-writer-branch")
    writer_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    original_run = worktree_manager.__dict__["run"]

    def fail_branch_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "symbolic-ref", "--quiet", "HEAD"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="probe failed")
        return original_run(argv, **kwargs)

    with manager.implementation_writer_handoff(9) as handoff:
        manager.authorize_direct_implementation_writer_transition(
            9, branch="writer-branch", base_sha=second, handoff=handoff
        )
        with (
            patch.dict(worktree_manager.__dict__, {"run": fail_branch_probe}),
            pytest.raises(WorktreeCreationReceiptError) as captured,
        ):
            writer_manager.create_worktree(
                9,
                "writer-branch",
                base_sha=second,
                remote_branch_reserved=True,
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert captured.value.recovery is not None
    assert captured.value.recovery["kind"] == "unproven_predecessor"
    assert captured.value.recovery["item_number"] == 9
    assert captured.value.recovery["path"] == str(predecessor.cwd)
    manual_action = captured.value.recovery["manual_action"]
    assert isinstance(manual_action, str)
    assert "Inspect it before cleanup" in manual_action


@pytest.mark.parametrize("mutation", ["dirty", "attached", "revision-drift", "obligations"])
def test_direct_writer_transition_preserves_invalid_predecessor(
    tmp_path: Path, mutation: str
) -> None:
    """An invalid detached predecessor cannot arm a direct transition."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(9, SourceLane.IMPLEMENTATION, first)
    if mutation == "dirty":
        (predecessor.cwd / "pending-change").write_text("preserve\n", encoding="utf-8")
    elif mutation == "attached":
        _git(predecessor.cwd, "switch", "-c", "unexpected-branch")
    elif mutation == "obligations":
        manager.add_obligation(9, SourceLane.IMPLEMENTATION, "durable-cleanup")
    else:
        _git(predecessor.cwd, "reset", "--hard", second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError) as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    assert predecessor.cwd.exists()
    assert manager._read_receipt(9, SourceLane.IMPLEMENTATION) is not None
    assert captured.value.recovery is not None
    assert (
        captured.value.recovery.kind.value
        == {
            "dirty": "dirty_worktree",
            "attached": "branch_mismatch",
            "revision-drift": "revision_drift",
            "obligations": "durable_obligations",
        }[mutation]
    )
    assert captured.value.recovery.item_number == 9
    assert str(captured.value.recovery.path) == str(predecessor.cwd)
    assert str(captured.value.recovery.receipt_path).endswith("9-impl.json")


def test_direct_writer_transition_rejects_receipt_when_path_is_missing(
    tmp_path: Path,
) -> None:
    """A receipt without its checkout remains available for manual recovery."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    predecessor = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        first,
        branch="old-writer-branch",
    )
    receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "remove", str(predecessor.cwd))

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="receipt path is missing") as captured:
            manager.authorize_direct_implementation_writer_transition(
                9,
                branch="writer-branch",
                base_sha=second,
                handoff=handoff,
            )

    assert captured.value.recovery is not None
    assert captured.value.recovery.kind is SourceWorkspaceRecoveryKind.RECEIPT_PATH_MISSING
    assert receipt_path.exists()
    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_implementation_writer_handoff_cannot_be_forged_outside_manager(
    tmp_path: Path,
) -> None:
    """The handoff capability requires the issuer context manager."""
    assert not hasattr(implementation_writer, "_CONSTRUCTION_TOKEN")
    assert not hasattr(implementation_writer, "_new_implementation_writer_handoff")
    assert not hasattr(implementation_writer, "_set_implementation_writer_handoff_active")
    handoff_type: Any = implementation_writer.ImplementationWriterHandoff
    with pytest.raises(TypeError, match="issuer context manager"):
        handoff_type(tmp_path, 9, token=object())
    assert not hasattr(implementation_writer.ImplementationWriterHandoff, "_activate")

    repo, _, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
    )
    forged = object.__new__(implementation_writer.ImplementationWriterHandoff)
    object.__setattr__(forged, "_active", True)
    object.__setattr__(forged, "_construction_token", object())
    object.__setattr__(forged, "_item_number", 9)
    object.__setattr__(
        forged,
        "_lock_path",
        worktree_manager.source_lane_lock_path(repo, 9, "impl"),
    )
    object.__setattr__(forged, "_repo_root", repo.resolve())
    with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
        worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=forged,
        )


def test_implementation_writer_handoff_rejects_a_noncanonical_lock_path(
    tmp_path: Path,
) -> None:
    """A handoff issued on another lock cannot authorize writer creation."""
    repo, _, second = _repository(tmp_path)
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
    )
    wrong_lock = tmp_path / "not-the-source-lane.lock"

    with implementation_writer.implementation_writer_handoff(repo, 9, wrong_lock) as handoff:
        with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
            worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )


def test_implementation_writer_creation_requires_an_active_handoff(tmp_path: Path) -> None:
    """An implementation writer cannot allocate without its lane handoff."""
    repo, _, second = _repository(tmp_path)
    manager = WorktreeManager(repo_root=repo, base_branch=second)

    with pytest.raises(WorktreeCreationReceiptError, match="handoff"):
        manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
        )


def test_fresh_writer_claim_blocks_concurrent_adoption_until_claim_finishes(
    tmp_path: Path,
) -> None:
    """A fresh writer stays intact while a competing adoption waits for its claim."""
    repo, _, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    adopted_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    adoption_errors: list[Exception] = []
    contention_observed = threading.Event()
    adoption_mutation_started = threading.Event()
    adoption_finished = threading.Event()

    adopted_source = SourceWorkspaceManager(repo, repository="example/project")

    def adopt() -> None:
        try:
            with file_lock(
                adopted_source._lane_lock_path(9, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                raise AssertionError("source lease was not held")
        except LockUnavailableError:
            contention_observed.set()
        try:
            with adopted_source.implementation_writer_handoff(9) as adopted_handoff:
                adopted_manager.create_worktree(
                    9,
                    "writer-branch",
                    source_lane=SourceLane.IMPLEMENTATION.value,
                    implementation_adoption_head=second,
                    implementation_writer_handoff=adopted_handoff,
                )
        except Exception as exc:  # pragma: no cover - assertion below reports failures
            adoption_errors.append(exc)
        finally:
            adoption_finished.set()

    real_adoption = adopted_manager._add_authenticated_adopted_implementation_writer

    def observe_adoption(*args: Any, **kwargs: Any) -> None:
        adoption_mutation_started.set()
        real_adoption(*args, **kwargs)

    worker = threading.Thread(target=adopt)
    with patch.object(
        adopted_manager,
        "_add_authenticated_adopted_implementation_writer",
        side_effect=observe_adoption,
    ):
        with source_manager.implementation_writer_handoff(9) as handoff:
            writer = worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
            authority = worktree_manager.implementation_writer_authority(writer)
            _git(repo, "push", "origin", "writer-branch")
            worker.start()
            assert contention_observed.wait(timeout=5)
            assert not adoption_mutation_started.is_set()
            assert writer.exists()
            assert _git(writer, "rev-parse", "HEAD") == second

            source_manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

        assert adoption_mutation_started.wait(timeout=5)

    assert adoption_finished.wait(timeout=5)
    assert not adoption_errors


def test_worker_pool_adoption_waits_for_active_source_lease(
    tmp_path: Path,
) -> None:
    """WorkerPool adoption preserves a leased writer until the lease releases."""
    repo, _, second = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    source_manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=source_manager.base_dir,
        base_branch=second,
        remote_git_env={},
        remote_git_config=("-c", "credential.helper="),
    )
    with source_manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        binding = source_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )
    _git(repo, "push", "origin", "writer-branch")

    completion_q: CompletionQueue = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
    )
    contention_observed = threading.Event()
    adoption_mutation_started = threading.Event()
    sync_completed = threading.Event()
    real_handoff = SourceWorkspaceManager.implementation_writer_handoff

    @contextmanager
    def observed_handoff(manager: SourceWorkspaceManager, item_number: int):
        try:
            with file_lock(
                manager._lane_lock_path(item_number, SourceLane.IMPLEMENTATION),
                blocking=False,
                require_exclusive=True,
            ):
                raise AssertionError("source lease was not held")
        except LockUnavailableError:
            contention_observed.set()
        with real_handoff(manager, item_number) as handoff:
            yield handoff

    real_sync = pool._sync_worktree_to_remote_branch

    def observed_sync(*args: Any, **kwargs: Any) -> None:
        real_sync(*args, **kwargs)
        sync_completed.set()

    real_adoption = WorktreeManager._add_authenticated_adopted_implementation_writer

    def observed_adoption(manager: WorktreeManager, *args: Any, **kwargs: Any) -> None:
        adoption_mutation_started.set()
        real_adoption(manager, *args, **kwargs)

    job = GitJob(
        repo="example/project",
        op="create_worktree",
        timeout_s=60,
        kwargs={
            "issue_number": 9,
            "branch_name": "writer-branch",
            "repo_root": str(repo),
            "source_lane": SourceLane.IMPLEMENTATION.value,
            "sync_to_remote": True,
            "pr_number": 90,
            "implementation_adoption_head": second,
        },
    )
    try:
        with (
            patch.object(
                SourceWorkspaceManager,
                "implementation_writer_handoff",
                observed_handoff,
            ),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=({}, ("-c", "credential.helper=")),
            ) as remote_configuration,
            patch.object(pool, "_sync_worktree_to_remote_branch", observed_sync),
            patch.object(
                WorktreeManager,
                "_add_authenticated_adopted_implementation_writer",
                observed_adoption,
            ),
        ):
            with source_manager.acquire(binding):
                pool.submit(job, StageName.REPO)
                assert contention_observed.wait(timeout=5)
                assert not adoption_mutation_started.is_set()
                assert writer.exists()
                assert _git(writer, "rev-parse", "HEAD") == second
                assert _git(writer, "branch", "--show-current") == "writer-branch"

            _, result = completion_q.get(timeout=10)
            assert adoption_mutation_started.is_set()
            assert result.ok is True, (result.error, remote_configuration.call_args_list)
            assert sync_completed.is_set()
            assert result.value == {
                "path": str(writer),
                "impl_source_revision": second,
                "dirty": False,
                "status": "",
                "diff": "",
            }
        rebound = source_manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )
        assert rebound.cwd == binding.cwd
        assert rebound.revision == binding.revision
        assert rebound.generation == binding.generation + 1
    finally:
        pool.shutdown(mark_interrupted=False)


def test_claim_does_not_reacquire_the_active_lane_lock(tmp_path: Path) -> None:
    """Claim uses the caller's active handoff without nesting its file lock."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    lock_paths: list[Path] = []

    @contextmanager
    def tracked_lock(path: Path, **kwargs: Any):
        lock_paths.append(path)
        with file_lock(path, **kwargs):
            yield

    with patch("hephaestus.automation.implementation_writer.file_lock", side_effect=tracked_lock):
        with manager.implementation_writer_handoff(9) as handoff:
            writer = worktree_manager.create_worktree(
                9,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=worktree_manager.implementation_writer_authority(writer),
                handoff=handoff,
            )

    assert lock_paths == [manager._lane_lock_path(9, SourceLane.IMPLEMENTATION)]


def test_implementation_writer_rejects_wrong_or_inactive_handoff(tmp_path: Path) -> None:
    """A handoff for another item or an ended handoff cannot allocate a writer."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(WorktreeCreationReceiptError, match="handoff"):
            worktree_manager.create_worktree(
                10,
                "writer-branch",
                source_lane=SourceLane.IMPLEMENTATION.value,
                implementation_writer_handoff=handoff,
            )
    with pytest.raises(WorktreeCreationReceiptError, match="inactive"):
        worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )


def test_claim_implementation_writer_converts_receipt_write_error(
    tmp_path: Path,
) -> None:
    """A receipt write failure is an ownership error, not a retryable Git error."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        with (
            patch.object(manager, "_write_receipt", side_effect=OSError("disk full")),
            pytest.raises(
                SourceWorkspaceError, match="cannot record implementation writer receipt"
            ),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_authority_failure_preserves_existing_receipt(
    tmp_path: Path,
) -> None:
    """An authority failure does not alter an existing durable receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )

        with (
            patch.object(manager, "_write_receipt") as write_receipt,
            pytest.raises(SourceWorkspaceError, match="authority is invalid"),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    write_receipt.assert_not_called()
    assert (
        manager.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        ).cwd
        == writer
    )


def test_claim_implementation_writer_restart_cannot_adopt_unconsumed_authority(
    tmp_path: Path,
) -> None:
    """A stopped authority handoff leaves no durable writer ownership record."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)

        with (
            patch(
                "hephaestus.automation.source_worktree.consume_implementation_writer_authority",
                side_effect=RuntimeError("worker stopped"),
            ),
            pytest.raises(SourceWorkspaceError, match="authority is invalid"),
        ):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    restarted = SourceWorkspaceManager(repo, repository="example/project")
    assert not (restarted.state_dir / "9-impl.json").exists()
    with pytest.raises(SourceWorkspaceError, match="not owned by this lane"):
        restarted.prepare(
            9,
            SourceLane.IMPLEMENTATION,
            second,
            branch="writer-branch",
        )


def test_claim_implementation_writer_rejects_clean_unauthorized_worktree(
    tmp_path: Path,
) -> None:
    """A clean deterministic path is not writer evidence without an authority."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_constructed_authority(
    tmp_path: Path,
) -> None:
    """An opaque authority that the manager did not mint cannot claim a writer."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)
    constructed = cast(ImplementationWriterAuthority, object())

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=constructed,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_stale_authority(
    tmp_path: Path,
) -> None:
    """An authority from before a clean writer move cannot authorize the lane."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=manager.base_dir,
        base_branch=second,
    )
    with manager.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        authority = worktree_manager.implementation_writer_authority(writer)
        _git(writer, "reset", "--hard", first)

        with pytest.raises(SourceWorkspaceError, match="authority"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                authority=authority,
                handoff=handoff,
            )

    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_non_lane_path(tmp_path: Path) -> None:
    """A writer outside the deterministic lane cannot become source state."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    foreign_writer = tmp_path / "foreign-writer"
    _git(repo, "worktree", "add", "-b", "writer-branch", str(foreign_writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="does not match the deterministic lane"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=foreign_writer,
                handoff=handoff,
            )

    assert not manager.path_for(9, SourceLane.IMPLEMENTATION).exists()


def test_claim_implementation_writer_rejects_an_incompatible_receipt(
    tmp_path: Path,
) -> None:
    """A controlled handoff cannot replace a lane's recorded branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.prepare(
        9,
        SourceLane.IMPLEMENTATION,
        second,
        branch="previous-writer-branch",
    ).cwd
    _git(writer, "switch", "-c", "writer-branch")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="incompatible source workspace receipt"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "writer-branch"


def test_claim_implementation_writer_rejects_a_wrong_attached_branch(
    tmp_path: Path,
) -> None:
    """A deterministic path does not authorize a different writer branch."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "other-writer-branch", str(writer), second)

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="does not match the requested branch"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "other-writer-branch"
    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_preserves_a_dirty_writer(tmp_path: Path) -> None:
    """A dirty controlled writer remains available for recovery."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)
    dirty_file = writer / "recover-me.txt"
    dirty_file.write_text("preserve this\n", encoding="utf-8")

    with manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="dirty and preserved"):
            manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert dirty_file.read_text(encoding="utf-8") == "preserve this\n"
    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_foreign_receipt(tmp_path: Path) -> None:
    """A different repository identity cannot refresh a writer receipt."""
    repo, _, second = _repository(tmp_path)
    first = SourceWorkspaceManager(repo, repository="one/project")
    worktree_manager = WorktreeManager(
        repo_root=repo,
        base_dir=first.base_dir,
        base_branch=second,
    )
    with first.implementation_writer_handoff(9) as handoff:
        writer = worktree_manager.create_worktree(
            9,
            "writer-branch",
            source_lane=SourceLane.IMPLEMENTATION.value,
            implementation_writer_handoff=handoff,
        )
        first.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
            authority=worktree_manager.implementation_writer_authority(writer),
            handoff=handoff,
        )
    second_manager = SourceWorkspaceManager(repo, repository="two/project")

    with second_manager.implementation_writer_handoff(9) as handoff:
        with pytest.raises(SourceWorkspaceError, match="owned by another repository") as captured:
            second_manager.claim_implementation_writer(
                9,
                branch="writer-branch",
                path=writer,
                handoff=handoff,
            )

    assert _git(writer, "branch", "--show-current") == "writer-branch"
    assert captured.value.recovery is not None
    assert captured.value.recovery.kind is SourceWorkspaceRecoveryKind.FOREIGN_OWNER
    assert "one/project" in captured.value.recovery.manual_action


def test_current_review_lane_can_be_cleaned_by_pipeline_contract(tmp_path: Path) -> None:
    """A review lane created with the current deterministic name is removable."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(7, SourceLane.REVIEW, second)

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 7,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is True
    assert not binding.cwd.exists()
    assert not manager._receipt_path(7, SourceLane.REVIEW).exists()


def test_review_cleanup_reconciles_receipt_after_physical_cleanup(tmp_path: Path) -> None:
    """A valid stale review receipt is removed after physical cleanup."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(7, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(7, SourceLane.REVIEW)
    _git(repo, "worktree", "remove", str(binding.cwd))
    _git(repo, "worktree", "prune")

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 7,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is True
    assert not binding.cwd.exists()
    assert not receipt_path.exists()


def test_review_cleanup_without_receipt_removes_worktree_idempotently(tmp_path: Path) -> None:
    """A pre-binding review checkout can be removed twice without a receipt."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    path = manager.path_for(8, SourceLane.REVIEW)
    _git(repo, "worktree", "add", "--detach", str(path), second)
    assert not manager._receipt_path(8, SourceLane.REVIEW).exists()
    job = GitJob(
        repo="example/project",
        op="remove_worktree",
        timeout_s=60,
        kwargs={
            "worktree_path": str(path),
            "repo_root": str(repo),
            "issue_number": 8,
            "expected_head": second,
            "expected_detached": True,
            "source_lane": SourceLane.REVIEW.value,
        },
    )

    first = run_cleanup_job(job)
    second_result = run_cleanup_job(job)

    assert first.ok is True
    assert second_result.ok is True
    assert not path.exists()


def test_review_cleanup_rejects_invalid_present_receipt(tmp_path: Path) -> None:
    """A present receipt with a changed revision cannot use absent-receipt cleanup."""
    repo, first, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(9, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(9, SourceLane.REVIEW)

    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 9,
                "expected_head": first,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is False
    assert result.error == "source workspace receipt revision changed"
    assert binding.cwd.exists()
    assert receipt_path.exists()


def test_review_cleanup_receipt_error_names_operation_path_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt-removal error preserves its exact metadata path and cause."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(10, SourceLane.REVIEW, second)
    receipt_path = manager._receipt_path(10, SourceLane.REVIEW)
    original_unlink = Path.unlink

    def refuse_receipt_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == receipt_path:
            raise OSError("receipt access denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_receipt_unlink)
    result = run_cleanup_job(
        GitJob(
            repo="example/project",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(binding.cwd),
                "repo_root": str(repo),
                "issue_number": 10,
                "expected_head": second,
                "expected_detached": True,
                "source_lane": SourceLane.REVIEW.value,
            },
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "source workspace receipt removal failed" in result.error
    assert str(receipt_path) in result.error
    assert "receipt access denied" in result.error
    assert receipt_path.exists()


def test_dirty_lane_is_preserved_and_rejected(tmp_path: Path) -> None:
    """Failure state is never erased by preparation or cleanup."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    binding = manager.prepare(5, SourceLane.IMPLEMENTATION, second)
    (binding.cwd / "untracked.txt").write_text("recover me\n", encoding="utf-8")

    with pytest.raises(SourceWorkspaceError, match="dirty"):
        manager.prepare(5, SourceLane.IMPLEMENTATION, second)
    with pytest.raises(SourceWorkspaceError, match="dirty"):
        manager.cleanup(5, SourceLane.IMPLEMENTATION)

    assert (binding.cwd / "untracked.txt").read_text(encoding="utf-8") == "recover me\n"


def test_repository_identity_prevents_equal_number_collision(tmp_path: Path) -> None:
    """Repository-qualified ownership prevents cross-repository adoption."""
    repo, _, second = _repository(tmp_path)
    first_manager = SourceWorkspaceManager(repo, repository="one/project")
    second_manager = SourceWorkspaceManager(repo, repository="two/project")

    first = first_manager.prepare(6, SourceLane.REVIEW, second)

    with pytest.raises(SourceWorkspaceError, match="owned by another repository"):
        second_manager.prepare(6, SourceLane.REVIEW, second)

    assert first.cwd.name == "auto-6-review"
