"""Behavior tests for portable worktree helper console commands."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hephaestus.github.skill_worktrees import (
    audit_worktrees_main,
    prepare_worktree_main,
    remove_worktree_main,
)


def _git(cwd: Path, *arguments: str) -> str:
    """Run a successful Git command in a test repository."""
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _initialize_repository(path: Path) -> None:
    """Create a minimal repository suitable for worktree tests."""
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Hephaestus Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "--quiet", "-m", "test: initial")


def test_prepare_worktree_creates_requested_path_from_attested_start_point(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Creation uses the caller's exact branch, path root, and base revision."""
    repository = tmp_path / "repo"
    worktree = tmp_path / "isolated" / "review"
    _initialize_repository(repository)
    start_sha = _git(repository, "rev-parse", "HEAD")
    monkeypatch.chdir(repository)

    assert (
        prepare_worktree_main(
            [
                "skill/review",
                "--path",
                str(worktree),
                "--path-root",
                str(worktree.parent),
                "--start-point",
                start_sha,
            ]
        )
        == 0
    )

    evidence = json.loads(capsys.readouterr().out)
    assert evidence == {
        "branch": "skill/review",
        "created": True,
        "path": str(worktree),
        "start_sha": start_sha,
    }
    assert _git(worktree, "rev-parse", "HEAD") == start_sha
    _git(repository, "worktree", "remove", "--force", str(worktree))


def test_prepare_worktree_rejects_a_symlinked_path_component(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The trusted path root is inspected lexically before creation."""
    repository = tmp_path / "repo"
    _initialize_repository(repository)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.chdir(repository)

    assert (
        prepare_worktree_main(
            [
                "skill/review",
                "--path",
                str(symlink_parent / "review"),
                "--path-root",
                str(symlink_parent),
                "--start-point",
                "HEAD",
            ]
        )
        == 1
    )
    assert "symlink" in capsys.readouterr().err


def test_audit_worktrees_reports_a_computable_inventory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The inventory exposes cleanliness, registered path, and current HEAD."""
    repository = tmp_path / "repo"
    _initialize_repository(repository)
    monkeypatch.chdir(repository)

    assert audit_worktrees_main([]) == 0

    records = json.loads(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["path"] == str(repository.resolve())
    assert records[0]["clean"] is True
    assert records[0]["head"] == _git(repository, "rev-parse", "HEAD")


def test_remove_worktree_requires_a_clean_expected_head(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Removal is refused after the audited worktree becomes dirty."""
    repository = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _initialize_repository(repository)
    _git(repository, "worktree", "add", "--quiet", "-b", "review", str(worktree))
    expected_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(repository)

    assert remove_worktree_main([str(worktree), "--expected-head", expected_head]) == 1

    assert "not clean" in capsys.readouterr().err
    _git(repository, "worktree", "remove", "--force", str(worktree))


def test_remove_worktree_removes_only_the_approved_clean_registration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A matching audited head authorizes removal without pruning other records."""
    repository = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _initialize_repository(repository)
    _git(repository, "worktree", "add", "--quiet", "-b", "review", str(worktree))
    expected_head = _git(worktree, "rev-parse", "HEAD")
    monkeypatch.chdir(repository)

    assert remove_worktree_main([str(worktree), "--expected-head", expected_head]) == 0

    assert not worktree.exists()
    assert f"removed {worktree} at {expected_head}" in capsys.readouterr().out
