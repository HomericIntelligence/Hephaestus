"""Git utility functions for repository operations.

Provides helpers for:
- Repository root discovery
- Repository owner/name detection
- Safe git operations with error handling
- Git lock cleanup
"""

import logging
import subprocess
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root as _get_repo_root
from hephaestus.utils.helpers import run_subprocess
from hephaestus.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    capture_output: bool = True,
    check: bool = True,
    timeout: int | None = None,
    log_errors: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command with consistent error handling.

    Args:
        cmd: Command and arguments as list
        cwd: Working directory (defaults to current)
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit
        timeout: Optional timeout in seconds
        log_errors: If False, suppress ERROR logging on failure. Use when
            the caller expects and handles the failure itself.

    Returns:
        CompletedProcess instance

    Raises:
        subprocess.CalledProcessError: If check=True and command fails
        subprocess.TimeoutExpired: If timeout is exceeded

    """
    logger.debug("Running command: %s", " ".join(cmd))
    return run_subprocess(
        cmd,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        check=check,
        log_on_error=log_errors,
    )


def get_repo_root(path: Path | None = None) -> Path:
    """Find the git repository root directory.

    Args:
        path: Starting path for search (defaults to cwd)

    Returns:
        Path to repository root

    Raises:
        RuntimeError: If not in a git repository

    """
    return _get_repo_root(path)


def get_repo_info(repo_root: Path | None = None) -> tuple[str, str]:
    """Get repository owner and name from git remote.

    Args:
        repo_root: Repository root (defaults to auto-detect)

    Returns:
        Tuple of (owner, repo_name)

    Raises:
        RuntimeError: If unable to determine repo info

    """
    if repo_root is None:
        repo_root = get_repo_root()

    try:
        result = run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        remote_url = result.stdout.strip()

        # Parse various git URL formats
        # SSH: git@github.com:owner/repo.git
        # HTTPS: https://github.com/owner/repo.git
        if "@" in remote_url and ":" in remote_url:
            # SSH format
            parts = remote_url.split(":")[-1].replace(".git", "").split("/")
            owner, repo = parts[-2], parts[-1]
        elif remote_url.startswith("https://"):
            # HTTPS format
            parts = remote_url.replace(".git", "").split("/")
            owner, repo = parts[-2], parts[-1]
        else:
            raise RuntimeError(f"Unable to parse git remote URL: {remote_url}")

        logger.debug("Detected repo: %s/%s", owner, repo)
        return owner, repo

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git remote URL: {e}") from e


def safe_git_fetch(repo_root: Path, retries: int = 3) -> bool:
    """Safely fetch from git remote with retry and exponential backoff.

    Uses the retry_with_backoff decorator for consistent retry behavior
    with jitter to prevent thundering herd problems.

    Args:
        repo_root: Repository root directory
        retries: Number of retry attempts

    Returns:
        True if fetch succeeded, False otherwise

    """

    @retry_with_backoff(
        max_retries=retries,
        initial_delay=1.0,
        backoff_factor=2,
        retry_on=(subprocess.CalledProcessError, subprocess.TimeoutExpired),
        logger=logger.warning,
        jitter=True,
    )
    def _fetch() -> bool:
        run(
            ["git", "fetch", "origin"],
            cwd=repo_root,
            timeout=30,
        )
        logger.debug("Git fetch succeeded")
        return True

    try:
        return _fetch()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.error("Git fetch failed after all retries")
        return False


def clean_stale_git_locks(repo_root: Path) -> None:
    """Remove stale git lock files.

    Args:
        repo_root: Repository root directory

    """
    git_dir = repo_root / ".git"
    lock_files = [
        git_dir / "index.lock",
        git_dir / "HEAD.lock",
        git_dir / "refs" / "heads" / "*.lock",
    ]

    for lock_pattern in lock_files:
        if "*" in str(lock_pattern):
            # Handle glob patterns
            parent = lock_pattern.parent
            pattern = lock_pattern.name
            if parent.exists():
                for lock_file in parent.glob(pattern):
                    if lock_file.exists():
                        logger.warning("Removing stale git lock: %s", lock_file)
                        try:
                            lock_file.unlink()
                        except OSError as e:
                            logger.error("Failed to remove lock %s: %s", lock_file, e)
        else:
            # Handle direct paths
            if lock_pattern.exists():
                logger.warning("Removing stale git lock: %s", lock_pattern)
                try:
                    lock_pattern.unlink()
                except OSError as e:
                    logger.error("Failed to remove lock %s: %s", lock_pattern, e)


def get_current_branch(repo_root: Path | None = None) -> str:
    """Get the current git branch name.

    Args:
        repo_root: Repository root (defaults to auto-detect)

    Returns:
        Branch name

    Raises:
        RuntimeError: If unable to determine branch

    """
    if repo_root is None:
        repo_root = get_repo_root()

    try:
        result = run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get current branch: {e}") from e


def is_clean_working_tree(repo_root: Path | None = None) -> bool:
    """Check if the working tree is clean (no uncommitted changes).

    Args:
        repo_root: Repository root (defaults to auto-detect)

    Returns:
        True if working tree is clean

    """
    if repo_root is None:
        repo_root = get_repo_root()

    try:
        result = run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        return len(result.stdout.strip()) == 0
    except subprocess.CalledProcessError:
        return False
