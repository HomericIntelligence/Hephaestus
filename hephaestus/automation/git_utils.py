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
from typing import Any

import hephaestus.automation.git_runtime as _git_runtime
from hephaestus.constants import agent_git_timeout
from hephaestus.utils.retry import retry_with_backoff

from .session_naming import issue_auto_impl_branch_name as _session_issue_auto_impl_branch_name

logger = logging.getLogger(__name__)

COMMIT_POLICY_REWRITE_EXEC = "git commit --amend --no-edit -S -s --allow-empty"

# Keep the historical patchable/public names while making their compatibility
# re-export role explicit to static analyzers and type checkers.
clear_repo_caches = _git_runtime.clear_repo_caches
get_repo_info = _git_runtime.get_repo_info
get_repo_root = _git_runtime.get_repo_root
get_repo_slug = _git_runtime.get_repo_slug
issue_ref = _git_runtime.issue_ref
pr_ref = _git_runtime.pr_ref
run = _git_runtime.run


class DetachedHeadPushError(RuntimeError):
    """Base error for a failed lease-protected detached-head publication."""


class DetachedHeadPushRemoteHeadChangedError(DetachedHeadPushError):
    """The remote branch changed after the reviewed-head proof was obtained."""


class DetachedHeadPushRemoteHeadUnchangedError(DetachedHeadPushError):
    """The detached push failed while the remote branch still matched its proof."""


class DetachedHeadPushRemoteProbeError(DetachedHeadPushError):
    """The remote branch could not be checked after a detached push failure."""


class DirectBranchReservationCollisionError(RuntimeError):
    """An absent-only direct branch reservation found an existing remote ref."""

    def __init__(self, branch_name: str) -> None:
        """Record the ref whose existence was confirmed by the remote probe."""
        self.branch_name = branch_name
        super().__init__(f"Direct-scope branch {branch_name} already exists")


def _timeout_kw(timeout: int | None) -> dict[str, Any]:
    """Return a ``run`` kwargs fragment only when a timeout was provided."""
    return {} if timeout is None else {"timeout": timeout}


def issue_auto_impl_branch_name(issue_number: int | str) -> str:
    """Return the canonical branch name for an issue implementation PR."""
    return _session_issue_auto_impl_branch_name(issue_number)


def commit_if_changes(
    issue_number: int,
    worktree_path: Path,
    agent: str = "claude",
    *,
    agent_model: str | None = None,
    committed_log_message: str = "Committed changes for issue #%s",
    allowed_paths: Collection[str] | None = None,
    timeout: int | None = None,
    git_message_timeout: int = 1200,
) -> bool:
    """Commit pending changes in *worktree_path* if the worktree is dirty.

    Args:
        issue_number: GitHub issue number used by the commit helper.
        worktree_path: Path to the git worktree to inspect.
        agent: Agent name forwarded to the commit helper.
        agent_model: Explicit model and reasoning effort forwarded to the
            commit-message helper.
        committed_log_message: ``logging`` format string for a successful commit.
        allowed_paths: Optional exact path allowlist forwarded to the commit
            helper. When set, only those porcelain paths may be staged.
        timeout: Optional timeout in seconds for local git commands.

    Returns:
        True if a commit was created, otherwise False.

    """
    result = run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        **_timeout_kw(timeout),
    )
    if not result.stdout.strip():
        logger.info("No changes to commit for issue #%s", issue_number)
        return False

    try:
        # Import on demand to keep the product-layer commit implementation out
        # of the neutral Git utility import path without hidden registration
        # state or an import-order dependency.
        from .pr_manager import commit_changes

        commit_kwargs: dict[str, Any] = {"allowed_paths": allowed_paths}
        if agent_model is not None:
            commit_kwargs["agent_model"] = agent_model
        if timeout is not None:
            commit_kwargs["git_timeout"] = timeout
        commit_kwargs["git_message_timeout"] = git_message_timeout
        commit_changes(
            issue_number,
            worktree_path,
            agent,
            **commit_kwargs,
        )
        logger.info(committed_log_message, issue_number)
        return True
    except RuntimeError as e:
        logger.warning("Commit skipped for issue #%s: %s", issue_number, e)
        return False


def push_branch(branch_name: str, worktree_path: Path, *, timeout: int | None = None) -> None:
    """Push *branch_name* to ``origin``.

    Args:
        branch_name: Branch name to push.
        worktree_path: Path to the git worktree.
        timeout: Optional timeout in seconds for the push.

    Raises:
        RuntimeError: If the push fails.

    """
    try:
        run(
            ["git", "push", "origin", branch_name],
            cwd=worktree_path,
            **_timeout_kw(timeout),
        )
        logger.info("Pushed branch %s to origin", branch_name)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to push branch {branch_name}: {e}") from e


def reserve_remote_branch_if_absent(
    branch_name: str,
    base_sha: str,
    repo_root: Path,
    *,
    timeout: int | None = None,
) -> None:
    """Atomically create ``origin/<branch_name>`` at ``base_sha`` only when absent.

    A direct CLI scope needs a server-side ownership proof before an agent can
    begin writing its deterministic implementation branch.  An empty
    ``--force-with-lease`` expectation makes the receive-pack reject a branch
    that appeared after the caller's local inspection.

    This metadata-only push is the sole automation push that uses
    ``--no-verify``.  It publishes an already-resolved base commit, not agent
    changes, so running a repository pre-push suite cannot validate anything
    new and can make branch claiming depend on ambient checkout artifacts.  The
    later implementation push does not use this exception and remains subject
    to every configured hook.  Git pre-commit hooks are unaffected because
    this function creates no commit.
    """
    try:
        run(
            [
                "git",
                "push",
                "--no-verify",
                f"--force-with-lease=refs/heads/{branch_name}:",
                "origin",
                f"{base_sha}:refs/heads/{branch_name}",
            ],
            cwd=repo_root,
            **_timeout_kw(timeout),
        )
    except subprocess.CalledProcessError as exc:
        if _remote_branch_exists(branch_name, repo_root, timeout=timeout):
            raise DirectBranchReservationCollisionError(branch_name) from exc
        raise RuntimeError(f"Failed to reserve direct-scope branch {branch_name}: {exc}") from exc


def _remote_branch_exists(
    branch_name: str,
    repo_root: Path,
    *,
    timeout: int | None,
) -> bool:
    """Return whether a remote probe conclusively found ``branch_name``.

    A rejected absent-only lease can mean a competing branch creator, but it
    can also mean authentication, transport, or server trouble.  Only a
    successful post-failure ``ls-remote`` result for the exact ref proves the
    former; every other outcome remains a retriable infrastructure failure.
    """
    expected_ref = f"refs/heads/{branch_name}"
    try:
        probe = run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", expected_ref],
            cwd=repo_root,
            check=False,
            log_errors=False,
            **_timeout_kw(timeout),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    return any(
        line.partition("\t")[2] == expected_ref for line in str(probe.stdout or "").splitlines()
    )


def push_branch_if_remote_matches(
    branch_name: str,
    expected_remote_sha: str,
    worktree_path: Path,
    *,
    timeout: int | None = None,
) -> None:
    """Push only an expected fast-forward of a direct-scope reservation.

    The ancestry check prevents a locally rewritten branch from becoming a
    forced update.  The explicit server-side lease then rejects any human or
    competing writer that changed the reservation after admission.
    """
    ancestry = run(
        ["git", "merge-base", "--is-ancestor", expected_remote_sha, "HEAD"],
        cwd=worktree_path,
        check=False,
        **_timeout_kw(timeout),
    )
    if ancestry.returncode != 0:
        raise RuntimeError(
            f"Direct-scope branch {branch_name} is not a fast-forward of its reservation"
        )
    try:
        run(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/{branch_name}:{expected_remote_sha}",
                "origin",
                f"HEAD:refs/heads/{branch_name}",
            ],
            cwd=worktree_path,
            **_timeout_kw(timeout),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to publish direct-scope branch {branch_name}: {exc}") from exc


def delete_reserved_branch_if_unchanged(
    branch_name: str,
    expected_remote_sha: str,
    repo_root: Path,
    *,
    timeout: int | None = None,
) -> bool:
    """Delete an unused direct-scope reservation only while it remains ours.

    A stale lease is expected when an external writer took ownership after the
    reservation.  It is reported as ``False`` only after a follow-up ref read
    confirms that the remote no longer points to our expected SHA.  Transport
    and authentication failures raise so callers can retry rather than
    mistaking an unreachable reservation for a safe ownership loss.
    """
    try:
        run(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/{branch_name}:{expected_remote_sha}",
                "origin",
                f":refs/heads/{branch_name}",
            ],
            cwd=repo_root,
            **_timeout_kw(timeout),
        )
        return True
    except subprocess.CalledProcessError as exc:
        try:
            observed = run(
                ["git", "ls-remote", "--refs", "origin", f"refs/heads/{branch_name}"],
                cwd=repo_root,
                **_timeout_kw(timeout),
            ).stdout.split()
        except subprocess.CalledProcessError as probe_exc:
            raise RuntimeError(
                f"Failed to release direct-scope branch {branch_name}; "
                "could not verify the remote reservation"
            ) from probe_exc
        if not observed or observed[0] != expected_remote_sha:
            logger.warning(
                "Direct-scope branch %s changed before its reservation could be released",
                branch_name,
            )
            return False
        raise RuntimeError(
            f"Failed to release direct-scope branch {branch_name}; "
            "remote reservation remains unchanged"
        ) from exc


def delete_local_branch_if_unchanged(
    branch_name: str,
    expected_sha: str,
    repo_root: Path,
    *,
    timeout: int | None = None,
) -> bool:
    """Delete a local no-op branch only if it is still at ``expected_sha``.

    The caller holds the shared worktree-metadata lock. It first verifies the
    expected ref value, then uses ``git branch -d`` rather than plumbing ref
    deletion: Git refuses to delete a branch attached to any worktree. A
    changed, absent, or checked-out branch is a safe ``False`` result.
    """
    ref = f"refs/heads/{branch_name}"
    observed = run(
        ["git", "show-ref", "--verify", "--hash", ref],
        cwd=repo_root,
        check=False,
        **_timeout_kw(timeout),
    )
    if observed.returncode == 1 or observed.stdout.strip() != expected_sha:
        return False
    try:
        run(["git", "branch", "-d", branch_name], cwd=repo_root, **_timeout_kw(timeout))
        return True
    except subprocess.CalledProcessError as exc:
        detail = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
        if "checked out" in detail or "not fully merged" in detail:
            return False
        raise RuntimeError(
            f"Failed to release local direct-scope branch {branch_name}; ref remains unchanged"
        ) from exc


def push_head_to_branch(
    branch_name: str,
    expected_remote_sha: str,
    worktree_path: Path,
    *,
    source_sha: str | None = None,
    timeout: int | None = None,
) -> None:
    """Publish detached ``HEAD`` to ``origin/<branch_name>`` safely.

    Direct PR review uses a detached, isolated worktree so it can never reset
    or remove a writer checkout. Addressing commits therefore live on its
    detached ``HEAD`` rather than on the local branch ref. The explicit lease
    permits an address agent to rebase onto current main while refusing to
    overwrite a PR head that changed after its reviewed-head proof.
    """
    source_ref = source_sha or "HEAD"
    try:
        run(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/{branch_name}:{expected_remote_sha}",
                "origin",
                f"{source_ref}:refs/heads/{branch_name}",
            ],
            cwd=worktree_path,
            **_timeout_kw(timeout),
        )
        logger.info("Published detached HEAD to origin/%s", branch_name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # The rejected push can be a local pre-push-hook failure, transport
        # failure, or a server-side lease rejection.  Never infer which from
        # git's stderr: hook output is untrusted diagnostic content and may
        # contain repository data.  A fresh authoritative ref read classifies
        # only the safe ownership distinction needed by the pipeline.
        try:
            observed = run(
                ["git", "ls-remote", "--refs", "origin", f"refs/heads/{branch_name}"],
                cwd=worktree_path,
                **_timeout_kw(timeout),
            ).stdout.split()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as probe_exc:
            raise DetachedHeadPushRemoteProbeError(
                "Detached review push failed and the remote head could not be verified"
            ) from probe_exc
        if source_sha is not None and observed and observed[0] == source_sha:
            # A transport error can arrive after receive-pack accepted this
            # exact immutable source commit. The intended remote state is
            # already present, so treating it as drift would needlessly start
            # a second review and preserve a checkout that was published.
            logger.info("Detached review commit was published before the push result was lost")
            return
        if not observed or observed[0] != expected_remote_sha:
            raise DetachedHeadPushRemoteHeadChangedError(
                "Detached review push observed a different remote head"
            ) from exc
        raise DetachedHeadPushRemoteHeadUnchangedError(
            "Detached review push failed while the remote head remained unchanged"
        ) from exc


def has_unpushed_commits(
    branch_name: str, worktree_path: Path, *, timeout: int | None = None
) -> bool:
    """Return whether ``HEAD`` is ahead of ``origin/<branch_name>``.

    This is used only by the coordinator-owned commit/push handoff to recover
    an address agent that committed locally despite its prompt.  It performs
    no network operation; worktree synchronization fetched the tracking ref
    before the agent ran.
    """
    result = run(
        ["git", "rev-list", "--count", f"origin/{branch_name}..HEAD"],
        cwd=worktree_path,
        capture_output=True,
        **_timeout_kw(timeout),
    )
    try:
        return int((result.stdout or "0").strip()) > 0
    except ValueError as exc:
        raise RuntimeError(f"Could not count unpushed commits for {branch_name}") from exc


def safe_git_fetch(repo_root: Path, retries: int = 3, *, timeout_s: int | None = None) -> bool:
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
            timeout=timeout_s if timeout_s is not None else agent_git_timeout(),
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


def get_current_branch(repo_root: Path | None = None, *, timeout: int | None = None) -> str:
    """Get the current git branch name.

    Args:
        repo_root: Repository root (defaults to auto-detect)
        timeout: Optional timeout in seconds for the git command.

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
            **_timeout_kw(timeout),
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
    timeout: int | None = None,
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
        timeout: Optional timeout in seconds for each git command.

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
            **_timeout_kw(timeout),
        )
    except subprocess.CalledProcessError as exc:
        if not _is_push_rejected_diverged(exc):
            raise
        # Resolve the branch name lazily — most callers know it, but we don't
        # want to require it on every caller.
        if branch is None:
            branch = get_current_branch(cwd, timeout=timeout)
        logger.warning(
            "git push to %s/%s rejected as diverged; fetching + force-with-lease retry",
            remote,
            branch,
        )
        # Fetch the canonical tip so the lease check has something current to
        # compare against. If this fetch fails, raise — we cannot safely
        # lease-push without an up-to-date remote-tracking ref.
        run(["git", "fetch", remote, branch], cwd=cwd, **_timeout_kw(timeout))
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
            **_timeout_kw(timeout),
        )


def sync_worktree_to_remote_branch(
    cwd: Path,
    branch: str,
    *,
    remote: str = "origin",
    pr_number: int | None = None,
    timeout: int | None = None,
) -> None:
    """Reset ``cwd`` to ``<remote>/<branch>`` so the agent starts from the PR head.

    The worktree may have been created from a stale local branch (e.g.
    ``WorktreeManager`` reused an existing local ref that pointed at the repo's
    old ``main`` tip from a previous run, never noticing that ``origin/<branch>``
    has advanced). Before any agent runs in this worktree, we want HEAD to
    match the PR's actual head on the remote so the agent's commit is built on
    top of the real PR history.

    This runs in two steps in ``cwd``:

    1. ``git fetch <remote> +refs/heads/<branch>:refs/remotes/<remote>/<branch>``
       — materializes the remote-tracking ref so the reset has a current
       target. If that branch ref is unavailable and ``pr_number`` is supplied,
       fetch GitHub's ``refs/pull/N/head`` instead.
    2. ``git reset --hard <remote>/<branch>`` (or ``FETCH_HEAD`` for the pull
       ref fallback) moves HEAD to the PR's actual head.

    The worktree is throwaway (the driver removes it after each issue), so
    ``reset --hard`` is safe here: there is no human work to preserve.

    Args:
        cwd: Worktree path.
        branch: Remote branch name (the PR's head).
        remote: Remote name (default ``origin``).
        pr_number: Optional PR number used to fall back to GitHub's pull ref
            when the head branch is not available on ``remote``.
        timeout: Optional timeout in seconds for each git command.

    Raises:
        subprocess.CalledProcessError: If either git command fails. Callers
            should treat this as a hard error — without a synced HEAD the
            subsequent CI-fix push would land on the wrong base.

    """
    logger.info("Syncing worktree at %s to %s/%s before agent run", cwd, remote, branch)
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    try:
        run(
            ["git", "fetch", remote, f"+refs/heads/{branch}:{tracking_ref}"],
            cwd=cwd,
            **_timeout_kw(timeout),
        )
    except subprocess.CalledProcessError as error:
        if pr_number is None or not _is_missing_remote_ref_error(error):
            raise
        pull_ref = f"refs/pull/{pr_number}/head"
        logger.info(
            "Branch %s is unavailable on %s; syncing worktree at %s from %s",
            branch,
            remote,
            cwd,
            pull_ref,
        )
        run(["git", "fetch", remote, pull_ref], cwd=cwd, **_timeout_kw(timeout))
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=cwd, **_timeout_kw(timeout))
        return
    run(["git", "reset", "--hard", f"{remote}/{branch}"], cwd=cwd, **_timeout_kw(timeout))


def _is_missing_remote_ref_error(error: subprocess.CalledProcessError) -> bool:
    """Return whether a fetch failed because the requested branch ref is absent."""
    diagnostics = "\n".join(
        str(value) for value in (error.stdout, error.stderr) if value is not None
    ).lower()
    return any(
        marker in diagnostics
        for marker in (
            "couldn't find remote ref",
            "could not find remote ref",
            "remote ref does not exist",
        )
    )


def _remove_untracked_files_tracked_by_ref(
    cwd: Path,
    ref: str,
    *,
    timeout: int | None = None,
) -> list[Path]:
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
            **_timeout_kw(timeout),
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
            run(
                ["git", "cat-file", "-e", f"{ref}:{rel}"],
                cwd=cwd,
                log_errors=False,
                **_timeout_kw(timeout),
            )
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
        "--empty=drop",
        base_ref,
        "--exec",
        COMMIT_POLICY_REWRITE_EXEC,
    ]


def ensure_branch_commit_metadata(
    cwd: Path,
    base_branch: str = "main",
    *,
    remote: str = "origin",
    timeout: int | None = None,
) -> None:
    """Rewrite branch commits so each carries a verified signature and DCO trailer."""
    base_ref = f"{remote}/{base_branch}"
    run(["git", "fetch", remote, base_branch], cwd=cwd, **_timeout_kw(timeout))
    _remove_untracked_files_tracked_by_ref(cwd, base_ref, timeout=timeout)
    try:
        run(_commit_policy_rebase_command(base_ref), cwd=cwd, **_timeout_kw(timeout))
    except subprocess.CalledProcessError:
        # Leave the worktree ready for the caller's next automation attempt.
        # ``check=False`` preserves the original rebase failure signal.
        run(["git", "rebase", "--abort"], cwd=cwd, check=False, **_timeout_kw(timeout))
        raise


def rebase_worktree_onto(
    cwd: Path,
    base_branch: str = "main",
    *,
    remote: str = "origin",
    preserve_conflicts: bool = False,
    timeout: int | None = None,
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
       conflict, the default ``git rebase --abort`` restores the pre-rebase
       HEAD. ``preserve_conflicts=True`` instead leaves the host-owned rebase
       paused so an edit-only agent can change file contents before the host
       validates and continues it.

    The caller is expected to push the rebased HEAD with
    :func:`push_current_branch_with_lease_on_divergence` (the rebase rewrites
    history, so a lease push is required).

    Args:
        cwd: Worktree path (already synced to the PR head).
        base_branch: Branch to rebase onto (default ``main``).
        remote: Remote name (default ``origin``).
        preserve_conflicts: Leave a conflicted rebase paused for a later
            host-owned continuation instead of aborting it.
        timeout: Optional timeout in seconds for each git command.

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
    run(["git", "fetch", remote, base_branch], cwd=cwd, **_timeout_kw(timeout))
    _remove_untracked_files_tracked_by_ref(cwd, base_ref, timeout=timeout)
    try:
        run(_commit_policy_rebase_command(base_ref), cwd=cwd, **_timeout_kw(timeout))
        logger.info("Rebased worktree at %s onto %s/%s cleanly", cwd, remote, base_branch)
        return True
    except subprocess.CalledProcessError:
        if not preserve_conflicts:
            # ``check=False`` because an abort error must not mask the original
            # conflict signal.
            run(["git", "rebase", "--abort"], cwd=cwd, check=False, **_timeout_kw(timeout))
        logger.info(
            "Rebase of worktree at %s onto %s/%s hit conflicts; %s",
            cwd,
            remote,
            base_branch,
            "preserved for host-owned resolution" if preserve_conflicts else "aborted",
        )
        return False


def is_clean_working_tree(repo_root: Path | None = None, *, timeout: int | None = None) -> bool:
    """Check if the working tree is clean (no uncommitted changes).

    Args:
        repo_root: Repository root (defaults to auto-detect)
        timeout: Optional timeout in seconds for the git status probe.

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
            **_timeout_kw(timeout),
        )
        return len(result.stdout.strip()) == 0
    except subprocess.CalledProcessError:
        return False
