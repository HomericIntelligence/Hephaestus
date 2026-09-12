"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches and timeouts."""

import argparse
import asyncio
import importlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from hephaestus.github.tidy import (
    _detect_default_branch,
    _in_git_repo,
    _repo_root,
    _working_tree_clean,
    parse_problem_branches,
)

tidy_module = importlib.import_module("hephaestus.github.tidy")


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("git@github.com:HomericIntelligence/Hephaestus.git", "HomericIntelligence/Hephaestus"),
        ("https://github.com/HomericIntelligence/Scylla.git", "HomericIntelligence/Scylla"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://gitlab.com/owner/repo.git", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_repo_from_remote(remote_url: str | None, expected: str | None) -> None:
    """Read the GitHub repository name from supported origin URLs."""
    with patch.object(tidy_module, "git_remote_url", return_value=remote_url):
        assert tidy_module._detect_repo_from_remote() == expected


def test_detect_repo_from_remote_returns_none_on_lookup_error() -> None:
    """An origin lookup error leaves the repository name unknown."""
    with patch.object(tidy_module, "git_remote_url", side_effect=RuntimeError("git not found")):
        assert tidy_module._detect_repo_from_remote() is None


WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123-finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
        "worktree /repo/.worktrees/topic",
        "HEAD 789abc",
        "branch refs/heads/topic",
        "",
        "worktree /repo/.worktrees/detached",
        "HEAD deadbeef",
        "detached",
        "",
        "worktree /repo/bare",
        "bare",
        "",
    )
)

SPACED_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123 finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
    )
)

LOCKED_SPACED_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123 finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "locked",
        "",
    )
)

PRUNABLE_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/build/.worktrees/123-finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "prunable gitdir file points to non-existent location",
        "",
    )
)

NEWLINE_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123\nfinished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
    )
)


def _candidate_worktree_porcelain(
    root: Path,
    path: Path,
    branch: str,
    *,
    head: str = "123456",
    locked: bool = False,
) -> str:
    """Build a primary worktree and one attached cleanup candidate."""
    return "\0".join(
        (
            f"worktree {root}",
            "HEAD abcdef",
            "branch refs/heads/main",
            "",
            f"worktree {path}",
            f"HEAD {head}",
            f"branch refs/heads/{branch}",
            *(("locked",) if locked else ()),
            "",
        )
    )


def _branch_delete_transaction_input(
    branch: str,
    expected_head: str,
    trunk: str,
    trunk_head: str,
) -> str:
    """Build the conditional branch-deletion transaction sent to Git."""
    return "\0".join(
        (
            "start",
            f"verify refs/heads/{trunk}",
            trunk_head,
            f"delete refs/heads/{branch}",
            expected_head,
            "prepare",
            "commit",
            "",
        )
    )


def test_tidy_sdk_preserves_explicit_model() -> None:
    """Forward the model name and omit the effort for Claude."""
    factory = MagicMock()
    tidy_module._claude_options(factory, Path("/repo"), "My-Model:max")
    assert factory.call_args.kwargs["model"] == "My-Model"


@pytest.mark.parametrize("model", ["", ":default"])
def test_tidy_sdk_uses_configured_default(model: str) -> None:
    """Do not supply a model when the operator omits it."""
    factory = MagicMock()
    tidy_module._claude_options(factory, Path("/repo"), model)
    assert "model" not in factory.call_args.kwargs


# Fixture: clean gh-tidy run (no problem branches)
CLEAN_OUTPUT = """\
Checking out main and pulling the latest from remote origin...
Finished tidying!
"""

# Fixture: one problem branch
ONE_PROBLEM = """\
Rebasing ALL local branches on to latest master...
Rebasing feature/my-branch...
WARNING: Problem rebasing feature/my-branch
Finished rebasing!

Cleaning unnecessary files & optimizing your local repo...
WARNING: Unable to auto-rebase the following branches:
    * feature/my-branch

Finished tidying!
"""

# Fixture: multiple problem branches
MULTI_PROBLEM = """\
WARNING: Unable to auto-rebase the following branches:
    * feature/alpha
    * fix/beta-crash
    * chore/deps-update

Finished tidying!
"""

# Fixture: ANSI-coloured output (gh-tidy emits \e[93m yellow for warnings)
ANSI_PROBLEM = (
    "\x1b[93mWARNING: Unable to auto-rebase the following branches:\x1b[0m\n"
    "\x1b[93m    * feature/with-ansi\x1b[0m\n"
    "\x1b[92mFinished tidying!\x1b[0m\n"
)

# Fixture: problem header with no bullets (edge case — header present, no branch listed)
EMPTY_PROBLEM_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:

Finished tidying!
"""

# Fixture: problem header where a non-bullet line immediately follows
TRAILING_TEXT_AFTER_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:
    * chore/broken
Please fix manually.
Finished tidying!
"""


def test_clean_output_returns_empty() -> None:
    """No problem branches when output is a clean run."""
    assert parse_problem_branches(CLEAN_OUTPUT) == []


def test_single_problem_branch() -> None:
    """Single problem branch is extracted correctly."""
    result = parse_problem_branches(ONE_PROBLEM)
    assert result == ["feature/my-branch"]


def test_multiple_problem_branches() -> None:
    """All branches listed under the warning header are returned."""
    result = parse_problem_branches(MULTI_PROBLEM)
    assert result == ["feature/alpha", "fix/beta-crash", "chore/deps-update"]


def test_ansi_codes_stripped() -> None:
    """ANSI escape sequences are stripped before parsing."""
    result = parse_problem_branches(ANSI_PROBLEM)
    assert result == ["feature/with-ansi"]


def test_empty_problem_block() -> None:
    """Warning header with no bullet lines returns empty list."""
    result = parse_problem_branches(EMPTY_PROBLEM_BLOCK)
    assert result == []


def test_trailing_text_terminates_block() -> None:
    """Non-bullet line after the branch list terminates parsing."""
    result = parse_problem_branches(TRAILING_TEXT_AFTER_BLOCK)
    assert result == ["chore/broken"]


def test_no_problem_header_at_all() -> None:
    """Output with no warning header returns empty list."""
    result = parse_problem_branches("Finished tidying!\n")
    assert result == []


def test_run_gh_tidy_rebases_and_auto_deletes_merged_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gh-tidy boundary uses the complete unattended cleanup argv."""
    process = MagicMock()
    process.stdout = iter(())
    process.returncode = 0
    popen = MagicMock()
    popen.return_value.__enter__.return_value = process
    monkeypatch.setattr(tidy_module.subprocess, "Popen", popen)

    assert tidy_module._run_gh_tidy("main", dry_run=False) == (0, "")

    popen.assert_called_once_with(
        [
            "gh",
            "tidy",
            "--rebase-all",
            "--auto-delete-merged",
            "--trunk",
            "main",
            "--skip-gc",
        ],
        stdin=tidy_module.sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=ANY,
    )
    process.wait.assert_called_once_with()


def test_parse_worktree_porcelain_skips_main_and_detached_worktrees() -> None:
    """Cleanup candidates require a non-main worktree with an attached branch."""
    assert hasattr(tidy_module, "_parse_worktree_porcelain")
    assert tidy_module._parse_worktree_porcelain(WORKTREE_PORCELAIN, Path("/repo")) == [
        (Path("/repo/.worktrees/123-finished"), "123-finished"),
        (Path("/repo/.worktrees/topic"), "topic"),
    ]


def test_parse_worktree_porcelain_skips_primary_from_linked_worktree() -> None:
    """The primary worktree is never a cleanup candidate from a linked worktree."""
    assert tidy_module._parse_worktree_porcelain(
        WORKTREE_PORCELAIN,
        Path("/repo/.worktrees/topic"),
    ) == [(Path("/repo/.worktrees/123-finished"), "123-finished")]


def test_worktree_porcelain_requests_nul_terminated_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree discovery requests Git's delimiter-safe porcelain format."""
    result = subprocess.CompletedProcess([], 0, stdout="inventory", stderr="")
    run_git = MagicMock(return_value=result)
    monkeypatch.setattr(tidy_module, "run_git", run_git)

    assert tidy_module._worktree_porcelain() == "inventory"
    run_git.assert_called_once_with(
        ["worktree", "list", "--porcelain", "-z"],
        check=False,
        log_on_error=False,
    )


def test_parse_worktree_porcelain_preserves_space_in_path() -> None:
    """A worktree path containing spaces remains paired with its branch."""
    assert tidy_module._parse_worktree_porcelain(
        SPACED_WORKTREE_PORCELAIN,
        Path("/repo"),
    ) == [(Path("/repo/.worktrees/123 finished"), "123-finished")]


def test_parse_worktree_porcelain_preserves_newline_in_nul_path() -> None:
    """NUL output preserves a newline-containing worktree path."""
    assert tidy_module._parse_worktree_porcelain(
        NEWLINE_WORKTREE_PORCELAIN,
        Path("/repo"),
    ) == [(Path("/repo/.worktrees/123\nfinished"), "123-finished")]


@pytest.mark.parametrize(
    ("second_path", "second_branch"),
    [
        ("/repo/.worktrees/123-finished", "replacement"),
        ("/repo/.worktrees/replacement", "123-finished"),
    ],
)
def test_parse_worktree_records_rejects_duplicate_cleanup_identity(
    second_path: str,
    second_branch: str,
) -> None:
    """Duplicate paths or attached branches make the inventory unsafe."""
    porcelain = "\0".join(
        (
            "worktree /repo",
            "HEAD abcdef",
            "branch refs/heads/main",
            "",
            "worktree /repo/.worktrees/123-finished",
            "HEAD 123456",
            "branch refs/heads/123-finished",
            "",
            f"worktree {second_path}",
            "HEAD 654321",
            f"branch refs/heads/{second_branch}",
            "",
        )
    )

    with pytest.raises(tidy_module.WorktreeInventoryError, match="malformed"):
        tidy_module._parse_worktree_records(porcelain)


def test_parse_worktree_records_rejects_primary_parent_path_alias(tmp_path: Path) -> None:
    """A parent-component alias of the primary worktree is unsafe."""
    primary = tmp_path / "primary"
    primary.mkdir()
    primary_alias = primary / "child" / ".."
    porcelain = _candidate_worktree_porcelain(primary, primary_alias, "123-finished")

    with pytest.raises(tidy_module.WorktreeInventoryError, match="malformed"):
        tidy_module._parse_worktree_records(porcelain)


def test_cleanup_rejects_current_worktree_symlink_alias_before_classification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A symlink alias of the current worktree cannot become a cleanup candidate."""
    primary = tmp_path / "primary"
    current = tmp_path / "current"
    alias = tmp_path / "current-alias"
    primary.mkdir()
    current.mkdir()
    try:
        alias.symlink_to(current, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    porcelain = "\0".join(
        (
            f"worktree {primary}",
            "HEAD abcdef",
            "branch refs/heads/main",
            "",
            f"worktree {current}",
            "HEAD 123456",
            "branch refs/heads/topic",
            "",
            f"worktree {alias}",
            "HEAD 654321",
            "branch refs/heads/123-finished",
            "",
        )
    )
    issue_is_closed = MagicMock()
    branch_is_merged = MagicMock()
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", issue_is_closed)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)

    with pytest.raises(tidy_module.WorktreeInventoryError, match="malformed"):
        tidy_module._cleanup_stale_worktrees(current, "main", dry_run=False)

    issue_is_closed.assert_not_called()
    branch_is_merged.assert_not_called()


def test_cleanup_rejects_malformed_inventory_without_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed inventory stops cleanup before state classification."""
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: "worktree /repo\0HEAD abcdef\0\0unexpected\0",
    )
    branch_is_merged = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    with pytest.raises(tidy_module.WorktreeInventoryError, match="malformed"):
        tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=False)
    branch_is_merged.assert_not_called()
    remove.assert_not_called()


def test_cleanup_stale_worktrees_dry_run_reports_closed_issue_without_removing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run reports a closed-issue worktree and never invokes git removal."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    if not hasattr(tidy_module, "_cleanup_stale_worktrees"):
        pytest.fail("tidy does not yet implement stale-worktree cleanup")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert hasattr(tidy_module, "_cleanup_stale_worktrees")
    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=True) == 0
    assert "Would remove stale worktree" in caplog.text
    remove.assert_not_called()


def test_cleanup_stale_worktree_with_space_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run evaluates and reports the complete space-containing path."""
    spaced_path = tmp_path / "123 finished"
    spaced_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, spaced_path, "123-finished")
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: porcelain,
    )
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    is_dirty = MagicMock(return_value=False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=True) == 0
    is_dirty.assert_called_once_with(spaced_path)
    assert str(spaced_path) in caplog.text
    assert "123-finished" in caplog.text
    remove.assert_not_called()


def test_cleanup_stale_worktree_with_newline_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run evaluates the complete newline-containing path."""
    newline_path = tmp_path / "123\nfinished"
    newline_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, newline_path, "123-finished")
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: porcelain,
    )
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    is_dirty = MagicMock(return_value=False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=True) == 0
    is_dirty.assert_called_once_with(newline_path)
    assert str(newline_path) in caplog.text
    assert "123-finished" in caplog.text
    remove.assert_not_called()


def test_cleanup_skips_locked_worktree_with_space_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A locked spaced-path worktree is skipped without removal."""
    spaced_path = tmp_path / "123 finished"
    spaced_path.mkdir()
    porcelain = "\0".join(
        (
            f"worktree {tmp_path}",
            "HEAD abcdef",
            "branch refs/heads/main",
            "",
            f"worktree {spaced_path}",
            "HEAD 123456",
            "branch refs/heads/123-finished",
            "locked",
            "",
        )
    )
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: porcelain,
    )
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert f"Skipping locked worktree {spaced_path}" in caplog.text
    remove.assert_not_called()


def test_cleanup_skips_prunable_worktree_without_touching_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prunable record is not a cleanup target."""
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: PRUNABLE_WORKTREE_PORCELAIN,
    )
    is_dirty = MagicMock(side_effect=AssertionError("must not inspect a prunable path"))
    prompt = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=False) == 1
    is_dirty.assert_not_called()
    prompt.assert_not_called()
    remove.assert_not_called()


def test_cleanup_reports_prunable_worktree_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prunable registration requires separate recovery before cleanup."""
    caplog.set_level("ERROR", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: PRUNABLE_WORKTREE_PORCELAIN,
    )

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=False) == 1
    assert "prunable worktree" in caplog.text


def test_cleanup_reports_worktree_inside_git_metadata_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup does not remove a checkout that Git metadata contains."""
    metadata_path = tmp_path / ".git" / "worktrees" / "tidy-123-finished"
    metadata_path.mkdir(parents=True)
    porcelain = _candidate_worktree_porcelain(tmp_path, metadata_path, "123-finished")
    is_dirty = MagicMock(side_effect=AssertionError("must not inspect Git metadata"))
    caplog.set_level("ERROR", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Git metadata" in caplog.text
    is_dirty.assert_not_called()


def test_cleanup_reports_common_git_metadata_worktree_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup protects a shared Git directory from a linked worktree."""
    linked_root = tmp_path / "linked"
    linked_root.mkdir()
    common_git_dir = tmp_path / "primary" / ".git"
    linked_git_dir = common_git_dir / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked_root / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    metadata_path = common_git_dir / "worktrees" / "tidy-123-finished"
    metadata_path.mkdir(parents=True)
    porcelain = _candidate_worktree_porcelain(linked_root, metadata_path, "123-finished")
    is_dirty = MagicMock(side_effect=AssertionError("must not inspect Git metadata"))
    caplog.set_level("ERROR", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)

    assert tidy_module._cleanup_stale_worktrees(linked_root, "main", dry_run=False) == 1
    assert "Git metadata" in caplog.text
    is_dirty.assert_not_called()


def test_cleanup_rejects_worktree_under_foreign_git_metadata_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup rejects a path that resolves into another repository's Git metadata."""
    repo_path = tmp_path / "repo-a"
    foreign_metadata = tmp_path / "repo-b" / ".git" / "worktrees"
    (repo_path / ".git").mkdir(parents=True)
    foreign_metadata.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.parent.mkdir()
    try:
        agent_parent.symlink_to(foreign_metadata, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    candidate = agent_parent / "tidy-123-finished"
    candidate.mkdir()
    porcelain = _candidate_worktree_porcelain(repo_path, candidate, "123-finished")
    issue_is_closed = MagicMock(return_value=True)
    branch_is_merged = MagicMock(return_value=True)
    is_dirty = MagicMock(return_value=False)
    prompt = MagicMock(return_value="y")
    remove = MagicMock(return_value=False)
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", issue_is_closed)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(repo_path, "main", dry_run=False) == 1
    issue_is_closed.assert_not_called()
    branch_is_merged.assert_not_called()
    is_dirty.assert_not_called()
    prompt.assert_not_called()
    remove.assert_not_called()


def test_cleanup_rejects_direct_foreign_git_metadata_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup rejects a direct path in another repository Git metadata."""
    repo_path = tmp_path / "repo-a"
    foreign_path = tmp_path / "repo-b" / ".git" / "worktrees" / "tidy-123-finished"
    (repo_path / ".git").mkdir(parents=True)
    foreign_path.mkdir(parents=True)
    porcelain = _candidate_worktree_porcelain(repo_path, foreign_path, "123-finished")
    issue_is_closed = MagicMock(return_value=True)
    branch_is_merged = MagicMock(return_value=True)
    is_dirty = MagicMock(return_value=False)
    prompt = MagicMock(return_value="y")
    remove = MagicMock(return_value=False)
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", issue_is_closed)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(repo_path, "main", dry_run=False) == 1
    issue_is_closed.assert_not_called()
    branch_is_merged.assert_not_called()
    is_dirty.assert_not_called()
    prompt.assert_not_called()
    remove.assert_not_called()


def test_git_metadata_path_with_parent_components_fails_closed(tmp_path: Path) -> None:
    """A metadata path cannot bypass protection with parent components."""
    common_git_dir = tmp_path / ".git"
    candidate = common_git_dir / "worktrees" / "tidy" / ".." / ".." / ".." / "outside"

    assert not candidate.resolve().is_relative_to(common_git_dir.resolve())
    assert tidy_module._is_git_metadata_worktree_path(candidate, common_git_dir)


def test_cleanup_rechecks_metadata_before_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cleanup does not inspect a candidate that changes into Git metadata."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    metadata_path = tmp_path / ".git" / "worktrees" / "tidy-123-finished"
    metadata_path.mkdir(parents=True)
    porcelain = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    calls = 0

    def worktree_porcelain() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            worktree_path.rmdir()
            try:
                worktree_path.symlink_to(metadata_path, target_is_directory=True)
            except OSError as error:
                pytest.skip(f"Cannot create the test symlink: {error}")
        return porcelain

    is_dirty = MagicMock(side_effect=AssertionError("must not inspect Git metadata"))
    caplog.set_level("ERROR", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", worktree_porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Git metadata" in caplog.text
    is_dirty.assert_not_called()


def test_git_common_dir_rejects_linked_gitfile_without_commondir(tmp_path: Path) -> None:
    """A linked checkout with incomplete Git metadata fails closed."""
    linked_root = tmp_path / "linked"
    linked_root.mkdir()
    linked_git_dir = tmp_path / "primary" / ".git" / "worktrees" / "linked"
    linked_git_dir.mkdir(parents=True)
    (linked_root / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")

    with pytest.raises(tidy_module.WorktreeInventoryError, match="common Git directory"):
        tidy_module._git_common_dir(linked_root)


def test_cleanup_skips_candidate_that_disappears_before_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing worktree during cleanup does not stop the command."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = "\0".join(
        (
            f"worktree {tmp_path}",
            "HEAD abcdef",
            "branch refs/heads/main",
            "",
            f"worktree {worktree_path}",
            "HEAD 123456",
            "branch refs/heads/123-finished",
            "",
        )
    )
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(
        tidy_module,
        "_worktree_is_dirty",
        MagicMock(side_effect=FileNotFoundError("worktree disappeared")),
    )
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert "Skipping missing worktree" in caplog.text
    remove.assert_not_called()


def test_cleanup_reports_initial_missing_worktree_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing registration from the first inventory requires recovery."""
    missing_path = tmp_path / "123-finished"
    porcelain = _candidate_worktree_porcelain(tmp_path, missing_path, "123-finished")
    issue_is_closed = MagicMock()
    branch_is_merged = MagicMock()
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", issue_is_closed)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Skipping missing worktree" in caplog.text
    issue_is_closed.assert_not_called()
    branch_is_merged.assert_not_called()


def test_cleanup_revalidates_candidate_before_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup skips a path when its attached branch changes after inventory."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    first_inventory = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    replacement_inventory = _candidate_worktree_porcelain(tmp_path, worktree_path, "replacement")
    inventories = MagicMock(side_effect=(first_inventory, replacement_inventory))
    is_dirty = MagicMock(side_effect=AssertionError("must not inspect a replaced worktree"))
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert inventories.call_count == 2
    is_dirty.assert_not_called()


def test_cleanup_revalidates_same_branch_head_before_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup skips a replacement that retains the same branch name."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    first_inventory = _candidate_worktree_porcelain(
        tmp_path,
        worktree_path,
        "123-finished",
        head="111111",
    )
    replacement_inventory = _candidate_worktree_porcelain(
        tmp_path,
        worktree_path,
        "123-finished",
        head="222222",
    )
    inventories = MagicMock(side_effect=(first_inventory, replacement_inventory))
    is_dirty = MagicMock(side_effect=AssertionError("must not inspect a replacement"))
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert inventories.call_count == 2
    is_dirty.assert_not_called()


def test_cleanup_revalidates_candidate_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup does not remove a path after its registration changes."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    first_inventory = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    replacement_inventory = _candidate_worktree_porcelain(tmp_path, worktree_path, "replacement")
    inventories = MagicMock(
        side_effect=(first_inventory, first_inventory, replacement_inventory),
    )
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert inventories.call_count == 3
    remove.assert_not_called()


def test_cleanup_revalidates_clean_state_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup does not remove a worktree that becomes dirty after confirmation."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    inventories = MagicMock(side_effect=(porcelain, porcelain, porcelain))
    is_dirty = MagicMock(side_effect=(False, True))
    remove = MagicMock(side_effect=AssertionError("must not remove a dirty worktree"))
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert is_dirty.call_count == 2
    remove.assert_not_called()


def test_cleanup_revalidates_locked_state_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup does not remove a worktree that becomes locked after confirmation."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    unlocked_inventory = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    locked_inventory = _candidate_worktree_porcelain(
        tmp_path,
        worktree_path,
        "123-finished",
        locked=True,
    )
    inventories = MagicMock(side_effect=(unlocked_inventory, unlocked_inventory, locked_inventory))
    remove = MagicMock(side_effect=AssertionError("must not remove a locked worktree"))
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    remove.assert_not_called()


def test_cleanup_revalidates_stale_reason_before_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup does not remove a branch after its stale reason changes."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    inventories = MagicMock(side_effect=(porcelain, porcelain, porcelain))
    issue_is_closed = MagicMock(side_effect=(True, True, False))
    remove = MagicMock(side_effect=AssertionError("must not remove a changed candidate"))
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", issue_is_closed)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 0
    assert issue_is_closed.call_count == 3
    remove.assert_not_called()


def test_cleanup_reports_status_failure_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A status error leaves the candidate untouched and cleanup incomplete."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(tmp_path, worktree_path, "123-finished")
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: porcelain)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(
        tidy_module,
        "_worktree_is_dirty",
        MagicMock(side_effect=subprocess.CalledProcessError(1, ["git", "status"])),
    )

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Could not inspect worktree" in caplog.text


def test_cleanup_retains_branch_when_conditional_delete_detects_a_new_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ref changed after worktree removal is never deleted by its reused name."""
    expected_head = "a" * 40
    trunk_head = "b" * 40
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(
        tmp_path,
        worktree_path,
        "123-finished",
        head=expected_head,
    )
    run_git = MagicMock(
        side_effect=(
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{trunk_head}\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="ref changed"),
        )
    )
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", MagicMock(side_effect=(porcelain,) * 3))
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: True)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "run_git", run_git)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Retained local branch 123-finished" in caplog.text
    run_git.assert_has_calls(
        [
            call(["worktree", "remove", str(worktree_path)]),
            call(
                ["rev-parse", "--verify", "refs/heads/main"],
                check=False,
                log_on_error=False,
            ),
            call(
                ["update-ref", "--stdin", "-z"],
                input_text=_branch_delete_transaction_input(
                    "123-finished", expected_head, "main", trunk_head
                ),
                check=False,
                log_on_error=False,
            ),
        ]
    )


def test_remove_worktree_retains_an_unmerged_inspected_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unmerged inspected commit retains its branch after worktree removal."""
    expected_head = "a" * 40
    trunk_head = "b" * 40
    worktree_path = tmp_path / "123-finished"
    run_git = MagicMock(
        side_effect=(
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{trunk_head}\n", stderr=""),
        )
    )
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "run_git", run_git)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)

    assert not tidy_module._remove_worktree(worktree_path, "123-finished", expected_head, "main")
    assert "Retained local branch 123-finished" in caplog.text
    run_git.assert_has_calls(
        [
            call(["worktree", "remove", str(worktree_path)]),
            call(
                ["rev-parse", "--verify", "refs/heads/main"],
                check=False,
                log_on_error=False,
            ),
        ]
    )


def test_remove_worktree_reports_retained_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A conditional branch-delete failure leaves cleanup incomplete and visible."""
    expected_head = "a" * 40
    trunk_head = "b" * 40
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    run_git = MagicMock(
        side_effect=(
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{trunk_head}\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not fully merged"),
        )
    )
    monkeypatch.setattr(tidy_module, "run_git", run_git)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: True)

    assert (
        tidy_module._remove_worktree(
            tmp_path / "123-finished", "123-finished", expected_head, "main"
        )
        is False
    )
    assert "Retained local branch 123-finished" in caplog.text
    run_git.assert_has_calls(
        [
            call(["worktree", "remove", str(tmp_path / "123-finished")]),
            call(
                ["rev-parse", "--verify", "refs/heads/main"],
                check=False,
                log_on_error=False,
            ),
            call(
                ["update-ref", "--stdin", "-z"],
                input_text=_branch_delete_transaction_input(
                    "123-finished", expected_head, "main", trunk_head
                ),
                check=False,
                log_on_error=False,
            ),
        ]
    )


def test_remove_worktree_reports_branch_delete_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A conditional branch-delete exception leaves cleanup incomplete and visible."""
    expected_head = "a" * 40
    trunk_head = "b" * 40
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    run_git = MagicMock(
        side_effect=(
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{trunk_head}\n", stderr=""),
            OSError("branch deletion failed"),
        )
    )
    monkeypatch.setattr(tidy_module, "run_git", run_git)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: True)

    assert (
        tidy_module._remove_worktree(
            tmp_path / "123-finished", "123-finished", expected_head, "main"
        )
        is False
    )
    assert "Retained local branch 123-finished" in caplog.text


def test_remove_worktree_retains_branch_when_trunk_moves_after_merge_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A trunk ref change after merge evidence retains the local branch."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "123-finished"
    branch = "123-finished"
    trunk = "main"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    def git(
        *args: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            input=input_text,
            check=check,
            capture_output=True,
            text=True,
        )

    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    (repo_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
    expected_head = git("rev-parse", "HEAD").stdout.strip()
    git("branch", branch, expected_head)
    git("worktree", "add", "-q", str(worktree_path), branch)
    empty_tree = git("mktree", input_text="").stdout.strip()
    replacement_head = git("commit-tree", empty_tree, "-m", "test: replacement").stdout.strip()
    trunk_repointed = False
    monkeypatch.chdir(repo_path)

    def branch_is_merged(branch_ref: str, trunk_ref: str) -> bool:
        nonlocal trunk_repointed
        result = git("merge-base", "--is-ancestor", branch_ref, trunk_ref, check=False)
        assert result.returncode == 0
        git("update-ref", f"refs/heads/{trunk}", replacement_head)
        trunk_repointed = True
        return True

    monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)

    result = tidy_module._remove_worktree(worktree_path, branch, expected_head, trunk)

    assert trunk_repointed
    assert (
        git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    )
    assert result is False


def test_remove_worktree_deletes_branch_when_trunk_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stable trunk permits conditional local branch deletion."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "123-finished"
    branch = "123-finished"
    trunk = "main"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            check=check,
            capture_output=True,
            text=True,
        )

    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    (repo_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
    expected_head = git("rev-parse", "HEAD").stdout.strip()
    git("branch", branch, expected_head)
    git("worktree", "add", "-q", str(worktree_path), branch)
    monkeypatch.chdir(repo_path)

    assert tidy_module._remove_worktree(worktree_path, branch, expected_head, trunk) is True
    assert (
        git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode != 0
    )


def test_cleanup_reports_worktree_removal_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worktree removal failure is visible and leaves cleanup incomplete."""
    worktree_path = tmp_path / "123-finished"
    worktree_path.mkdir()
    porcelain = _candidate_worktree_porcelain(
        tmp_path,
        worktree_path,
        "123-finished",
    )
    inventories = MagicMock(side_effect=(porcelain, porcelain, porcelain))
    run_git = MagicMock(side_effect=subprocess.CalledProcessError(1, ["git", "worktree", "remove"]))
    caplog.set_level("WARNING", logger="hephaestus.github.tidy")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", inventories)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(tidy_module, "run_git", run_git)

    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=False) == 1
    assert "Could not remove worktree" in caplog.text
    run_git.assert_called_once_with(["worktree", "remove", str(worktree_path)])


def test_agent_prompt_directs_temporary_worktree_outside_git_metadata(tmp_path: Path) -> None:
    """A rebase agent creates its worktree outside Git metadata."""
    repo_path = tmp_path / "repo"
    prompt = tidy_module._make_agent_prompt("feature/nested", "main", repo_path, "owner/repo")
    worktree_line = next(
        line for line in prompt.splitlines() if line.startswith("- Worktree to create: ")
    )
    worktree_path = Path(worktree_line.removeprefix("- Worktree to create: "))

    assert worktree_path.parent == repo_path / "build" / ".worktrees"
    assert not worktree_path.is_relative_to(repo_path / ".git")
    assert "/" not in worktree_path.name
    assert f"git worktree add '{worktree_path}' 'feature/nested'" in prompt
    assert "git worktree prune" not in prompt


def test_agent_prompt_rejects_symlinked_worktree_parent_in_git_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rendered agent command cannot target Git metadata through a symlink."""
    repo_path = tmp_path / "repo"
    metadata_worktrees = repo_path / ".git" / "worktrees"
    metadata_worktrees.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.parent.mkdir()
    try:
        agent_parent.symlink_to(metadata_worktrees, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    render = MagicMock(side_effect=AssertionError("must not render an unsafe prompt"))
    catalog = MagicMock()
    catalog.render = render
    monkeypatch.setattr(tidy_module.PromptCatalog, "current", MagicMock(return_value=catalog))

    with pytest.raises(tidy_module.WorktreeInventoryError, match="Git metadata"):
        tidy_module._make_agent_prompt("feature/nested", "main", repo_path, "owner/repo")

    render.assert_not_called()


def test_dispatch_swarm_does_not_start_agents_for_an_unsafe_worktree_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All agent dispatch stops when the shared temporary parent is unsafe."""
    repo_path = tmp_path / "repo"
    metadata_worktrees = repo_path / ".git" / "worktrees"
    metadata_worktrees.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.parent.mkdir()
    try:
        agent_parent.symlink_to(metadata_worktrees, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    run_agent = MagicMock(return_value="failed")
    monkeypatch.setattr(tidy_module, "_run_direct_rebase_agent", run_agent)

    result = asyncio.run(
        tidy_module._dispatch_swarm(
            ["feature/nested"],
            "main",
            repo_path,
            "owner/repo",
            max_concurrent=1,
            dry_run=False,
            agent="codex",
        )
    )

    assert result == {"feature/nested": "failed (unsafe agent worktree)"}
    run_agent.assert_not_called()


def test_dispatch_swarm_does_not_partially_start_agents_for_an_unsafe_worktree_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unsafe later agent path prevents every agent from starting."""
    repo_path = tmp_path / "repo"
    metadata_worktrees = repo_path / ".git" / "worktrees"
    metadata_worktrees.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.mkdir(parents=True)
    unsafe_branch = "feature/unsafe"
    unsafe_path = tidy_module._agent_worktree_path(repo_path, unsafe_branch)
    try:
        unsafe_path.symlink_to(metadata_worktrees, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    run_agent = MagicMock(return_value="failed")
    monkeypatch.setattr(tidy_module, "_run_direct_rebase_agent", run_agent)

    result = asyncio.run(
        tidy_module._dispatch_swarm(
            ["feature/safe", unsafe_branch],
            "main",
            repo_path,
            "owner/repo",
            max_concurrent=1,
            dry_run=False,
            agent="codex",
        )
    )

    assert result == {
        "feature/safe": "failed (unsafe agent worktree)",
        unsafe_branch: "failed (unsafe agent worktree)",
    }
    run_agent.assert_not_called()


def test_agent_prompt_rejects_worktree_parent_in_foreign_git_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An agent prompt cannot create a worktree in foreign Git metadata."""
    repo_path = tmp_path / "repo-a"
    foreign_metadata = tmp_path / "repo-b" / ".git" / "worktrees"
    (repo_path / ".git").mkdir(parents=True)
    foreign_metadata.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.parent.mkdir()
    try:
        agent_parent.symlink_to(foreign_metadata, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    render = MagicMock(return_value="unsafe prompt")
    catalog = MagicMock()
    catalog.render = render
    monkeypatch.setattr(tidy_module.PromptCatalog, "current", MagicMock(return_value=catalog))

    with pytest.raises(tidy_module.WorktreeInventoryError):
        tidy_module._make_agent_prompt("feature/nested", "main", repo_path, "owner/repo")

    render.assert_not_called()


def test_dispatch_swarm_does_not_start_agents_for_foreign_git_metadata_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A foreign Git metadata parent prevents all agent starts."""
    repo_path = tmp_path / "repo-a"
    foreign_metadata = tmp_path / "repo-b" / ".git" / "worktrees"
    (repo_path / ".git").mkdir(parents=True)
    foreign_metadata.mkdir(parents=True)
    agent_parent = repo_path / "build" / ".worktrees"
    agent_parent.parent.mkdir()
    try:
        agent_parent.symlink_to(foreign_metadata, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create the test symlink: {error}")
    run_agent = MagicMock(return_value="ran")
    monkeypatch.setattr(tidy_module, "_run_direct_rebase_agent", run_agent)

    result = asyncio.run(
        tidy_module._dispatch_swarm(
            ["feature/nested"],
            "main",
            repo_path,
            "owner/repo",
            max_concurrent=1,
            dry_run=False,
            agent="codex",
        )
    )

    assert result == {"feature/nested": "failed (unsafe agent worktree)"}
    run_agent.assert_not_called()


def test_rebase_prompt_renders_with_public_context_without_worktree_parent(tmp_path: Path) -> None:
    """The packaged rebase prompt does not require private builder context."""
    worktree_path = tmp_path / "repo" / "build" / ".worktrees" / "tidy-agent"

    prompt = tidy_module.PromptCatalog.current().render(
        "tidy/rebase_fix.j2",
        branch="feature/nested",
        trunk="main",
        repo_path=tmp_path / "repo",
        repo_slug="owner/repo",
        worktree_path=worktree_path,
    )

    assert f"mkdir -p \"$(dirname '{worktree_path}')\"" in prompt


def test_rebase_prompt_renders_string_worktree_paths_for_lock_and_fallbacks(
    tmp_path: Path,
) -> None:
    """A public string path keeps lock and fallback commands in the agent worktree."""
    worktree_path = tmp_path / "repo" / "build" / ".worktrees" / "tidy-agent"

    prompt = tidy_module.PromptCatalog.current().render(
        "tidy/rebase_fix.j2",
        branch="feature/nested",
        trunk="main",
        repo_path=tmp_path / "repo",
        repo_slug="owner/repo",
        worktree_path=str(worktree_path),
    )

    assert f"> '{worktree_path}/uv.lock'" in prompt
    assert f"(cd '{worktree_path}' && git show <ref>:<file> > <file>)" in prompt
    assert f"git -C '{worktree_path}' switch <branch>" in prompt
    assert f"git -C '{worktree_path}' reset --keep" in prompt


def test_agent_prompt_quotes_worktree_commands_for_a_path_with_spaces(tmp_path: Path) -> None:
    """A rebase prompt preserves a repository path with spaces as one argument."""
    repo_path = tmp_path / "repo with spaces"
    prompt = tidy_module._make_agent_prompt("feature/nested", "main", repo_path, "owner/repo")
    worktree_path = tidy_module._agent_worktree_path(repo_path, "feature/nested")

    assert f"cd '{repo_path}'" in prompt
    assert f"mkdir -p \"$(dirname '{worktree_path}')\"" in prompt
    assert f"git worktree add '{worktree_path}' 'feature/nested'" in prompt


def test_agent_prompt_quotes_shell_metacharacters_as_single_arguments(tmp_path: Path) -> None:
    """Shell quotes keep each generated command argument intact."""
    injection_probe = "hephaestus_injection_probe"
    repo_path = tmp_path / f"repo ';$({injection_probe})"
    branch = f"feature/quote';$({injection_probe})"
    trunk = f"main';$({injection_probe})"
    (repo_path / ".git").mkdir(parents=True)
    prompt = tidy_module._make_agent_prompt(branch, trunk, repo_path, "owner/repo")
    worktree_path = tidy_module._agent_worktree_path(repo_path, branch)
    worktree_path.mkdir(parents=True)
    primary_lock = repo_path / "uv.lock"
    primary_lock.write_text("primary lock\n", encoding="utf-8")
    lines = prompt.splitlines()

    cd_line = next(line for line in lines if line.startswith("cd "))
    mkdir_line = next(line for line in lines if line.startswith("mkdir -p "))
    add_line = next(line for line in lines if line.startswith("git worktree add "))
    fetch_line = next(
        line for line in lines if line.startswith("git -C ") and " fetch origin " in line
    )
    rebase_line = next(line for line in lines if line.startswith("git -C ") and " rebase " in line)
    diff_line = next(
        line for line in lines if line.startswith("git -C ") and " diff --name-only " in line
    )
    show_line = next(line for line in lines if " show " in line and "uv.lock" in line)
    show_command = next(part for part in show_line.split("`") if part.startswith("git -C "))
    continue_line = next(line for line in lines if line.startswith("GIT_EDITOR=true git -C "))
    skip_line = next(line for line in lines if " rebase --skip" in line)
    skip_command = next(part for part in skip_line.split("`") if part.startswith("git -C "))
    log_line = next(line for line in lines if line.startswith("git -C ") and " log " in line)
    push_line = next(
        line for line in lines if line.startswith("git -C ") and " push --force-with-lease " in line
    )
    remove_line = next(
        line for line in lines if line.startswith("git -C ") and " worktree remove " in line
    )

    assert shlex.split(cd_line) == ["cd", str(repo_path)]
    mkdir_probe = subprocess.run(
        [
            "sh",
            "-c",
            "\n".join(
                (
                    f"{injection_probe}() {{ printf '%s\\n' invoked >&2; return 97; }}",
                    'mkdir() { printf "%s\\n" "$@"; }',
                    mkdir_line,
                )
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert mkdir_probe.returncode == 0, mkdir_probe.stderr
    assert mkdir_probe.stderr == ""
    assert mkdir_probe.stdout.splitlines() == ["-p", str(worktree_path.parent)]
    assert shlex.split(add_line) == ["git", "worktree", "add", str(worktree_path), branch]
    assert shlex.split(fetch_line) == ["git", "-C", str(worktree_path), "fetch", "origin", trunk]
    assert shlex.split(rebase_line) == [
        "git",
        "-C",
        str(worktree_path),
        "rebase",
        f"origin/{trunk}",
    ]
    assert shlex.split(diff_line) == [
        "git",
        "-C",
        str(worktree_path),
        "diff",
        "--name-only",
        "--diff-filter=U",
    ]
    assert shlex.split(show_command)[:5] == [
        "git",
        "-C",
        str(worktree_path),
        "show",
        f"origin/{trunk}:uv.lock",
    ]
    assert shlex.split(continue_line) == [
        "GIT_EDITOR=true",
        "git",
        "-C",
        str(worktree_path),
        "rebase",
        "--continue",
    ]
    assert shlex.split(skip_command) == [
        "git",
        "-C",
        str(worktree_path),
        "rebase",
        "--skip",
    ]
    assert shlex.split(log_line) == [
        "git",
        "-C",
        str(worktree_path),
        "log",
        f"origin/{trunk}..HEAD",
        "--oneline",
    ]
    assert shlex.split(push_line) == [
        "git",
        "-C",
        str(worktree_path),
        "push",
        "--force-with-lease",
        "--force-if-includes",
        "origin",
        branch,
    ]
    assert shlex.split(remove_line) == [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "remove",
        str(worktree_path),
    ]

    fake_bin = tmp_path / "bin"
    fake_git = fake_bin / "git"
    fake_bin.mkdir()
    fake_git.write_text("#!/bin/sh\nprintf '%s\\n' 'agent lock'\n", encoding="utf-8")
    fake_git.chmod(0o755)
    show_probe = subprocess.run(
        [
            "sh",
            "-c",
            "\n".join(
                (
                    f"{injection_probe}() {{ printf '%s\\n' invoked >&2; return 97; }}",
                    show_command,
                )
            ),
        ],
        cwd=repo_path,
        env={"PATH": f"{fake_bin}{os.pathsep}{os.defpath}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert show_probe.returncode == 0, show_probe.stderr
    assert show_probe.stderr == ""
    agent_lock = worktree_path / "uv.lock"
    assert agent_lock.is_file()
    assert agent_lock.read_text(encoding="utf-8") == "agent lock\n"
    assert primary_lock.read_text(encoding="utf-8") == "primary lock\n"


def test_agent_prompt_safety_net_commands_target_only_the_agent_worktree(
    tmp_path: Path,
) -> None:
    """Each fallback command keeps its read and write target in the agent worktree."""
    repo_path = tmp_path / "primary"
    (repo_path / ".git").mkdir(parents=True)
    prompt = tidy_module._make_agent_prompt("feature/nested", "main", repo_path, "owner/repo")
    worktree_path = tidy_module._agent_worktree_path(repo_path, "feature/nested")
    worktree_path.mkdir(parents=True)
    primary_marker = repo_path / "primary-marker"
    primary_marker.write_text("unchanged\n", encoding="utf-8")

    fallback_lines = [line for line in prompt.splitlines() if line.startswith("- Instead of `git")]
    assert len(fallback_lines) == 3
    show_command, switch_command, reset_command = (line.split("`")[-2] for line in fallback_lines)
    show_command = show_command.replace("<ref>:<file>", "origin/main:fixture.txt").replace(
        "<file>", "fixture.txt"
    )
    switch_command = switch_command.replace("<branch>", "feature/fallback")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "git-trace"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\t%s\\n\' "$PWD" "$*" >> "$TRACE_PATH"\n'
        "printf '%s\\n' 'fallback content'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
        "TRACE_PATH": str(trace),
    }

    for command in (show_command, switch_command, reset_command):
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=repo_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    assert (worktree_path / "fixture.txt").read_text(encoding="utf-8") == "fallback content\n"
    assert not (repo_path / "fixture.txt").exists()
    assert primary_marker.read_text(encoding="utf-8") == "unchanged\n"
    traces = trace.read_text(encoding="utf-8").splitlines()
    assert traces == [
        f"{worktree_path}\tshow origin/main:fixture.txt",
        f"{repo_path}\t-C {worktree_path} switch feature/fallback",
        f"{repo_path}\t-C {worktree_path} reset --keep",
    ]


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "feature/foo-bar",
        "fix/issue-123",
        "chore/bump-deps",
        "release/v2.0.0",
    ],
)
def test_various_branch_name_formats(branch: str) -> None:
    """Branch names with slashes, numbers, and hyphens are all parsed correctly."""
    output = (
        "WARNING: Unable to auto-rebase the following branches:\n"
        f"    * {branch}\n"
        "Finished tidying!\n"
    )
    assert parse_problem_branches(output) == [branch]


def test_dispatch_swarm_runs_codex_agents_in_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex swarm dispatch should preserve max_concurrent semantics."""
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_to_thread(func: object, *args: object) -> str:
        calls.append((func, args))
        return "fixed"

    monkeypatch.setattr(tidy_module.asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(
        tidy_module._dispatch_swarm(
            ["feature/a"],
            "main",
            tmp_path,
            "owner/repo",
            max_concurrent=1,
            dry_run=False,
            agent="codex",
        )
    )

    assert result == {"feature/a": "fixed"}
    assert calls
    assert calls[0][0] is tidy_module._run_direct_rebase_agent
    assert calls[0][1][0] == "codex"


class TestTidyHandlers:
    """Tests for extracted tidy workflow handlers."""

    def test_run_tidy_and_find_problem_branches_fails_closed_on_gh_tidy_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero gh tidy exit must raise, never fabricate a clean result.

        Athena #103: parsing partial output after gh tidy exited non-zero and
        then claiming "All branches rebased cleanly" produced a false success.
        The function now fails closed so callers cannot lie about cleanup state.
        """
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry_run: (128, ONE_PROBLEM))

        with pytest.raises(tidy_module.TidyExecutionError) as excinfo:
            tidy_module._run_tidy_and_find_problem_branches("main", False)
        assert excinfo.value.exit_code == 128

        # dry-run never mutates state, so it may still parse output for preview.
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry_run: (128, ONE_PROBLEM))
        assert tidy_module._run_tidy_and_find_problem_branches("main", True) == [
            "feature/my-branch"
        ]

    def test_handle_problem_branches_dry_run_json(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Dry-run problem branches emit the existing ok JSON envelope."""
        args = argparse.Namespace(no_swarm=False, dry_run=True, json=True, max_concurrent=5)

        assert (
            tidy_module._handle_problem_branches(
                args,
                ["feature/a"],
                "main",
                tmp_path,
                "owner/repo",
                "claude",
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["problem_branches"] == ["feature/a"]


class TestMain:
    """Smoke tests for hephaestus.github.tidy.main() covering --json branches."""

    @pytest.mark.usefixtures("require_git_path_format", "require_git_worktree_list_z")
    def test_cleanup_from_linked_worktree_never_targets_primary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup invoked from a linked worktree excludes the primary checkout."""
        repo = tmp_path / "repo"
        linked_root = tmp_path / "topic-linked"
        stale_root = tmp_path / "123-finished"

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "--initial-branch=main", str(repo))
        git("-C", str(repo), "config", "user.name", "Test User")
        git("-C", str(repo), "config", "user.email", "test@example.com")
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git("-C", str(repo), "add", "tracked.txt")
        git("-C", str(repo), "commit", "-m", "test fixture")
        git("-C", str(repo), "worktree", "add", "-b", "topic", str(linked_root))
        git("-C", str(repo), "worktree", "add", "-b", "123-finished", str(stale_root))

        monkeypatch.chdir(linked_root)
        monkeypatch.setattr(tidy_module, "_detect_repo_from_remote", lambda: "owner/repo")
        monkeypatch.setattr(tidy_module, "_working_tree_clean", lambda: True)
        monkeypatch.setattr(tidy_module, "_in_git_repo", lambda: True)
        monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda _path: False)
        monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda _issue: False)
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        branch_is_merged = MagicMock(return_value=True)
        monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-tidy",
                "--cleanup-stale-worktrees",
                "--dry-run",
                "--agent",
                "claude",
            ],
        )

        assert tidy_module.main() == 0
        assert {call.args[0] for call in branch_is_merged.call_args_list} == {"123-finished"}

    @pytest.mark.parametrize(
        ("failure", "message"),
        [
            pytest.param(
                subprocess.CompletedProcess([], 129, stdout="", stderr="unknown switch `z`"),
                "requires Git 2.36 or later",
                id="unsupported-known-diagnostic",
            ),
            pytest.param(
                subprocess.CompletedProcess([], 129, stdout="", stderr="different diagnostic"),
                "requires Git 2.36 or later",
                id="unsupported-alternate-diagnostic",
            ),
            pytest.param(
                subprocess.CompletedProcess([], 129, stdout="", stderr=""),
                "requires Git 2.36 or later",
                id="unsupported-empty-diagnostic",
            ),
            pytest.param(
                subprocess.CompletedProcess([], 2, stdout="", stderr="failure"),
                "exit status 2",
                id="inventory-failure",
            ),
            pytest.param(
                subprocess.TimeoutExpired(["git", "worktree", "list"], 30),
                "inventory timed out",
                id="inventory-timeout",
            ),
        ],
    )
    def test_inventory_failure_matrix_exits_cleanly_without_mutation(
        self,
        failure: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Inventory failures stop cleanup with one actionable error."""
        caplog.set_level("ERROR", logger="hephaestus.github.tidy")
        run_git = MagicMock(
            side_effect=failure if isinstance(failure, subprocess.TimeoutExpired) else None,
            return_value=failure if isinstance(failure, subprocess.CompletedProcess) else None,
        )
        remove = MagicMock()
        monkeypatch.setattr(tidy_module, "run_git", run_git)
        monkeypatch.setattr(tidy_module, "_remove_worktree", remove)
        monkeypatch.setattr(tidy_module, "_configure_logging", lambda *_args: None)
        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _branch: "main")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-tidy", "--cleanup-stale-worktrees", "--agent", "claude"],
        )

        assert tidy_module.main() == 1
        output = caplog.text + capsys.readouterr().out
        assert message in output
        assert "No cleanup change occurred" in output
        assert "Traceback" not in output
        remove.assert_not_called()
        run_git.assert_called_once_with(
            ["worktree", "list", "--porcelain", "-z"],
            check=False,
            log_on_error=False,
        )

    def test_inventory_compatibility_failure_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Unsupported inventory emits the standard JSON error envelope."""
        log_error = MagicMock()
        monkeypatch.setattr(
            tidy_module,
            "run_git",
            MagicMock(return_value=subprocess.CompletedProcess([], 129, stdout="", stderr="usage")),
        )
        monkeypatch.setattr(tidy_module, "_configure_logging", lambda *_args: None)
        monkeypatch.setattr(tidy_module.logger, "info", MagicMock())
        monkeypatch.setattr(tidy_module.logger, "error", log_error)
        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _branch: "main")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-tidy",
                "--cleanup-stale-worktrees",
                "--json",
                "--agent",
                "claude",
            ],
        )

        assert tidy_module.main() == 1
        captured = capsys.readouterr()
        assert captured.out
        payload = json.loads(captured.out)
        assert payload["status"] == "error"
        assert payload["exit_code"] == 1
        assert "requires Git 2.36 or later" in payload["message"]
        assert "No cleanup change occurred" in payload["message"]
        assert "Traceback" not in captured.out + captured.err
        log_error.assert_called_once()

    def test_git_executable_exit_129_is_actionable_and_non_mutating(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A legacy Git executable stops before a cleanup command."""
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        argument_log = tmp_path / "git-arguments"
        fake_git = fake_bin / "git"
        fake_git.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(argument_log))}\nexit 129\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(tidy_module, "_configure_logging", lambda *_args: None)
        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _branch: "main")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-tidy", "--cleanup-stale-worktrees", "--agent", "claude"],
        )
        caplog.set_level("ERROR", logger="hephaestus.github.tidy")

        assert tidy_module.main() == 1
        assert "requires Git 2.36 or later" in caplog.text
        assert "Traceback" not in caplog.text
        arguments = argument_log.read_text(encoding="utf-8").splitlines()
        assert arguments == ["worktree list --porcelain -z"]
        assert not any("worktree remove" in item or "branch -d" in item for item in arguments)

    def test_env_validation_failure_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When env validation fails, --json emits an error envelope."""
        import json

        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "environment" in payload["message"]

    def test_no_problem_branches_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Clean tidy run with --json emits ok envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: [])
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["problem_branches"] == 0

    def test_no_swarm_with_problems_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """--no-swarm with problem branches emits error envelope and exits 1."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-tidy", "--json", "--no-swarm", "--agent", "claude"]
        )
        assert tidy_module.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["swarm"] == "skipped"

    def test_handle_problem_branches_no_swarm_json(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Extracted problem-branch handler emits the existing no-swarm JSON."""
        import json

        args = tidy_module._build_arg_parser().parse_args(
            ["--json", "--no-swarm", "--agent", "claude"]
        )

        assert (
            tidy_module._handle_tidy_problem_branches(
                args=args,
                agent="claude",
                problem_branches=["feature/a"],
                trunk="main",
                repo_path=tmp_path,
                repo_slug="owner/repo",
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["problem_branches"] == ["feature/a"]
        assert payload["swarm"] == "skipped"

    def test_dry_run_with_problems_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """--dry-run with problem branches emits ok envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-tidy", "--json", "--dry-run", "--agent", "claude"]
        )
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["dry_run"] is True

    def test_full_dispatch_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """End-to-end with swarm dispatch (mocked) emits results envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])

        async def fake_dispatch(*args, **kwargs):
            return {"feature/a": "rebased"}

        monkeypatch.setattr(tidy_module, "_dispatch_swarm", fake_dispatch)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["results"] == {"feature/a": "rebased"}

    def test_env_validation_failure_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without --json, env-validation failure still exits 1."""
        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--agent", "claude"])
        assert tidy_module.main() == 1

    def test_main_threads_explicit_pi_policy_to_agent_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider resolution receives the CLI-owned Pi policy and auth budget."""
        captured: dict[str, object] = {}

        def fake_resolve(
            agent,
            *,
            disable_pi_automation,
            auth_status_timeout,
            pi_isolation_adapter,
            pi_dir,
        ):
            captured.update(
                agent=agent,
                disable_pi_automation=disable_pi_automation,
                auth_status_timeout=auth_status_timeout,
                pi_isolation_adapter=pi_isolation_adapter,
                pi_dir=pi_dir,
            )
            return "codex"

        monkeypatch.setattr(tidy_module, "resolve_agent", fake_resolve)
        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-tidy",
                "--agent",
                "codex",
                "--disable-pi-automation",
                "--auth-status-timeout",
                "19",
            ],
        )

        assert tidy_module.main() == 1
        assert captured == {
            "agent": "codex",
            "disable_pi_automation": True,
            "auth_status_timeout": 19,
            "pi_isolation_adapter": None,
            "pi_dir": None,
        }


class TestTimeoutHandling:
    """Tests for subprocess timeout handling in tidy helpers."""

    def test_detect_default_branch_with_timeout(self) -> None:
        """_detect_default_branch falls back to 'main' on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["gh"], 120)
            result = _detect_default_branch(None)
            assert result == "main"

    def test_detect_default_branch_calls_with_network_timeout(self) -> None:
        """_detect_default_branch passes a positive timeout through gh_call.

        _detect_default_branch now routes through
        :func:`hephaestus.github.client.gh_call`, which invokes the subprocess
        via ``run_subprocess`` with ``timeout=gh_cli_timeout()`` (#713). Assert
        at that seam that a positive timeout is still supplied, preserving the
        no-timeout-less-read invariant after the adapter move.
        """
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(stdout="main\n")
            _detect_default_branch(None)
            # Verify the call included a positive timeout
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] > 0

    def test_working_tree_clean_with_timeout(self) -> None:
        """_working_tree_clean propagates TimeoutExpired."""
        with patch("hephaestus.github.tidy._shared_working_tree_clean") as shared_clean:
            shared_clean.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _working_tree_clean()

    def test_working_tree_clean_uses_shared_helper(self) -> None:
        """_working_tree_clean routes git status through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_working_tree_clean") as shared_clean:
            shared_clean.return_value = True
            assert _working_tree_clean() is True
            shared_clean.assert_called_once_with()

    def test_in_git_repo_with_timeout(self) -> None:
        """_in_git_repo propagates TimeoutExpired."""
        with patch("hephaestus.github.tidy._shared_in_git_repo") as shared_in_repo:
            shared_in_repo.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _in_git_repo()

    def test_in_git_repo_uses_shared_helper(self) -> None:
        """_in_git_repo routes git rev-parse through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_in_git_repo") as shared_in_repo:
            shared_in_repo.return_value = True
            assert _in_git_repo() is True
            shared_in_repo.assert_called_once_with()

    def test_repo_root_uses_shared_helper(self) -> None:
        """_repo_root routes git root detection through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_repo_root") as shared_repo_root:
            shared_repo_root.return_value = Path("/path/to/repo")
            assert _repo_root() == Path("/path/to/repo")
            shared_repo_root.assert_called_once_with()

    def test_direct_rebase_agent_uses_explicit_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removed HEPH_AGENT_REBASE_TIMEOUT is inert; explicit timeout wins."""
        monkeypatch.setenv("HEPH_AGENT_REBASE_TIMEOUT", "1234")
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent(
                "codex", "prompt", "feature/a", Path("/repo"), timeout=37
            )

        assert run_agent.call_args.kwargs["timeout"] == 37
        assert run_agent.call_args.kwargs["model"] == ""

    def test_direct_rebase_agent_default_timeout(self) -> None:
        """Default rebase-agent timeout is AGENT_REBASE_TIMEOUT (2400)."""
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent("codex", "prompt", "feature/a", Path("/repo"))

        assert run_agent.call_args.kwargs["timeout"] == 2400


def test_tidy_parser_accepts_explicit_log_format() -> None:
    """Tidy logging is selected explicitly and remains separate from --json."""
    args = tidy_module._build_arg_parser().parse_args(["--log-format", "json"])

    assert args.log_format == "json"
    assert args.json is False


def test_tidy_configure_logging_forwards_explicit_format() -> None:
    """The tidy adapter forwards the selected format to shared CLI logging."""
    with patch.object(tidy_module, "configure_cli_logging") as configure:
        tidy_module._configure_logging(True, "json")

    configure.assert_called_once_with(verbose=True, log_format="json")
