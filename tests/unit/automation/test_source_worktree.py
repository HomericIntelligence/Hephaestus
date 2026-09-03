"""Lifecycle tests for deterministic source-reading worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.pipeline.git_cleanup import run_cleanup_job
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspaceManager,
)


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
    writer = manager.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)

    binding = manager.claim_implementation_writer(
        9,
        branch="writer-branch",
        path=writer,
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


def test_claim_implementation_writer_rejects_a_non_lane_path(tmp_path: Path) -> None:
    """A writer outside the deterministic lane cannot become source state."""
    repo, _, second = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    foreign_writer = tmp_path / "foreign-writer"
    _git(repo, "worktree", "add", "-b", "writer-branch", str(foreign_writer), second)

    with pytest.raises(SourceWorkspaceError, match="does not match the deterministic lane"):
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=foreign_writer,
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

    with pytest.raises(SourceWorkspaceError, match="incompatible source workspace receipt"):
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
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

    with pytest.raises(SourceWorkspaceError, match="does not match the requested branch"):
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
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

    with pytest.raises(SourceWorkspaceError, match="dirty and preserved"):
        manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
        )

    assert dirty_file.read_text(encoding="utf-8") == "preserve this\n"
    assert not (manager.state_dir / "9-impl.json").exists()


def test_claim_implementation_writer_rejects_a_foreign_receipt(tmp_path: Path) -> None:
    """A different repository identity cannot refresh a writer receipt."""
    repo, _, second = _repository(tmp_path)
    first = SourceWorkspaceManager(repo, repository="one/project")
    writer = first.path_for(9, SourceLane.IMPLEMENTATION)
    _git(repo, "worktree", "add", "-b", "writer-branch", str(writer), second)
    first.claim_implementation_writer(9, branch="writer-branch", path=writer)
    second_manager = SourceWorkspaceManager(repo, repository="two/project")

    with pytest.raises(SourceWorkspaceError, match="owned by another repository"):
        second_manager.claim_implementation_writer(
            9,
            branch="writer-branch",
            path=writer,
        )

    assert _git(writer, "branch", "--show-current") == "writer-branch"


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
            },
        )
    )

    assert result.ok is True
    assert not binding.cwd.exists()


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
