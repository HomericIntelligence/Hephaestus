"""Tests for worktree manager."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from hephaestus.automation.worktree_manager import WorktreeDirtyError, WorktreeManager


@pytest.fixture(autouse=True)
def _clear_loop_trunk_githash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests independent from automation-loop parent env."""
    monkeypatch.delenv("HEPH_TRUNK_GITHASH", raising=False)


class TestWorktreeManager:
    """Tests for WorktreeManager class."""

    def test_initialization_default_base_dir(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test initialization with default base directory."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"

        manager = WorktreeManager()

        assert manager.repo_root == tmp_path
        assert manager.base_dir == tmp_path / "build" / ".worktrees"
        assert manager.worktrees == {}

    def test_initialization_custom_base_dir(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test initialization with custom base directory."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        custom_dir = tmp_path / "custom_worktrees"

        manager = WorktreeManager(base_dir=custom_dir)

        assert manager.base_dir == custom_dir

    def test_create_worktree_success(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test successful worktree creation."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        # No existing worktree holds this branch (collision check returns None).
        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            worktree_path = manager.create_worktree(123, "123-feature")

        assert worktree_path == manager.base_dir / "issue-123"
        assert manager.worktrees[123] == worktree_path

        # Verify git calls: 1) base branch detection 2) local rev-parse check
        # 3) ls-remote check (branch absent locally) 4) worktree add from base
        assert worktree_mocks.run.call_count == 4
        # Check the worktree add call
        call_args = worktree_mocks.run.call_args[0][0]
        assert call_args[0:2] == ["git", "worktree"]
        assert "123-feature" in call_args

    def test_create_worktree_serializes_git_metadata_mutation(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Independent subprocesses must not race on shared git worktree metadata."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()
        lock_paths: list[Path] = []

        @contextmanager
        def fake_file_lock(path: Path) -> Iterator[None]:
            lock_paths.append(path)
            yield

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch("hephaestus.automation.worktree_manager.file_lock", side_effect=fake_file_lock),
        ):
            manager.create_worktree(123, "123-feature")

        assert lock_paths == [manager.base_dir / ".git-metadata.lock"]

    def test_create_worktree_default_branch_name(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test worktree creation with default branch name."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        manager.create_worktree(456)

        # Should use default branch name
        call_args = worktree_mocks.run.call_args[0][0]
        assert "456-auto" in call_args

    def test_create_worktree_already_exists(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test creating worktree when already exists."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            # Create first worktree
            path1 = manager.create_worktree(123, "123-feature")

            # Try to create same worktree again
            path2 = manager.create_worktree(123, "123-feature")

        assert path1 == path2
        # First creation: base detection, local rev-parse, ls-remote (branch
        # absent locally), worktree add. Second creation returns early.
        assert worktree_mocks.run.call_count == 4

    @patch("hephaestus.automation.worktree_manager.shutil.rmtree")
    def test_create_worktree_removes_stale_directory(
        self, mock_rmtree: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test that stale directories are removed before creation."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        # Create mock path that exists
        with patch.object(Path, "exists", return_value=True):
            manager.create_worktree(123, "123-feature")

        # Should try git worktree remove first
        git_remove_calls = [
            c
            for c in worktree_mocks.run.call_args_list
            if "worktree" in c[0][0] and "remove" in c[0][0]
        ]
        assert len(git_remove_calls) >= 1

        # Should call prune after
        prune_calls = [c for c in worktree_mocks.run.call_args_list if "prune" in c[0][0]]
        assert len(prune_calls) >= 1

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=False)
    def test_create_worktree_reuses_registered_dirty_existing_path(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """A rerun must preserve dirty work left in build/.worktrees/issue-N."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()
        worktree_path = manager.base_dir / "issue-1109"
        worktree_path.mkdir(parents=True)
        (worktree_path / "changed.py").write_text("dirty work")
        scratch = worktree_path / ".claude-prompt-1109.md"
        scratch.write_text("generated prompt")

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(
                manager,
                "list_worktrees",
                return_value=[{"path": str(worktree_path), "branch": "refs/heads/1109-auto"}],
            ),
            patch.object(manager, "_add_worktree_for_branch") as mock_add,
        ):
            result = manager.create_worktree(1109, "1109-auto")

        assert result == worktree_path
        assert manager.worktrees[1109] == worktree_path
        assert not scratch.exists()
        assert (worktree_path / "changed.py").exists()
        mock_add.assert_not_called()

    def test_create_worktree_refuses_non_worktree_path_with_contents(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Unknown non-empty directories are preserved instead of rmtree'd."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()
        worktree_path = manager.base_dir / "issue-1109"
        worktree_path.mkdir(parents=True)
        (worktree_path / "local-file.txt").write_text("do not delete")

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(manager, "list_worktrees", return_value=[]),
            pytest.raises(RuntimeError, match="not a registered git worktree"),
        ):
            manager.create_worktree(1109, "1109-auto")

        assert (worktree_path / "local-file.txt").exists()

    def test_create_worktree_extends_remote_branch(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Branch absent locally but on origin → extend origin/<branch> (#1018)."""
        worktree_mocks.repo_root.return_value = tmp_path

        # base_branch is never accessed on the remote-extend path, so no
        # symbolic-ref detection call fires: order is rev-parse, ls-remote,
        # fetch, worktree-add.
        rev_parse = MagicMock(returncode=1)  # branch NOT present locally
        ls_remote = MagicMock(
            returncode=0,
            stdout="deadbeef\trefs/heads/768-auto-impl\n",  # present on origin
        )
        fetch = MagicMock(returncode=0)
        add = MagicMock(returncode=0)
        worktree_mocks.run.side_effect = [rev_parse, ls_remote, fetch, add]

        manager = WorktreeManager()
        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            manager.create_worktree(768, "768-auto-impl")

        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        # A fetch of the remote branch must occur.
        assert any(a[:2] == ["git", "fetch"] and "768-auto-impl" in a for a in argvs)
        # The worktree-add must use origin/<branch>, NOT the base branch.
        add_argv = next(a for a in argvs if a[:3] == ["git", "worktree", "add"])
        assert "origin/768-auto-impl" in add_argv
        assert "origin/main" not in add_argv

    def test_create_worktree_falls_back_to_base_when_no_remote_branch(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Branch absent locally AND on origin → new branch from base (#1018)."""
        worktree_mocks.repo_root.return_value = tmp_path

        # The base path accesses self.base_branch, which lazily triggers
        # symbolic-ref detection AFTER the rev-parse and ls-remote checks.
        rev_parse = MagicMock(returncode=1)  # not local
        ls_remote = MagicMock(returncode=0, stdout="")  # not on origin
        detect = MagicMock(stdout="origin/main")
        add = MagicMock(returncode=0)
        worktree_mocks.run.side_effect = [rev_parse, ls_remote, detect, add]

        manager = WorktreeManager()
        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            manager.create_worktree(999, "999-auto-impl")

        add_argv = next(
            c[0][0]
            for c in worktree_mocks.run.call_args_list
            if c[0][0][:3] == ["git", "worktree", "add"]
        )
        # New-branch-from-base path preserved.
        assert "-b" in add_argv
        assert "999-auto-impl" in add_argv
        assert "origin/main" in add_argv

    def test_create_worktree_prefers_local_branch_over_remote(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """A locally-present branch is reused without any ls-remote call (#1018)."""
        worktree_mocks.repo_root.return_value = tmp_path

        # Local branch present → reuse path; base_branch is never accessed, so
        # there is no detection call and no ls-remote: order is rev-parse, add.
        rev_parse = MagicMock(returncode=0)  # branch present locally
        add = MagicMock(returncode=0)
        worktree_mocks.run.side_effect = [rev_parse, add]

        manager = WorktreeManager()
        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            manager.create_worktree(123, "123-feature")

        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        assert not any("ls-remote" in a for a in argvs)

    def test_create_worktree_failure(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test worktree creation failure."""
        worktree_mocks.repo_root.return_value = tmp_path
        # First call is base-branch detection (symbolic-ref) — must succeed.
        # All subsequent calls (rev-parse, worktree add) raise CalledProcessError.
        success_result = MagicMock()
        success_result.stdout = "origin/main"
        worktree_mocks.run.side_effect = [
            success_result,  # symbolic-ref → succeeds (base branch detected)
            subprocess.CalledProcessError(1, "git"),  # rev-parse check for branch
        ]

        manager = WorktreeManager()

        with pytest.raises(RuntimeError, match="Failed to create worktree"):
            manager.create_worktree(123, "123-feature")

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_remove_worktree_success(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test successful worktree removal."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        # Add worktree to tracked list (directory must exist on disk so removal
        # runs `git worktree remove` rather than the idempotent already-gone path).
        worktree_path = manager.base_dir / "issue-123"
        worktree_path.mkdir(parents=True)
        manager.worktrees[123] = worktree_path

        manager.remove_worktree(123)

        assert 123 not in manager.worktrees
        # Detection is lazy: only the remove call should fire here.
        assert worktree_mocks.run.call_count == 1
        call_args = worktree_mocks.run.call_args[0][0]
        assert call_args[0:3] == ["git", "worktree", "remove"]

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_remove_worktree_force(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test forced worktree removal."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        worktree_path = manager.base_dir / "issue-123"
        worktree_path.mkdir(parents=True)
        manager.worktrees[123] = worktree_path

        manager.remove_worktree(123, force=True)

        call_args = worktree_mocks.run.call_args[0][0]
        assert "--force" in call_args

    def test_remove_worktree_not_found(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test removing non-existent worktree."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        # Should not crash
        manager.remove_worktree(999)

        # Detection is lazy and remove() short-circuits when not tracked,
        # so no git calls fire.
        assert worktree_mocks.run.call_count == 0

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_remove_worktree_failure(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test worktree removal failure."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        worktree_path = manager.base_dir / "issue-123"
        worktree_path.mkdir(parents=True)
        manager.worktrees[123] = worktree_path

        worktree_mocks.run.side_effect = subprocess.CalledProcessError(1, "git")

        with pytest.raises(RuntimeError, match="Failed to remove worktree"):
            manager.remove_worktree(123)

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=False)
    def test_remove_worktree_dirty_raises(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Removing a dirty worktree without force raises WorktreeDirtyError."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        worktree_path = manager.base_dir / "issue-42"
        worktree_path.mkdir(parents=True)
        manager.worktrees[42] = worktree_path

        with pytest.raises(WorktreeDirtyError) as exc_info:
            manager.remove_worktree(42)

        assert exc_info.value.issue_number == 42
        assert exc_info.value.path == worktree_path
        # git worktree remove should NOT have been called
        remove_calls = [
            call
            for call in worktree_mocks.run.call_args_list
            if len(call[0][0]) >= 3 and call[0][0][:3] == ["git", "worktree", "remove"]
        ]
        assert remove_calls == []

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=False)
    def test_cleanup_all_preserves_dirty_worktree(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """cleanup_all skips dirty worktrees and records them in self.preserved."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        dirty_path = manager.base_dir / "issue-1"
        dirty_path.mkdir(parents=True)
        manager.worktrees[1] = dirty_path

        manager.cleanup_all()

        assert len(manager.preserved) == 1
        assert manager.preserved[0] == (1, dirty_path)

    def test_get_worktree(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test getting worktree path."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        worktree_path = manager.base_dir / "issue-123"
        manager.worktrees[123] = worktree_path

        assert manager.get_worktree(123) == worktree_path
        assert manager.get_worktree(999) is None

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_cleanup_all(self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test cleaning up all worktrees."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        # Add multiple worktrees (dirs must exist so each runs a real removal).
        for num in (123, 456, 789):
            path = manager.base_dir / f"issue-{num}"
            path.mkdir(parents=True)
            manager.worktrees[num] = path

        manager.cleanup_all()

        # All should be removed
        assert len(manager.worktrees) == 0
        # Should call git worktree remove for each
        assert worktree_mocks.run.call_count >= 3

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_cleanup_all_with_failures(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test cleanup continues even if some removals fail."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        for num in (123, 456):
            path = manager.base_dir / f"issue-{num}"
            path.mkdir(parents=True)
            manager.worktrees[num] = path

        # First removal fails, second succeeds
        worktree_mocks.run.side_effect = [
            subprocess.CalledProcessError(1, "git"),
            Mock(),
        ]

        # Should not crash
        manager.cleanup_all()

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_remove_worktree_missing_dir_is_idempotent(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Removing a worktree whose directory is already gone succeeds (#1532).

        Regression for the `[Errno 2] No such file or directory` failures: a
        missing directory must be treated as already-removed (drop the key,
        prune metadata) rather than running `git worktree remove` on a gone dir.
        """
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        # Registered but never created on disk (the post-first-removal alias case).
        gone_path = manager.base_dir / "issue-28"
        manager.worktrees[28] = gone_path

        manager.remove_worktree(28)

        assert 28 not in manager.worktrees
        # No `git worktree remove` fired; only the metadata prune.
        assert all(
            call.args[0][0:3] != ["git", "worktree", "remove"]
            for call in worktree_mocks.run.call_args_list
        )
        assert any(
            call.args[0] == ["git", "worktree", "prune"]
            for call in worktree_mocks.run.call_args_list
        )

    @patch("hephaestus.automation.worktree_manager.is_clean_working_tree", return_value=True)
    def test_cleanup_all_dedups_aliased_paths(
        self, mock_clean: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Several issue keys aliasing one path remove it once, no errors (#1532).

        Reproduces the branch-reuse aliasing (issues #12/#29/#65 sharing the
        issue-28 worktree): the shared directory is removed exactly once and the
        aliased registrations are dropped without re-running removal on a gone dir.
        """
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        shared = manager.base_dir / "issue-28"
        shared.mkdir(parents=True)
        # 28 owns the dir; 12/29/65 alias the same path (branch-reuse).
        for num in (28, 12, 29, 65):
            manager.worktrees[num] = shared

        manager.cleanup_all()

        assert manager.worktrees == {}
        assert manager.preserved == []
        # `git worktree remove` runs exactly once for the shared directory.
        remove_calls = [
            call
            for call in worktree_mocks.run.call_args_list
            if call.args[0][0:3] == ["git", "worktree", "remove"]
        ]
        assert len(remove_calls) == 1

    def test_prune_worktrees(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test pruning stale worktree metadata."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        manager.prune_worktrees()

        # Detection is lazy: only the prune call should fire here.
        assert worktree_mocks.run.call_count == 1
        call_args = worktree_mocks.run.call_args[0][0]
        assert call_args == ["git", "worktree", "prune"]

    def test_list_worktrees(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test listing all worktrees."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        mock_result = Mock()
        mock_result.stdout = """worktree /repo
HEAD abc123
branch refs/heads/main

worktree /repo/build/.worktrees/issue-123
HEAD def456
branch refs/heads/123-feature
"""
        worktree_mocks.run.return_value = mock_result

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 2
        assert worktrees[0]["path"] == "/repo"
        assert worktrees[0]["branch"] == "refs/heads/main"
        assert worktrees[1]["path"] == "/repo/build/.worktrees/issue-123"
        assert worktrees[1]["branch"] == "refs/heads/123-feature"

    def test_ensure_branch_deleted(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """Test deleting branch from local and remote."""
        worktree_mocks.repo_root.return_value = tmp_path
        # Mock base branch auto-detection
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        manager.ensure_branch_deleted("feature-branch")

        # Detection is lazy: only local delete + remote delete fire.
        assert worktree_mocks.run.call_count == 2
        # Check local delete (first call now)
        local_call = worktree_mocks.run.call_args_list[0][0][0]
        assert "branch" in local_call and "-D" in local_call
        # Check remote delete (second call now)
        remote_call = worktree_mocks.run.call_args_list[1][0][0]
        assert "push" in remote_call and "--delete" in remote_call

    def test_ensure_branch_deleted_handles_failure(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Test branch deletion handles failures gracefully."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()

        # Both deletes fail but shouldn't crash
        worktree_mocks.run.side_effect = subprocess.CalledProcessError(1, "git")

        # Should not raise
        manager.ensure_branch_deleted("feature-branch")


# ---------------------------------------------------------------------------
# #382/A4-05: base_branch silently defaulting to non-existent origin/main
# ---------------------------------------------------------------------------


class TestBaseBranchDetectionRaisesOnFailure:
    """Tests that WorktreeManager raises RuntimeError when base branch can't be detected.

    Detection is lazy: construction succeeds, the error fires on first
    ``base_branch`` access (or on the first ``create_worktree`` call, which
    reads the property).
    """

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_raises_when_no_candidates_exist(self, mock_get_root: Any, tmp_path: Any) -> None:
        """If symbolic-ref fails and neither origin/main nor origin/master exist, raise."""
        mock_get_root.return_value = tmp_path

        # All git calls fail: symbolic-ref, origin/main verify, origin/master verify
        with patch(
            "hephaestus.automation.worktree_manager.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            mgr = WorktreeManager()
            with pytest.raises(RuntimeError, match="Could not auto-detect the remote base branch"):
                _ = mgr.base_branch

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_no_longer_silently_defaults_to_origin_main(
        self, mock_get_root: Any, tmp_path: Any
    ) -> None:
        """Verify the old silent default (origin/main) is gone — raises instead."""
        mock_get_root.return_value = tmp_path

        with patch(
            "hephaestus.automation.worktree_manager.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            mgr = WorktreeManager()
            with pytest.raises(RuntimeError):
                _ = mgr.base_branch

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_explicit_base_branch_bypasses_detection(
        self, mock_get_root: Any, tmp_path: Any
    ) -> None:
        """Passing base_branch= explicitly skips auto-detection entirely."""
        mock_get_root.return_value = tmp_path

        # Even if git fails, passing base_branch= explicitly must succeed
        with patch(
            "hephaestus.automation.worktree_manager.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            mgr = WorktreeManager(base_branch="origin/custom")

        assert mgr.base_branch == "origin/custom"

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_loop_trunk_githash_bypasses_remote_detection(
        self, mock_get_root: Any, tmp_path: Any
    ) -> None:
        """Loop phases build issue worktrees from the exact validated trunk commit."""
        mock_get_root.return_value = tmp_path

        with (
            patch.dict("os.environ", {"HEPH_TRUNK_GITHASH": "330a7b1"}),
            patch(
                "hephaestus.automation.worktree_manager.run",
                side_effect=AssertionError("remote detection should not run"),
            ),
        ):
            mgr = WorktreeManager()

        assert mgr.base_branch == "330a7b1"

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_explicit_base_branch_overrides_loop_trunk_githash(
        self, mock_get_root: Any, tmp_path: Any
    ) -> None:
        """Manual callers can still force a specific base branch."""
        mock_get_root.return_value = tmp_path

        with patch.dict("os.environ", {"HEPH_TRUNK_GITHASH": "330a7b1"}):
            mgr = WorktreeManager(base_branch="origin/custom")

        assert mgr.base_branch == "origin/custom"

    @patch("hephaestus.automation.worktree_manager.get_repo_root")
    def test_construction_succeeds_when_detection_would_fail(
        self, mock_get_root: Any, tmp_path: Any
    ) -> None:
        """Constructing the manager must NOT eagerly detect the base branch.

        This guards against regressions where eager detection makes
        WorktreeManager unusable in test fixtures or other environments
        without origin/* refs.
        """
        mock_get_root.return_value = tmp_path

        with patch(
            "hephaestus.automation.worktree_manager.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            # Should not raise — detection is deferred
            mgr = WorktreeManager()

        assert mgr.repo_root == tmp_path


class TestCreateWorktreeBranchCollision:
    """create_worktree reuses an existing worktree when the branch is checked out there.

    Regression for the exit-128 collision: the implement loop resolves a PR's
    real head branch (e.g. 708-auto-impl) for a DIFFERENT issue (#725), but that
    branch is already checked out in the issue-708 worktree. git forbids the same
    branch in two worktrees, so `git worktree add` failed. Reuse the existing
    worktree instead of forcing or adding a second one.
    """

    def test_reuses_worktree_already_holding_branch(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        existing = manager.base_dir / "issue-708"

        # The branch 708-auto-impl is already checked out in the issue-708 worktree.
        with patch.object(
            manager,
            "list_worktrees",
            return_value=[
                {"path": str(existing), "branch": "refs/heads/708-auto-impl", "commit": "abc"},
            ],
        ):
            # Now ask for a worktree for issue #725 on that same branch.
            result = manager.create_worktree(725, "708-auto-impl")

        # Reuses the existing path, registers it under #725, and does NOT add a
        # second worktree for the same branch.
        assert result == existing
        assert manager.worktrees[725] == existing
        add_calls = [
            c
            for c in worktree_mocks.run.call_args_list
            if c[0] and c[0][0][:3] == ["git", "worktree", "add"]
        ]
        assert add_calls == [], "must NOT run `git worktree add` when reusing"

    def test_no_reuse_when_branch_not_checked_out_elsewhere(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """When the branch is NOT in any worktree, normal add path runs."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        with patch.object(manager, "list_worktrees", return_value=[]):
            result = manager.create_worktree(123, "123-auto-impl")

        assert result == manager.base_dir / "issue-123"
        add_calls = [
            c
            for c in worktree_mocks.run.call_args_list
            if c[0] and c[0][0][:3] == ["git", "worktree", "add"]
        ]
        assert len(add_calls) == 1, "fresh branch must add exactly one worktree"

    def test_branch_lookup_failure_aborts_before_worktree_add(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """If branch ownership cannot be listed, fail closed before adding."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()

        with patch.object(manager, "list_worktrees", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="Cannot safely determine"):
                manager.create_worktree(725, "708-auto-impl")

        add_calls = [
            c
            for c in worktree_mocks.run.call_args_list
            if c[0] and c[0][0][:3] == ["git", "worktree", "add"]
        ]
        assert add_calls == []

    def test_stale_local_branch_without_unique_commits_fast_forwards_to_base(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """A stale local issue branch should not make the implementer rework old code."""
        worktree_mocks.repo_root.return_value = tmp_path

        def fake_run(argv: list[str], **_: Any) -> MagicMock:
            if argv[:3] == ["git", "rev-parse", "--verify"]:
                return MagicMock(stdout="oldsha\n", returncode=0)
            if argv[:3] == ["git", "rev-list", "--left-right"]:
                return MagicMock(stdout="7 0\n", returncode=0)
            if argv[:2] == ["git", "symbolic-ref"]:
                return MagicMock(stdout="origin/main\n", returncode=0)
            return MagicMock(stdout="", returncode=0)

        worktree_mocks.run.side_effect = fake_run
        manager = WorktreeManager()

        with patch.object(manager, "list_worktrees", return_value=[]):
            manager.create_worktree(1109, "1109-auto-impl")

        argvs = [c.args[0] for c in worktree_mocks.run.call_args_list]
        add_argv = [
            "git",
            "worktree",
            "add",
            str(manager.base_dir / "issue-1109"),
            "1109-auto-impl",
        ]
        assert ["git", "branch", "-f", "1109-auto-impl", manager.base_branch] in argvs
        assert add_argv in argvs

    def test_local_branch_with_unique_commits_is_preserved(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """A branch with local work must not be forced back to the base branch."""
        worktree_mocks.repo_root.return_value = tmp_path

        def fake_run(argv: list[str], **_: Any) -> MagicMock:
            if argv[:3] == ["git", "rev-parse", "--verify"]:
                return MagicMock(stdout="localsha\n", returncode=0)
            if argv[:3] == ["git", "rev-list", "--left-right"]:
                return MagicMock(stdout="0 2\n", returncode=0)
            if argv[:2] == ["git", "symbolic-ref"]:
                return MagicMock(stdout="origin/main\n", returncode=0)
            return MagicMock(stdout="", returncode=0)

        worktree_mocks.run.side_effect = fake_run
        manager = WorktreeManager()

        with patch.object(manager, "list_worktrees", return_value=[]):
            manager.create_worktree(1109, "1109-auto-impl")

        argvs = [c.args[0] for c in worktree_mocks.run.call_args_list]
        add_argv = [
            "git",
            "worktree",
            "add",
            str(manager.base_dir / "issue-1109"),
            "1109-auto-impl",
        ]
        assert ["git", "branch", "-f", "1109-auto-impl", "origin/main"] not in argvs
        assert add_argv in argvs

    def test_worktree_holding_branch_matches_full_ref(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """_worktree_holding_branch matches on refs/heads/<name>, returns the path."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()
        p = manager.base_dir / "issue-708"
        with patch.object(
            manager,
            "list_worktrees",
            return_value=[{"path": str(p), "branch": "refs/heads/708-auto-impl", "commit": "x"}],
        ):
            assert manager._worktree_holding_branch("708-auto-impl") == p
            assert manager._worktree_holding_branch("999-auto-impl") is None

    def test_refresh_base_branch_refetches_and_redetects(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """refresh_base_branch fetches origin and clears the cached base (#1560)."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()
        # Prime the cache via first access.
        assert manager.base_branch == "origin/main"
        worktree_mocks.run.reset_mock()

        result = manager.refresh_base_branch()

        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        assert ["git", "fetch", "origin"] in argvs, argvs
        assert result == "origin/main"

    def test_refresh_base_branch_noop_when_pinned(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """A pinned base (explicit/HEPH_TRUNK_GITHASH) is never moved by refresh."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager(base_branch="deadbeef")
        worktree_mocks.run.reset_mock()

        result = manager.refresh_base_branch()

        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        assert all(a[:2] != ["git", "fetch"] for a in argvs), argvs
        assert result == "deadbeef"

    def test_create_worktree_refresh_base_fetches(self, worktree_mocks: Any, tmp_path: Any) -> None:
        """create_worktree(refresh_base=True) fetches origin before adding (#1560)."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value.stdout = "origin/main"
        manager = WorktreeManager()
        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            manager.create_worktree(123, "123-feature", refresh_base=True)
        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        assert ["git", "fetch", "origin"] in argvs, argvs

    def test_create_worktree_refresh_base_ignores_loop_trunk_pin(
        self, worktree_mocks: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue-major workers branch from fresh origin/main, not loop-start trunk."""
        monkeypatch.setenv("HEPH_TRUNK_GITHASH", "3883866")
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value = Mock(returncode=1, stdout="origin/main")
        manager = WorktreeManager()

        with patch.object(manager, "_worktree_holding_branch", return_value=None):
            manager.create_worktree(1420, "1420-auto-impl", refresh_base=True)

        argvs = [c[0][0] for c in worktree_mocks.run.call_args_list]
        assert ["git", "fetch", "origin"] in argvs, argvs
        add_calls = [argv for argv in argvs if argv[:3] == ["git", "worktree", "add"]]
        assert add_calls
        assert add_calls[-1][-1] == "origin/main"

    @patch("hephaestus.automation.worktree_manager.rebase_worktree_onto")
    def test_refresh_base_rebases_existing_local_issue_branch(
        self, mock_rebase: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Issue-major reruns rebase a reused local issue branch before implementation."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value = Mock(returncode=0, stdout="origin/main")
        mock_rebase.return_value = True
        manager = WorktreeManager()

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(manager, "_local_branch_exists", return_value=True),
        ):
            manager.create_worktree(1577, "1577-auto-impl", refresh_base=True)

        mock_rebase.assert_called_once_with(manager.base_dir / "issue-1577", "main")

    @patch("hephaestus.automation.worktree_manager.rebase_worktree_onto")
    def test_refresh_base_rebase_conflict_keeps_reused_issue_branch(
        self,
        mock_rebase: Any,
        worktree_mocks: Any,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rebase conflicts proceed with the reused branch at its current head."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value = Mock(returncode=0, stdout="origin/main")
        mock_rebase.return_value = False
        manager = WorktreeManager()

        with (
            caplog.at_level("WARNING", logger="hephaestus.automation.worktree_manager"),
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(manager, "_local_branch_exists", return_value=True),
        ):
            result = manager.create_worktree(1577, "1577-auto-impl", refresh_base=True)

        assert result == manager.base_dir / "issue-1577"
        assert manager.worktrees[1577] == manager.base_dir / "issue-1577"
        mock_rebase.assert_called_once_with(manager.base_dir / "issue-1577", "main")
        assert "proceeding with current branch head" in caplog.text

    @patch("hephaestus.automation.worktree_manager.rebase_worktree_onto")
    def test_refresh_base_rebases_existing_remote_issue_branch(
        self, mock_rebase: Any, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Issue-major reruns rebase a reused remote issue branch before implementation."""
        worktree_mocks.repo_root.return_value = tmp_path
        worktree_mocks.run.return_value = Mock(returncode=0, stdout="origin/main")
        mock_rebase.return_value = True
        manager = WorktreeManager()

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(manager, "_local_branch_exists", return_value=False),
            patch.object(manager, "_remote_branch_exists", return_value=True),
        ):
            manager.create_worktree(1580, "1580-auto-impl", refresh_base=True)

        mock_rebase.assert_called_once_with(manager.base_dir / "issue-1580", "main")

    def test_create_worktree_removes_partial_worktree_after_add_failure(
        self, worktree_mocks: Any, tmp_path: Any
    ) -> None:
        """Failures after git worktree add must not leak a registered worktree."""
        worktree_mocks.repo_root.return_value = tmp_path
        manager = WorktreeManager()
        worktree_path = manager.base_dir / "issue-1797"

        def fail_after_add(path: Path, *_: Any, **__: Any) -> None:
            path.mkdir(parents=True)
            raise RuntimeError("rebase failed")

        with (
            patch.object(manager, "_worktree_holding_branch", return_value=None),
            patch.object(manager, "_add_worktree_for_branch", side_effect=fail_after_add),
            pytest.raises(RuntimeError, match="Failed to create worktree"),
        ):
            manager.create_worktree(1797, "1797-auto-impl")

        assert 1797 not in manager.worktrees
        assert not worktree_path.exists()
        argvs = [call.args[0] for call in worktree_mocks.run.call_args_list]
        assert ["git", "worktree", "remove", "--force", str(worktree_path)] in argvs
        assert ["git", "worktree", "prune"] in argvs
