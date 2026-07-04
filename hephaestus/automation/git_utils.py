"""Git utility functions for repository operations.

Provides helpers for:
- Repository root discovery
- Repository owner/name detection
- Safe git operations with error handling
- Git lock cleanup
"""

import logging
import subprocess
from collections.abc import Collection
from pathlib import Path

from hephaestus.constants import agent_git_timeout

# ``get_repo_root`` is re-exported (not redefined) so that the single canonical
# implementation in ``hephaestus.utils.helpers`` is used everywhere, while
# ``hephaestus.automation.git_utils.get_repo_root`` remains a stable, patchable
# import path for the automation package and its tests. The ``X as X`` form
# marks it an explicit re-export so mypy does not flag ``attr-defined`` at the
# 13 import sites under --no-implicit-reexport.
from hephaestus.utils.cache import ThreadSafeCache
from hephaestus.utils.helpers import get_repo_root as get_repo_root, run_subprocess
from hephaestus.utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

COMMIT_POLICY_REWRITE_EXEC = "git commit --amend --no-edit -S -s"


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


_repo_info_cache: ThreadSafeCache[Path | None, tuple[str, str]] = ThreadSafeCache()


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

    def _compute() -> tuple[str, str]:
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

    return _repo_info_cache.get_or_compute(key, _compute)


# Keyed by the *resolved* repo_root path so a process that iterates multiple
# repositories (the automation loop, the myrmidon swarm) gets the right slug
# per repo instead of the first-cached one for all of them. The ``None`` key
# holds the result of the auto-detect branch.
_repo_slug_cache: ThreadSafeCache[Path | None, str] = ThreadSafeCache()


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

    def _compute() -> str:
        try:
            _, repo = get_repo_info(repo_root)
        except (RuntimeError, subprocess.CalledProcessError):
            return "repo"
        return repo

    return _repo_slug_cache.get_or_compute(key, _compute)


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


def commit_if_changes(
    issue_number: int,
    worktree_path: Path,
    agent: str = "claude",
    *,
    committed_log_message: str = "Committed changes for issue #%s",
    allowed_paths: Collection[str] | None = None,
) -> bool:
    """Commit pending changes in *worktree_path* if the worktree is dirty.

    Args:
        issue_number: GitHub issue number used by the commit helper.
        worktree_path: Path to the git worktree to inspect.
        agent: Agent name forwarded to the commit helper.
        committed_log_message: ``logging`` format string for a successful commit.
        allowed_paths: Optional exact path allowlist forwarded to the commit
            helper. When set, only those porcelain paths may be staged.

    Returns:
        True if a commit was created, otherwise False.

    """
    result = run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
    )
    if not result.stdout.strip():
        logger.info("No changes to commit for issue #%s", issue_number)
        return False

    try:
        from .pr_manager import commit_changes

        commit_changes(issue_number, worktree_path, agent, allowed_paths=allowed_paths)
        logger.info(committed_log_message, issue_number)
        return True
    except RuntimeError as e:
        logger.warning("Commit skipped for issue #%s: %s", issue_number, e)
        return False


def push_branch(branch_name: str, worktree_path: Path) -> None:
    """Push *branch_name* to ``origin``.

    Args:
        branch_name: Branch name to push.
        worktree_path: Path to the git worktree.

    Raises:
        RuntimeError: If the push fails.

    """
    try:
        run(["git", "push", "origin", branch_name], cwd=worktree_path)
        logger.info("Pushed branch %s to origin", branch_name)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e


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
            timeout=agent_git_timeout(),
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
    push_ref: str = "HEAD",
) -> subprocess.CompletedProcess[str]:
    """Push ``HEAD`` to ``<remote>``; on divergence, fetch + force-with-lease retry.

    The first attempt is ``git push <remote> <push_ref>``. If that fails with a
    non-fast-forward / fetch-first rejection — the exact symptom seen when a
    second CI-fix iteration runs before the bot's previous push has been mirrored
    locally, or when a human commits to the bot's branch — we then:

    1. ``git fetch <remote> <branch>`` to update the remote-tracking ref.
    2. ``git push --force-with-lease=<branch> <remote> <push_ref_or_default>`` so
       the push refuses if a *new* commit landed between step 1 and now (the
       safety guarantee ``--force-with-lease`` provides over a bare ``--force``).

    Any other error from the first push (auth failure, network, etc.) is
    re-raised unchanged. The second push's failure is also re-raised — callers
    log it and treat the issue as failed.

    Args:
        cwd: Worktree path to run the git commands in.
        branch: Branch name on the remote. If omitted, derived from ``git
            rev-parse --abbrev-ref HEAD`` in ``cwd``.
        remote: Remote name (default ``origin``).
        push_ref: Refspec to push. Defaults to ``"HEAD"`` (push the current
            branch to whatever the remote tracks). When the local HEAD may
            have been moved off the target branch by an agent (#832), callers
            should pass an explicit refspec like ``f"HEAD:{branch}"`` to force
            the push to land on the named remote branch regardless of local
            branch state.

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
                push_ref,
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
        # The lease retry preserves any explicit ``push_ref`` the caller passed
        # so HEAD lands on the right *remote* branch even if the local HEAD has
        # drifted (#832). The default ``"HEAD"`` is rewritten to
        # ``HEAD:<branch>`` so the lease push and the initial push behave
        # consistently when no explicit refspec is given.
        lease_push_ref = push_ref if push_ref != "HEAD" else f"HEAD:{branch}"
        return run(
            [
                "git",
                "push",
                f"--force-with-lease={branch}",
                remote,
                lease_push_ref,
            ],
            cwd=cwd,
        )


def sync_worktree_to_remote_branch(
    cwd: Path,
    branch: str,
    *,
    remote: str = "origin",
) -> None:
    """Reset ``cwd`` to ``<remote>/<branch>`` so the agent starts from the PR head.

    The worktree may have been created from a stale local branch (e.g.
    ``WorktreeManager`` reused an existing local ref that pointed at the repo's
    old ``main`` tip from a previous run, never noticing that ``origin/<branch>``
    has advanced). Before any agent runs in this worktree, we want HEAD to
    match the PR's actual head on the remote so the agent's commit is built on
    top of the real PR history.

    This runs in two steps in ``cwd``:

    1. ``git fetch <remote> <branch>`` — updates the remote-tracking ref so the
       reset has a current target.
    2. ``git reset --hard <remote>/<branch>`` — moves HEAD to the PR's actual
       head, discarding any divergent local commits or working-tree edits.

    The worktree is throwaway (the driver removes it after each issue), so
    ``reset --hard`` is safe here: there is no human work to preserve.

    Args:
        cwd: Worktree path.
        branch: Remote branch name (the PR's head).
        remote: Remote name (default ``origin``).

    Raises:
        subprocess.CalledProcessError: If either git command fails. Callers
            should treat this as a hard error — without a synced HEAD the
            subsequent CI-fix push would land on the wrong base.

    """
    logger.info("Syncing worktree at %s to %s/%s before agent run", cwd, remote, branch)
    run(["git", "fetch", remote, branch], cwd=cwd)
    run(["git", "reset", "--hard", f"{remote}/{branch}"], cwd=cwd)


def _remove_untracked_files_tracked_by_ref(cwd: Path, ref: str) -> list[Path]:
    """Remove untracked worktree files whose paths are tracked by ``ref``.

    ``git reset --hard`` intentionally leaves untracked files behind. In reused
    automation worktrees, stale files from a previous failed agent turn can then
    block ``git rebase`` with "untracked working tree files would be overwritten"
    when the base branch has since added those same paths. Deleting only
    untracked files that already exist in the target ref preserves unrelated
    scratch files while unblocking the deterministic rebase path.
    """
    try:
        result = run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            log_errors=False,
        )
    except subprocess.CalledProcessError:
        return []

    cwd_resolved = cwd.resolve()
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    removed: list[Path] = []
    for rel in (part for part in stdout.split("\0") if part):
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            logger.warning("Skipping unsafe untracked path before rebase: %s", rel)
            continue
        try:
            run(["git", "cat-file", "-e", f"{ref}:{rel}"], cwd=cwd, log_errors=False)
        except subprocess.CalledProcessError:
            continue

        target = (cwd / rel_path).resolve()
        try:
            target.relative_to(cwd_resolved)
        except ValueError:
            logger.warning("Skipping untracked path outside worktree before rebase: %s", rel)
            continue
        if not (target.is_file() or target.is_symlink()):
            continue
        target.unlink()
        removed.append(rel_path)

    if removed:
        logger.info(
            "Removed %s stale untracked file(s) tracked by %s before rebase: %s",
            len(removed),
            ref,
            ", ".join(str(path) for path in removed),
        )
    return removed


def _commit_policy_rebase_command(base_ref: str) -> list[str]:
    """Return a rebase command that repairs signature and DCO metadata per commit."""
    return [
        "git",
        "rebase",
        "--force-rebase",
        base_ref,
        "--exec",
        COMMIT_POLICY_REWRITE_EXEC,
    ]


def ensure_branch_commit_metadata(
    cwd: Path,
    base_branch: str = "main",
    *,
    remote: str = "origin",
) -> None:
    """Rewrite branch commits so each carries a verified signature and DCO trailer."""
    base_ref = f"{remote}/{base_branch}"
    run(["git", "fetch", remote, base_branch], cwd=cwd)
    _remove_untracked_files_tracked_by_ref(cwd, base_ref)
    try:
        run(_commit_policy_rebase_command(base_ref), cwd=cwd)
    except subprocess.CalledProcessError:
        # Leave the worktree ready for the caller's next automation attempt.
        # ``check=False`` preserves the original rebase failure signal.
        run(["git", "rebase", "--abort"], cwd=cwd, check=False)
        raise


def rebase_worktree_onto(
    cwd: Path,
    base_branch: str = "main",
    *,
    remote: str = "origin",
) -> bool:
    """Mechanically rebase the worktree at ``cwd`` onto ``<remote>/<base_branch>``.

    This is the cheap, deterministic path for PRs that are merely *behind* the
    base branch (or have textually non-overlapping changes): a policy-aware
    ``git rebase --force-rebase --exec`` resolves them with no agent involvement while
    re-signing each replayed commit and adding a DCO sign-off. Only when the
    rebase hits a real conflict do we hand off to the CI-fix agent.

    Two steps in ``cwd``:

    1. ``git fetch <remote> <base_branch>`` — refresh the remote-tracking ref so
       the rebase target is current.
    2. ``git rebase --force-rebase <remote>/<base_branch> --exec ...`` —
       replay the PR's commits on top of the latest base and run
       ``git commit --amend --no-edit -S -s`` after each replayed commit. On
       conflict, ``git rebase --abort`` restores the pre-rebase HEAD so the
       worktree is left clean for the agent path.

    The caller is expected to push the rebased HEAD with
    :func:`push_current_branch_with_lease_on_divergence` (the rebase rewrites
    history, so a lease push is required).

    Args:
        cwd: Worktree path (already synced to the PR head).
        base_branch: Branch to rebase onto (default ``main``).
        remote: Remote name (default ``origin``).

    Returns:
        ``True`` if the rebase applied cleanly. ``False`` if the rebase hit
        conflicts and was aborted, signalling the caller to fall back to the
        agent.

    Raises:
        subprocess.CalledProcessError: If the ``git fetch`` fails. A fetch
            failure is a hard error (no current base to rebase onto); the conflict
            case is handled internally and returns ``False`` rather than raising.

    """
    base_ref = f"{remote}/{base_branch}"
    run(["git", "fetch", remote, base_branch], cwd=cwd)
    _remove_untracked_files_tracked_by_ref(cwd, base_ref)
    try:
        run(_commit_policy_rebase_command(base_ref), cwd=cwd)
        logger.info("Rebased worktree at %s onto %s/%s cleanly", cwd, remote, base_branch)
        return True
    except subprocess.CalledProcessError:
        # Conflicts — abort so the worktree is restored to the PR head, then let
        # the caller hand the real conflict to the agent. ``check=False`` because
        # an abort that itself errors must not mask the conflict signal.
        run(["git", "rebase", "--abort"], cwd=cwd, check=False)
        logger.info(
            "Rebase of worktree at %s onto %s/%s hit conflicts; aborted",
            cwd,
            remote,
            base_branch,
        )
        return False


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
