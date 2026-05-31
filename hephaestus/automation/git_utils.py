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
    env: dict[str, str] | None = None,
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
        env: Optional environment dict to pass to subprocess.run().
            If provided, replaces the current process environment.

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
        env=env,
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


_repo_info_cache: dict[Path | None, tuple[str, str]] = {}


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

    key = repo_root.resolve() if repo_root is not None else None
    cached = _repo_info_cache.get(key)
    if cached is not None:
        return cached

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
        _repo_info_cache[key] = (owner, repo)
        return owner, repo

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git remote URL: {e}") from e


# Keyed by the *resolved* repo_root path so a process that iterates multiple
# repositories (the automation loop, the myrmidon swarm) gets the right slug
# per repo instead of the first-cached one for all of them. The ``None`` key
# holds the result of the auto-detect branch.
_repo_slug_cache: dict[Path | None, str] = {}


def get_repo_slug(repo_root: Path | None = None) -> str:
    """Return the short repo name for log/status prefixes (e.g. ``AchaeanFleet``).

    Cached per ``repo_root`` for the lifetime of the process so hot log
    paths do not re-invoke ``git remote`` on every call. Falls back to
    ``"repo"`` if the remote URL cannot be parsed so callers can always
    interpolate the result into status strings without exception handling.

    Args:
        repo_root: Repository root (defaults to auto-detect)

    Returns:
        Short repository name (no owner prefix), or ``"repo"`` on failure.

    """
    key = repo_root.resolve() if repo_root is not None else None
    cached = _repo_slug_cache.get(key)
    if cached is not None:
        return cached
    try:
        _, repo = get_repo_info(repo_root)
    except (RuntimeError, subprocess.CalledProcessError):
        repo = "repo"
    _repo_slug_cache[key] = repo
    return repo


def clear_repo_caches() -> None:
    """Clear both repo info and slug caches. For test isolation and long-lived processes."""
    _repo_info_cache.clear()
    _repo_slug_cache.clear()


def issue_ref(issue_number: int | str) -> str:
    """Return a ``<repo>#<number>`` reference string for logs and status lines."""
    return f"{get_repo_slug()}#{issue_number}"


def pr_ref(pr_number: int | str) -> str:
    """Return a ``<repo>#<number>`` reference string for PRs (same format as issues)."""
    return f"{get_repo_slug()}#{pr_number}"


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


# When the remote branch has advanced (someone else — or a parallel ci_driver
# worker — pushed in the meantime), ``git push`` reports one of these two
# stderr fragments. We catch them to trigger a fetch + force-with-lease retry
# rather than abandoning the CI fix after a single attempt.
_PUSH_REJECTED_FRAGMENTS: tuple[str, ...] = (
    "non-fast-forward",
    "fetch first",
)


def _is_push_rejected_diverged(exc: subprocess.CalledProcessError) -> bool:
    """Return True iff ``git push`` failed because the remote branch diverged."""
    blob = (exc.stderr or "") + (exc.stdout or "")
    return any(fragment in blob for fragment in _PUSH_REJECTED_FRAGMENTS)


def push_current_branch_with_lease_on_divergence(
    cwd: Path,
    *,
    branch: str | None = None,
    remote: str = "origin",
) -> subprocess.CompletedProcess[str]:
    """Push ``HEAD`` to ``<remote>``; on divergence, fetch + force-with-lease retry.

    The first attempt is a plain ``git push <remote> HEAD``. If that fails with a
    non-fast-forward / fetch-first rejection — the exact symptom seen when a
    second CI-fix iteration runs before the bot's previous push has been mirrored
    locally, or when a human commits to the bot's branch — we then:

    1. ``git fetch <remote> <branch>`` to update the remote-tracking ref.
    2. ``git push --force-with-lease=<branch> <remote> HEAD:<branch>`` so the
       push refuses if a *new* commit landed between step 1 and now (the safety
       guarantee `--force-with-lease` provides over a bare `--force`).

    Any other error from the first push (auth failure, network, etc.) is
    re-raised unchanged. The second push's failure is also re-raised — callers
    log it and treat the issue as failed.

    Args:
        cwd: Worktree path to run the git commands in.
        branch: Branch name. If omitted, derived from ``git rev-parse
            --abbrev-ref HEAD`` in ``cwd``.
        remote: Remote name (default ``origin``).

    Returns:
        The successful push's ``CompletedProcess``.

    Raises:
        subprocess.CalledProcessError: If both the initial push and the
            lease-retry push fail. The exception is the *retry* failure if the
            initial push was a recognized divergence, otherwise the *initial*
            failure.

    """
    try:
        return run(
            [
                "git",
                "push",
                remote,
                "HEAD",
            ],
            cwd=cwd,
        )
    except subprocess.CalledProcessError as exc:
        if not _is_push_rejected_diverged(exc):
            raise
        # Resolve the branch name lazily — most callers know it, but we don't
        # want to require it on every caller.
        if branch is None:
            branch = get_current_branch(cwd)
        logger.warning(
            "git push to %s/%s rejected as diverged; fetching + force-with-lease retry",
            remote,
            branch,
        )
        # Fetch the canonical tip so the lease check has something current to
        # compare against. If this fetch fails, raise — we cannot safely
        # lease-push without an up-to-date remote-tracking ref.
        run(["git", "fetch", remote, branch], cwd=cwd)
        return run(
            [
                "git",
                "push",
                f"--force-with-lease={branch}",
                remote,
                f"HEAD:{branch}",
            ],
            cwd=cwd,
        )


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
