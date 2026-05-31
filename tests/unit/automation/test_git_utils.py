"""Tests for git utility functions."""

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.git_utils import (
    clear_repo_caches,
    get_current_branch,
    get_repo_info,
    get_repo_root,
    is_clean_working_tree,
    push_current_branch_with_lease_on_divergence,
    run,
    safe_git_fetch,
    sync_worktree_to_remote_branch,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> Generator[None, None, None]:
    """Clear repo caches before each test to avoid cross-test interference."""
    clear_repo_caches()
    yield
    clear_repo_caches()


class TestRun:
    """Tests for run function."""

    def test_successful_command(self) -> None:
        """Test running a successful command."""
        result = run(["echo", "hello"], check=True, capture_output=True)

        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_failed_command_with_check(self) -> None:
        """Test running a failed command with check=True."""
        with pytest.raises(subprocess.CalledProcessError):
            run(["false"], check=True)

    def test_failed_command_without_check(self) -> None:
        """Test running a failed command with check=False."""
        result = run(["false"], check=False)
        assert result.returncode != 0

    def test_with_cwd(self, tmp_path: Any) -> None:
        """Test running command with custom working directory."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = run(["ls", "test.txt"], cwd=tmp_path, capture_output=True)

        assert result.returncode == 0
        assert "test.txt" in result.stdout


class TestGetRepoRoot:
    """Tests for get_repo_root function."""

    @patch("hephaestus.automation.git_utils._get_repo_root")
    def test_successful_detection(self, mock_get_root: Any) -> None:
        """Test successful repository root detection."""
        mock_get_root.return_value = Path("/home/user/repo")

        root = get_repo_root()

        assert root == Path("/home/user/repo")
        mock_get_root.assert_called_once()

    def test_returns_path(self, tmp_path: Any) -> None:
        """Test that get_repo_root returns a Path object."""
        root = get_repo_root(tmp_path)
        assert isinstance(root, Path)


class TestGetRepoInfo:
    """Tests for get_repo_info function."""

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_ssh_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test parsing SSH URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        owner, repo = get_repo_info()

        assert owner == "owner"
        assert repo == "repo"

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_https_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test parsing HTTPS URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "https://github.com/owner/repo.git\n"
        mock_run.return_value = mock_result

        owner, repo = get_repo_info()

        assert owner == "owner"
        assert repo == "repo"

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_invalid_url_format(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test handling invalid URL format."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "invalid-url\n"
        mock_run.return_value = mock_result

        with pytest.raises(RuntimeError, match="Unable to parse git remote URL"):
            get_repo_info()

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_result_caching_prevents_repeated_run_calls(
        self, mock_get_root: Any, mock_run: Any
    ) -> None:
        """Test that repeated get_repo_info calls use cached result."""
        repo_root = Path("/home/user/repo")
        mock_get_root.return_value = repo_root
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        # First call should invoke run() and cache the result
        owner1, repo1 = get_repo_info(repo_root)
        assert owner1 == "owner"
        assert repo1 == "repo"
        assert mock_run.call_count == 1

        # Second call with same repo_root should return cached result without calling run()
        owner2, repo2 = get_repo_info(repo_root)
        assert owner2 == "owner"
        assert repo2 == "repo"
        assert mock_run.call_count == 1  # Should not increase

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_clear_repo_caches_forces_re_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test that clear_repo_caches forces re-detection on next call."""
        repo_root = Path("/home/user/repo")
        mock_get_root.return_value = repo_root
        mock_result = Mock()
        mock_result.stdout = "git@github.com:owner/repo.git\n"
        mock_run.return_value = mock_result

        # First call caches the result
        get_repo_info(repo_root)
        assert mock_run.call_count == 1

        # Clear caches
        clear_repo_caches()

        # Next call should invoke run() again
        get_repo_info(repo_root)
        assert mock_run.call_count == 2


class TestGetCurrentBranch:
    """Tests for get_current_branch function."""

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_successful_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test successful branch detection."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = "main\n"
        mock_run.return_value = mock_result

        branch = get_current_branch()

        assert branch == "main"

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_failed_detection(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test failed branch detection."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")

        with pytest.raises(RuntimeError, match="Failed to get current branch"):
            get_current_branch()


class TestIsCleanWorkingTree:
    """Tests for is_clean_working_tree function."""

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_clean_tree(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test clean working tree."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        assert is_clean_working_tree() is True

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_dirty_tree(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test dirty working tree."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_result = Mock()
        mock_result.stdout = " M modified_file.txt\n"
        mock_run.return_value = mock_result

        assert is_clean_working_tree() is False

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.automation.git_utils.get_repo_root")
    def test_error_returns_false(self, mock_get_root: Any, mock_run: Any) -> None:
        """Test error returns False."""
        mock_get_root.return_value = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")

        assert is_clean_working_tree() is False


class TestSafeGitFetch:
    """Tests for safe_git_fetch function."""

    @patch("hephaestus.automation.git_utils.run")
    def test_successful_fetch(self, mock_run: Any) -> None:
        """Test successful git fetch."""
        repo_root = Path("/home/user/repo")

        result = safe_git_fetch(repo_root, retries=1)

        assert result is True
        mock_run.assert_called_once()

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.utils.retry.time.sleep")
    def test_retry_on_failure(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test retry on fetch failure."""
        repo_root = Path("/home/user/repo")
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),
            subprocess.CalledProcessError(1, "git"),
            Mock(),  # Success on third try
        ]

        result = safe_git_fetch(repo_root, retries=3)

        assert result is True
        assert mock_run.call_count == 3

    @patch("hephaestus.automation.git_utils.run")
    @patch("hephaestus.utils.retry.time.sleep")
    def test_all_retries_fail(self, mock_sleep: Any, mock_run: Any) -> None:
        """Test when all retries fail."""
        repo_root = Path("/home/user/repo")
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")

        result = safe_git_fetch(repo_root, retries=2)

        assert result is False
        # With retry_with_backoff(max_retries=2), it runs initial + 2 retries = 3
        assert mock_run.call_count == 3


class TestPushCurrentBranchWithLeaseOnDivergence:
    """Tests for push_current_branch_with_lease_on_divergence."""

    @patch("hephaestus.automation.git_utils.run")
    def test_happy_path_plain_push(self, mock_run: Any) -> None:
        """Successful initial push: no fetch, no force, single git invocation."""
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        result = push_current_branch_with_lease_on_divergence(worktree)

        assert result is mock_run.return_value
        assert mock_run.call_count == 1
        args, _kwargs = mock_run.call_args
        assert args[0] == ["git", "push", "origin", "HEAD"]

    @patch("hephaestus.automation.git_utils.run")
    def test_non_fast_forward_triggers_fetch_and_lease(self, mock_run: Any) -> None:
        """non-fast-forward rejection → fetch + force-with-lease retry succeeds."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr=" ! [rejected]   HEAD -> 511-impl (non-fast-forward)\n",
        )
        # Sequence: push fails, get_current_branch, fetch, lease push (all succeed).
        mock_run.side_effect = [
            push_err,
            Mock(returncode=0, stdout="511-impl\n"),  # get_current_branch
            Mock(returncode=0),  # fetch
            Mock(returncode=0),  # lease push
        ]

        result = push_current_branch_with_lease_on_divergence(worktree)

        assert result.returncode == 0
        assert mock_run.call_count == 4
        # 3rd call must be a fetch of the diverged branch.
        fetch_args, _ = mock_run.call_args_list[2]
        assert fetch_args[0] == ["git", "fetch", "origin", "511-impl"]
        # 4th call must be the lease-protected push.
        lease_args, _ = mock_run.call_args_list[3]
        assert lease_args[0] == [
            "git",
            "push",
            "--force-with-lease=511-impl",
            "origin",
            "HEAD:511-impl",
        ]

    @patch("hephaestus.automation.git_utils.run")
    def test_fetch_first_also_triggers_lease(self, mock_run: Any) -> None:
        """'fetch first' rejection text triggers the same retry path."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr=" ! [rejected]   HEAD -> 43-impl (fetch first)\n",
        )
        mock_run.side_effect = [
            push_err,
            Mock(returncode=0, stdout="43-impl\n"),
            Mock(returncode=0),
            Mock(returncode=0),
        ]

        push_current_branch_with_lease_on_divergence(worktree)
        # Confirm the retry path executed (4 git invocations).
        assert mock_run.call_count == 4

    @patch("hephaestus.automation.git_utils.run")
    def test_branch_arg_skips_get_current_branch(self, mock_run: Any) -> None:
        """If caller passes branch=, no rev-parse call is needed for lease retry."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="non-fast-forward\n",
        )
        # Only 3 calls now: failed push, fetch, lease push.
        mock_run.side_effect = [push_err, Mock(returncode=0), Mock(returncode=0)]

        push_current_branch_with_lease_on_divergence(worktree, branch="577-nomad-vessel-groups")

        assert mock_run.call_count == 3
        fetch_args, _ = mock_run.call_args_list[1]
        assert fetch_args[0] == ["git", "fetch", "origin", "577-nomad-vessel-groups"]
        lease_args, _ = mock_run.call_args_list[2]
        assert "--force-with-lease=577-nomad-vessel-groups" in lease_args[0]

    @patch("hephaestus.automation.git_utils.run")
    def test_unrelated_push_failure_raises(self, mock_run: Any) -> None:
        """Auth/network failures (not divergence) propagate without retry."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            128,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="fatal: could not read Username for 'https://github.com'\n",
        )
        mock_run.side_effect = [push_err]

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            push_current_branch_with_lease_on_divergence(worktree)
        # The original (non-divergence) error is re-raised — not silently swallowed.
        assert exc_info.value.returncode == 128
        assert mock_run.call_count == 1

    @patch("hephaestus.automation.git_utils.run")
    def test_lease_push_failure_propagates(self, mock_run: Any) -> None:
        """If the lease-protected retry also fails, the caller sees that error."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD"],
            output="",
            stderr="non-fast-forward\n",
        )
        lease_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "--force-with-lease=511-impl", "origin", "HEAD:511-impl"],
            output="",
            stderr="stale info\n",
        )
        mock_run.side_effect = [push_err, Mock(returncode=0), lease_err]

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            push_current_branch_with_lease_on_divergence(worktree, branch="511-impl")
        assert exc_info.value.stderr == "stale info\n"

    @patch("hephaestus.automation.git_utils.run")
    def test_explicit_push_ref_used_on_initial_push(self, mock_run: Any) -> None:
        """When push_ref is set, the initial push uses it as the refspec (#832)."""
        # Without this, a Claude-side branch switch would route the push to a
        # stray branch instead of the PR's head.
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        push_current_branch_with_lease_on_divergence(
            worktree, branch="5391-auto-impl", push_ref="HEAD:5391-auto-impl"
        )

        args, _ = mock_run.call_args
        assert args[0] == ["git", "push", "origin", "HEAD:5391-auto-impl"]

    @patch("hephaestus.automation.git_utils.run")
    def test_explicit_push_ref_preserved_in_lease_retry(self, mock_run: Any) -> None:
        """The lease-retry path must use the same explicit push_ref (#832)."""
        worktree = Path("/tmp/worktree-xyz")
        push_err = subprocess.CalledProcessError(
            1,
            ["git", "push", "origin", "HEAD:5391-auto-impl"],
            output="",
            stderr="non-fast-forward\n",
        )
        mock_run.side_effect = [push_err, Mock(returncode=0), Mock(returncode=0)]

        push_current_branch_with_lease_on_divergence(
            worktree, branch="5391-auto-impl", push_ref="HEAD:5391-auto-impl"
        )

        # 3rd call must be the lease push using the explicit refspec.
        lease_args, _ = mock_run.call_args_list[2]
        assert lease_args[0] == [
            "git",
            "push",
            "--force-with-lease=5391-auto-impl",
            "origin",
            "HEAD:5391-auto-impl",
        ]


class TestSyncWorktreeToRemoteBranch:
    """Tests for sync_worktree_to_remote_branch (#832 — reset before agent)."""

    @patch("hephaestus.automation.git_utils.run")
    def test_fetches_then_resets_to_remote_head(self, mock_run: Any) -> None:
        """Runs ``git fetch origin <branch>`` then ``git reset --hard origin/<branch>``."""
        mock_run.return_value = Mock(returncode=0)
        worktree = Path("/tmp/worktree-xyz")

        sync_worktree_to_remote_branch(worktree, "5450-auto-impl")

        assert mock_run.call_count == 2
        fetch_args, fetch_kwargs = mock_run.call_args_list[0]
        assert fetch_args[0] == ["git", "fetch", "origin", "5450-auto-impl"]
        assert fetch_kwargs["cwd"] == worktree
        reset_args, reset_kwargs = mock_run.call_args_list[1]
        assert reset_args[0] == ["git", "reset", "--hard", "origin/5450-auto-impl"]
        assert reset_kwargs["cwd"] == worktree

    @patch("hephaestus.automation.git_utils.run")
    def test_fetch_failure_propagates(self, mock_run: Any) -> None:
        """If fetch fails, raise — we cannot safely reset to a stale ref."""
        fetch_err = subprocess.CalledProcessError(
            128,
            ["git", "fetch"],
            output="",
            stderr="fatal: unable to access remote\n",
        )
        mock_run.side_effect = [fetch_err]

        with pytest.raises(subprocess.CalledProcessError):
            sync_worktree_to_remote_branch(Path("/tmp/worktree-xyz"), "any-branch")
        # Reset must NOT have run after a fetch failure.
        assert mock_run.call_count == 1
