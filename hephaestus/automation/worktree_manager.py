"""Git worktree management for parallel issue implementation.

Provides:
- Thread-safe worktree creation and cleanup
- Branch management
- Worktree state tracking

Layout note
-----------
Implementation worktrees live under ``<repo_root>/build/.worktrees/issue-{N}``.
Read-only strict-review checkouts use the separate
``<repo_root>/build/.worktrees/strict-review-pr-{N}`` path. Putting them inside
the repo (rather than ``~/.tmp``) keeps them visible to the implementer process
even after a force-kill: an interrupted run leaves the worktree on disk so a
subsequent invocation can either resume work or surface a ``WorktreeDirtyError``
to the operator. The trade-off is that ``git status`` in the parent repo can
show ``build/.worktrees/`` as untracked if it isn't ignored; ensure
``build/.worktrees/`` is in ``.gitignore`` (or a global ignore) for any repo
that runs the automation. Recovery procedure for a force-killed loop:

    git -C <repo> worktree list
    # Inspect each build/.worktrees/issue-N for unexpected modifications
    git -C <repo> worktree remove build/.worktrees/issue-N    # if abandoning
    rm -rf <repo>/build/.worktrees/issue-N                    # last resort
"""

import contextlib
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from hephaestus.utils.file_lock import file_lock

from .git_utils import get_repo_root, is_clean_working_tree, rebase_worktree_onto, run

logger = logging.getLogger(__name__)

_AUTOMATION_PROMPT_PREFIXES = (
    ".claude-pr-review-",
    ".claude-address-review-",
    ".claude-prompt-",
    ".claude-followup-",
)


def _timeout_kw(timeout: int | None) -> dict[str, Any]:
    """Return a ``run`` kwargs fragment only when a timeout was provided."""
    return {} if timeout is None else {"timeout": timeout}


def _loop_trunk_githash() -> str | None:
    """Return the loop-provided trunk commit-ish when available."""
    trunk = os.environ.get("HEPH_TRUNK_GITHASH", "").strip()
    if not trunk or trunk == "unknown":
        return None
    return trunk


def _looks_like_sha(ref: str) -> bool:
    """Return True when ``ref`` looks like a raw hex commit-ish."""
    return len(ref) >= 7 and all(ch in "0123456789abcdefABCDEF" for ch in ref)


class WorktreeDirtyError(Exception):
    """Raised when a worktree cannot be removed because it contains uncommitted changes."""

    def __init__(self, issue_number: int | str, path: Path) -> None:
        """Initialize with the affected issue number and worktree path."""
        self.issue_number = issue_number
        self.path = path
        super().__init__(f"Worktree for issue #{issue_number} at {path} has uncommitted changes")


class WorktreeManager:
    """Thread-safe manager for git worktrees.

    Allows parallel issue implementation in isolated worktrees.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        base_branch: str | None = None,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize worktree manager.

        Args:
            base_dir: Base directory for worktrees (default: repo_root/build/.worktrees)
            base_branch: Base branch for worktrees (default: auto-detect from origin/HEAD
                lazily on first use)
            repo_root: Repository checkout to operate in (default: ambient CWD's
                checkout via ``get_repo_root()``). Pass explicitly whenever the
                caller may be handling a repo other than its own CWD (e.g. a
                multi-repo pipeline worker) — every git subprocess this class
                shells out to (``worktree add``, ``fetch``, ``prune``, ...) runs
                with this as ``cwd``.

        """
        self.repo_root = repo_root if repo_root is not None else get_repo_root()
        if base_dir is None:
            base_dir = self.repo_root / "build" / ".worktrees"
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Base-branch detection is deferred to first use so that constructing
        # a WorktreeManager in test fixtures or environments without origin/*
        # refs does not raise. The automation loop passes HEPH_TRUNK_GITHASH as
        # a session-stable fallback, but issue-major workers may still request a
        # fresh remote base via refresh_base=True before cutting a new branch.
        # An explicit constructor base remains pinned and is never moved.
        loop_trunk = _loop_trunk_githash()
        self._base_branch_override = base_branch or loop_trunk
        self._base_branch_override_source = (
            "explicit" if base_branch is not None else "loop_trunk" if loop_trunk else None
        )
        self._base_branch_resolved: str | None = None
        self.worktrees: dict[int | str, Path] = {}
        self.preserved: list[tuple[int | str, Path]] = []
        self.lock = threading.Lock()

        logger.debug("Initialized WorktreeManager at %s", self.base_dir)

    def _git_metadata_lock_path(self) -> Path:
        """Return the cross-process lock guarding shared git worktree metadata."""
        return self.base_dir / ".git-metadata.lock"

    @property
    def base_branch(self) -> str:
        """The base branch, auto-detected on first access."""
        return self._resolve_base_branch()

    def _resolve_base_branch(self, *, timeout: int | None = None) -> str:
        """Return the base branch, using ``timeout`` for lazy git detection."""
        if self._base_branch_resolved is not None:
            return self._base_branch_resolved
        if self._base_branch_override is not None:
            self._base_branch_resolved = self._base_branch_override
            return self._base_branch_resolved
        self._base_branch_resolved = self._detect_base_branch(timeout=timeout)
        return self._base_branch_resolved

    def refresh_base_branch(self, *, timeout: int | None = None) -> str:
        """Re-fetch ``origin`` and re-resolve the auto-detected base branch.

        Issue-major loop (#1560): each issue's worktree should branch off the
        FRESHLY-merged trunk, not a snapshot pinned once per loop. Calling this
        before creating a worktree fetches the latest ``origin`` and clears the
        cached ``_base_branch_resolved`` so the next ``base_branch`` access
        re-detects ``origin/HEAD``.

        A no-op only when the base is explicitly pinned with ``base_branch=``.
        ``HEPH_TRUNK_GITHASH`` is a loop-start fallback, not an issue-major
        freshness pin, so refresh_base=True deliberately drops it and re-detects
        the current remote base. Returns the effective base branch.
        """
        if self._base_branch_override_source == "explicit":
            return self.base_branch
        if self._base_branch_override_source == "loop_trunk":
            self._base_branch_override = None
            self._base_branch_override_source = None
        with file_lock(self._git_metadata_lock_path()):
            with contextlib.suppress(Exception):
                run(
                    ["git", "fetch", "origin"],
                    cwd=self.repo_root,
                    capture_output=True,
                    **_timeout_kw(timeout),
                )
            self._base_branch_resolved = None  # force re-detect on next access
            return self._resolve_base_branch(timeout=timeout)

    def _detect_base_branch(self, *, timeout: int | None = None) -> str:
        try:
            result = run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                cwd=self.repo_root,
                capture_output=True,
                log_errors=False,
                **_timeout_kw(timeout),
            )
            detected = result.stdout.strip()
            logger.debug("Auto-detected base branch: %s", detected)
            return detected
        except Exception:
            for candidate in ("origin/main", "origin/master"):
                try:
                    run(
                        ["git", "rev-parse", "--verify", candidate],
                        cwd=self.repo_root,
                        capture_output=True,
                        **_timeout_kw(timeout),
                    )
                    logger.warning("Could not auto-detect base branch, found %s", candidate)
                    return candidate
                except Exception:
                    continue
            raise RuntimeError(
                "Could not auto-detect the remote base branch. "
                "Neither 'origin/main' nor 'origin/master' exists. "
                "Run 'git remote set-head origin --auto' or pass "
                "base_branch= explicitly to WorktreeManager()."
            ) from None

    def create_worktree(
        self,
        issue_number: int,
        branch_name: str | None = None,
        *,
        refresh_base: bool = False,
        isolated: bool = False,
        isolated_name: str | None = None,
        timeout: int | None = None,
    ) -> Path:
        """Create a new worktree for an issue.

        Args:
            issue_number: Issue number
            branch_name: Branch name (default: {issue_number}-auto)
            refresh_base: When True, re-fetch origin and re-resolve the
                auto-detected base branch first, so the new branch is cut from
                the freshly-merged trunk. Used by the issue-major loop (#1560)
                so each issue branches off the latest trunk; no-op when the base
                is explicitly pinned. Default False preserves prior behavior for
                all other callers (review, address-review, ci-driver).
            isolated: When True, create an in-root detached checkout instead of
                reusing another worktree that holds ``branch_name``. Strict
                and PR review use this so a preserved writer checkout is never
                reset or exposed to an agent.
            isolated_name: Stable name for an isolated checkout.  Callers that
                need independent detached checkouts for the same PR (for
                example mutable PR review and read-only strict review) must
                supply distinct names. Ignored unless ``isolated`` is true.
            timeout: Optional timeout in seconds for each git command.

        Returns:
            Path to worktree directory

        Raises:
            RuntimeError: If worktree creation fails

        """
        with self.lock:
            isolated_key = isolated_name or f"strict-review-pr-{issue_number}"
            worktree_key: int | str = isolated_key if isolated else issue_number
            if worktree_key in self.worktrees:
                logger.warning("Worktree for issue #%s already exists", issue_number)
                return self.worktrees[worktree_key]

            if refresh_base:
                self.refresh_base_branch(timeout=timeout)

            if branch_name is None:
                branch_name = f"{issue_number}-auto"

            worktree_path = self.base_dir / (isolated_key if isolated else f"issue-{issue_number}")

            # Reuse, don't collide: git forbids the same branch in two worktrees.
            # When ``branch_name`` is already checked out elsewhere (e.g. a PR
            # resolved to its real head branch ``708-auto-impl`` which the
            # issue-708 worktree already holds), ``git worktree add`` would fail
            # with "already used by worktree at ..." (exit 128). Return that
            # existing worktree and register it under this issue; the caller then
            # syncs it to the PR head (fetch + reset --hard origin/<branch>).
            existing = (
                None
                if isolated
                else self._worktree_holding_branch(branch_name, timeout=timeout)
            )
            if existing is not None and existing != worktree_path:
                logger.info(
                    "Branch %s already checked out at %s — reusing that worktree for issue #%s",
                    branch_name,
                    existing,
                    issue_number,
                )
                self.worktrees[worktree_key] = existing
                return existing

            if self._reuse_existing_dirty_worktree(
                issue_number,
                worktree_key,
                worktree_path,
                timeout=timeout,
            ):
                return worktree_path

            # Remove existing clean worktree directory if present. Dirty
            # registered worktrees are reused above; non-worktree paths with
            # unknown contents fail closed there instead of being removed.
            if worktree_path.exists():
                logger.warning("Removing existing worktree directory: %s", worktree_path)
                self._remove_worktree_path_forcefully(worktree_path, timeout=timeout)

            try:
                with file_lock(self._git_metadata_lock_path()):
                    try:
                        if isolated:
                            self._add_isolated_worktree_for_branch(
                                worktree_path,
                                branch_name,
                                timeout=timeout,
                            )
                        else:
                            self._add_worktree_for_branch(
                                worktree_path,
                                branch_name,
                                refresh_base=refresh_base,
                                timeout=timeout,
                            )
                    except Exception:
                        self.worktrees.pop(worktree_key, None)
                        self._remove_worktree_path_forcefully(worktree_path, timeout=timeout)
                        raise
                self.worktrees[worktree_key] = worktree_path
                logger.info("Created worktree for issue #%s at %s", issue_number, worktree_path)
                return worktree_path

            except Exception as e:
                raise RuntimeError(f"Failed to create worktree: {e}") from e

    def _add_isolated_worktree_for_branch(
        self,
        worktree_path: Path,
        branch_name: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Create a detached checkout without reusing a writer worktree.

        ``git worktree add --detach`` permits the same branch to be checked out
        elsewhere, because the new worktree is not attached to that branch.
        The worker synchronizes this throwaway checkout to the remote PR head
        before any reviewer runs. If a fork head is not locally available, use
        the base only as a temporary checkout; the existing pull-ref fallback
        in that synchronization path supplies the real reviewed commit.
        """
        try:
            run(
                ["git", "worktree", "add", "--detach", str(worktree_path), branch_name],
                cwd=self.repo_root,
                **_timeout_kw(timeout),
            )
        except subprocess.CalledProcessError:
            # The head can be a fork branch which is not a local ref. Do not
            # attach or create that branch; the worker will fetch/reset it (or
            # its PR ref fallback) before the strict reviewer receives it.
            run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree_path),
                    self._resolve_base_branch(timeout=timeout),
                ],
                cwd=self.repo_root,
                **_timeout_kw(timeout),
            )

    def _remove_worktree_path_forcefully(
        self,
        worktree_path: Path,
        *,
        timeout: int | None = None,
    ) -> None:
        """Remove a worktree path and prune stale git metadata."""
        # Try git worktree remove first to clean up git metadata.
        try:
            run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=self.repo_root,
                check=False,
                **_timeout_kw(timeout),
            )
        except Exception as e:
            logger.debug("git worktree remove failed (expected if not a worktree): %s", e)

        try:
            # Fallback to direct directory removal.
            if worktree_path.exists():
                try:
                    shutil.rmtree(worktree_path)
                except Exception as e:
                    logger.warning(
                        "Failed to remove worktree directory %s directly: %s",
                        worktree_path,
                        e,
                    )
        finally:
            try:
                run(
                    ["git", "worktree", "prune"],
                    cwd=self.repo_root,
                    check=False,
                    **_timeout_kw(timeout),
                )
            except Exception as e:
                logger.debug("git worktree prune failed: %s", e)

    def _add_worktree_for_branch(
        self,
        worktree_path: Path,
        branch_name: str,
        *,
        refresh_base: bool = False,
        timeout: int | None = None,
    ) -> None:
        """Add a git worktree, choosing the right source for ``branch_name``.

        Resolution order:

        1. Branch exists locally → reuse it, then rebase it if this is an
           issue-major fresh-base checkout.
        2. Branch exists on origin only → fetch and extend ``origin/<branch>``,
           so a remote branch from a prior loop is not discarded and re-created
           from base (which would produce a divergent duplicate PR — #1018),
           then rebase it if this is an issue-major fresh-base checkout.
        3. Branch is new → create it from the base branch.

        Args:
            worktree_path: Destination path for the worktree.
            branch_name: Branch the worktree should track.
            refresh_base: When True, reused issue automation branches are rebased
                onto the refreshed base before the implementer starts.
            timeout: Optional timeout in seconds for each git command.

        """
        if self._local_branch_exists(branch_name, timeout=timeout):
            self._refresh_stale_local_branch_if_safe(branch_name, timeout=timeout)
            logger.info("Branch %s already exists, reusing it", branch_name)
            run(
                ["git", "worktree", "add", str(worktree_path), branch_name],
                cwd=self.repo_root,
                **_timeout_kw(timeout),
            )
            self._rebase_existing_issue_branch_if_requested(
                worktree_path,
                branch_name,
                refresh_base=refresh_base,
                timeout=timeout,
            )
        elif self._remote_branch_exists(branch_name, timeout=timeout):
            logger.info(
                "Branch %s exists on origin, extending it from origin/%s",
                branch_name,
                branch_name,
            )
            run(["git", "fetch", "origin", branch_name], cwd=self.repo_root, **_timeout_kw(timeout))
            run(
                [
                    "git",
                    "worktree",
                    "add",
                    str(worktree_path),
                    "-b",
                    branch_name,
                    f"origin/{branch_name}",
                ],
                cwd=self.repo_root,
                **_timeout_kw(timeout),
            )
            self._rebase_existing_issue_branch_if_requested(
                worktree_path,
                branch_name,
                refresh_base=refresh_base,
                timeout=timeout,
            )
        else:
            run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    self._resolve_base_branch(timeout=timeout),
                ],
                cwd=self.repo_root,
                **_timeout_kw(timeout),
            )

    def _rebase_existing_issue_branch_if_requested(
        self,
        worktree_path: Path,
        branch_name: str,
        *,
        refresh_base: bool,
        timeout: int | None = None,
    ) -> None:
        """Rebase reused issue automation branches before implementation starts."""
        if not refresh_base or not self._is_issue_automation_branch(branch_name):
            return
        base_branch_name = self._origin_base_branch_name(timeout=timeout)
        if base_branch_name is None:
            logger.info(
                "Skipping pre-implementation rebase for %s: base %s is not an origin branch",
                branch_name,
                self.base_branch,
            )
            return
        logger.info(
            "Rebasing reused issue branch %s onto %s before implementation",
            branch_name,
            self.base_branch,
        )
        if not rebase_worktree_onto(worktree_path, base_branch_name, **_timeout_kw(timeout)):
            logger.warning(
                "Could not rebase reused issue branch %s onto %s before implementation; "
                "proceeding with current branch head",
                branch_name,
                self.base_branch,
            )

    def _origin_base_branch_name(self, *, timeout: int | None = None) -> str | None:
        """Return the branch name portion for an ``origin/<branch>`` base."""
        base_ref = self._resolve_base_branch(timeout=timeout)
        if base_ref.startswith("refs/remotes/origin/"):
            return base_ref.removeprefix("refs/remotes/origin/")
        if base_ref.startswith("origin/"):
            return base_ref.removeprefix("origin/")
        if "/" not in base_ref and not _looks_like_sha(base_ref):
            return base_ref
        return None

    def _refresh_stale_local_branch_if_safe(
        self,
        branch_name: str,
        *,
        timeout: int | None = None,
    ) -> None:
        """Fast-forward a stale local branch that has no work beyond base.

        A killed or superseded automation run can leave ``<issue>-auto-impl``
        pointing at an old base commit. If that branch has no commits that are
        unique relative to ``self.base_branch``, rerunning the loop should not
        ask the implementer to re-create work that already landed on the base.
        If the branch has any unique commits, keep it untouched so in-flight work
        is preserved.
        """
        if not self._is_issue_automation_branch(branch_name):
            return
        try:
            base_branch = self._resolve_base_branch(timeout=timeout)
            result = run(
                [
                    "git",
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{base_branch}...{branch_name}",
                ],
                cwd=self.repo_root,
                capture_output=True,
                check=False,
                **_timeout_kw(timeout),
            )
            if result.returncode != 0:
                return
            parts = (result.stdout or "").strip().split()
            if len(parts) != 2:
                return
            behind_base, ahead_base = (int(parts[0]), int(parts[1]))
        except Exception as e:
            logger.debug("Could not compute divergence for %s: %s", branch_name, e)
            return

        if ahead_base != 0 or behind_base == 0:
            return

        logger.info(
            "Branch %s has no commits beyond %s and is %s commit(s) behind; fast-forwarding",
            branch_name,
            base_branch,
            behind_base,
        )
        run(
            ["git", "branch", "-f", branch_name, base_branch],
            cwd=self.repo_root,
            **_timeout_kw(timeout),
        )

    def _is_issue_automation_branch(self, branch_name: str) -> bool:
        """Return True for branch names owned by the issue automation loop."""
        issue_prefix, sep, suffix = branch_name.partition("-")
        return bool(sep and issue_prefix.isdigit() and suffix in {"auto", "auto-impl"})

    def _local_branch_exists(self, branch_name: str, *, timeout: int | None = None) -> bool:
        """Return True if ``branch_name`` exists in the local repository.

        Args:
            branch_name: Branch name to verify locally.

        Returns:
            True if the branch resolves locally, False otherwise.

        Raises:
            Exception: If the command runner raises an error outside the
                subprocess exception hierarchy, so unexpected failures retain
                their traceback and reach the caller instead of being
                misclassified as a missing branch.

        """
        try:
            result = run(
                ["git", "rev-parse", "--verify", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                check=False,
                **_timeout_kw(timeout),
            )
            return result.returncode == 0
        except subprocess.SubprocessError:
            logger.warning(
                "Local branch check failed for %s in %s; treating it as absent",
                branch_name,
                self.repo_root,
                exc_info=True,
            )
            return False

    def _remote_branch_exists(self, branch_name: str, *, timeout: int | None = None) -> bool:
        """Return True if ``branch_name`` exists on origin.

        Uses ``git ls-remote --heads origin <branch>`` and checks for the
        ``refs/heads/<branch>`` ref in the output. Any failure (no network,
        unexpected output) is treated as "not present" so worktree creation
        falls back to the base-branch path rather than crashing.

        Args:
            branch_name: Branch name to look up on origin.

        Returns:
            True if the branch exists on origin, False otherwise.

        """
        try:
            result = run(
                ["git", "ls-remote", "--heads", "origin", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                check=False,
                log_errors=False,
                **_timeout_kw(timeout),
            )
            return f"refs/heads/{branch_name}" in (result.stdout or "")
        except Exception as e:
            logger.debug("ls-remote check failed for %s (treating as absent): %s", branch_name, e)
            return False

    def _is_automation_prompt_artifact(self, path: Path) -> bool:
        """Return True for exact generated agent prompt scratch files."""
        if not path.is_file() or not path.name.endswith(".md"):
            return False
        for prefix in _AUTOMATION_PROMPT_PREFIXES:
            if path.name.startswith(prefix):
                issue_part = path.name.removeprefix(prefix).removesuffix(".md")
                return issue_part.isdigit()
        return False

    def _cleanup_automation_prompt_artifacts(self, worktree_path: Path) -> None:
        """Delete generated prompt scratch files from a worktree root.

        These files are written only as debug/session scratch and are removed
        in normal ``finally`` blocks. If an automation process is killed before
        cleanup, they must not cause a worktree to be treated as meaningfully
        dirty.
        """
        if not worktree_path.exists() or not worktree_path.is_dir():
            return
        for child in worktree_path.iterdir():
            if self._is_automation_prompt_artifact(child):
                child.unlink()

    def _path_is_registered_worktree(
        self,
        worktree_path: Path,
        *,
        timeout: int | None = None,
    ) -> bool:
        """Return True when ``worktree_path`` appears in git's worktree list."""
        target = worktree_path.resolve()
        for wt in self.list_worktrees(raise_on_error=True, timeout=timeout):
            path = wt.get("path")
            if path and Path(path).resolve() == target:
                return True
        return False

    def _path_has_contents(self, path: Path) -> bool:
        """Return True if a path exists and contains any directory entries."""
        return path.exists() and path.is_dir() and any(path.iterdir())

    def _reuse_existing_dirty_worktree(
        self,
        issue_number: int,
        worktree_key: int | str,
        worktree_path: Path,
        *,
        timeout: int | None = None,
    ) -> bool:
        """Register and reuse an existing dirty worktree instead of deleting it.

        A previous automation process may have preserved dirty work for an
        issue. A new manager instance has an empty in-memory ``worktrees`` map,
        so create_worktree must inspect the on-disk path before force-removal.
        """
        if not worktree_path.exists():
            return False

        self._cleanup_automation_prompt_artifacts(worktree_path)

        if self._path_is_registered_worktree(worktree_path, timeout=timeout):
            if not is_clean_working_tree(worktree_path, timeout=timeout):
                logger.info(
                    "Reusing dirty existing worktree for issue #%s at %s",
                    issue_number,
                    worktree_path,
                )
                self.worktrees[worktree_key] = worktree_path
                return True
            return False

        if self._path_has_contents(worktree_path):
            raise RuntimeError(
                f"Existing path {worktree_path} is not a registered git worktree and "
                "contains files; refusing to remove it automatically"
            )

        return False

    def remove_worktree(
        self,
        issue_number: int | str,
        force: bool = False,
        *,
        timeout: int | None = None,
    ) -> None:
        """Remove a worktree.

        Args:
            issue_number: Issue number or an internal isolated-worktree key
            force: Force removal even with uncommitted changes
            timeout: Optional timeout in seconds for each git command.

        Raises:
            WorktreeDirtyError: If the worktree has uncommitted changes and force=False
            RuntimeError: If worktree removal fails for another reason

        """
        with self.lock:
            if issue_number not in self.worktrees:
                logger.warning("No worktree found for issue #%s", issue_number)
                return

            worktree_path = self.worktrees[issue_number]

            # Idempotent removal: when several issues share one branch they alias
            # the same path (see create_worktree's branch-reuse), so cleanup_all
            # may reach this with the directory already gone after the first key
            # removed it. Treat an absent directory as already-removed rather than
            # raising [Errno 2] — drop the key and prune stale git metadata (#1532).
            if not worktree_path.exists():
                logger.info(
                    "Worktree directory for issue #%s already gone (%s); pruning metadata",
                    issue_number,
                    worktree_path,
                )
                del self.worktrees[issue_number]
                with contextlib.suppress(Exception):
                    run(
                        ["git", "worktree", "prune"],
                        cwd=self.repo_root,
                        check=False,
                        **_timeout_kw(timeout),
                    )
                return

            self._cleanup_automation_prompt_artifacts(worktree_path)

            if not force and not is_clean_working_tree(worktree_path, timeout=timeout):
                raise WorktreeDirtyError(issue_number, worktree_path)

            try:
                cmd = ["git", "worktree", "remove", str(worktree_path)]
                if force:
                    cmd.append("--force")

                with file_lock(self._git_metadata_lock_path()):
                    run(cmd, cwd=self.repo_root, **_timeout_kw(timeout))

                del self.worktrees[issue_number]
                logger.info("Removed worktree for issue #%s", issue_number)

            except Exception as e:
                raise RuntimeError(f"Failed to remove worktree: {e}") from e

    def get_worktree(self, issue_number: int | str) -> Path | None:
        """Get worktree path for an issue.

        Args:
            issue_number: Issue number or an internal isolated-worktree key

        Returns:
            Worktree path or None if not found

        """
        with self.lock:
            return self.worktrees.get(issue_number)

    def cleanup_all(self, force: bool = False, *, timeout: int | None = None) -> None:
        """Remove all managed worktrees.

        Dirty worktrees (uncommitted changes) are skipped rather than force-removed.
        They are recorded in ``self.preserved`` so callers can surface a rerun command.

        Args:
            force: Force removal even with uncommitted changes
            timeout: Optional timeout in seconds for each git command.

        Note:
            Known limitation: Releases lock between iterations to avoid
            holding it during slow git operations. If concurrent create_worktree
            is called, new worktrees may be added during cleanup. This is
            acceptable since cleanup_all is typically called during shutdown.

        """
        with self.lock:
            issue_numbers = list(self.worktrees.keys())

        # Several issues can alias the same path when they share a branch (see
        # create_worktree's branch-reuse). Remove each distinct directory once;
        # for aliased keys whose path was already removed, just drop the stale
        # registration instead of re-running `git worktree remove` on a gone dir
        # (which logged "[Errno 2]" failures before #1532).
        removed_paths: set[Path] = set()
        for issue_num in issue_numbers:
            with self.lock:
                path = self.worktrees.get(issue_num)
            if path is not None and path in removed_paths:
                with self.lock:
                    self.worktrees.pop(issue_num, None)
                logger.debug(
                    "Issue #%s aliases already-removed worktree %s; dropping registration",
                    issue_num,
                    path,
                )
                continue
            try:
                self.remove_worktree(issue_num, force=force, timeout=timeout)
                if path is not None:
                    removed_paths.add(path)
            except WorktreeDirtyError as e:
                logger.info("Preserved dirty worktree for issue #%s at %s", e.issue_number, e.path)
                self.preserved.append((e.issue_number, e.path))
            except Exception as e:
                logger.error("Failed to remove worktree for issue #%s: %s", issue_num, e)

    def prune_worktrees(self, *, timeout: int | None = None) -> None:
        """Prune stale worktree administrative files.

        Useful for cleaning up after manual worktree deletion.
        """
        try:
            run(["git", "worktree", "prune"], cwd=self.repo_root, **_timeout_kw(timeout))
            logger.info("Pruned stale worktrees")
        except Exception as e:
            logger.error("Failed to prune worktrees: %s", e)

    def _worktree_holding_branch(
        self,
        branch_name: str,
        *,
        timeout: int | None = None,
    ) -> Path | None:
        """Return the path of the worktree that has ``branch_name`` checked out.

        git refuses to check out the same branch in two worktrees, so before
        adding a worktree we must detect an existing one holding the branch and
        reuse it. ``list_worktrees`` reports the branch as the full ref
        ``refs/heads/<name>``; match on that. Returns ``None`` if no worktree
        holds the branch.
        """
        target_ref = f"refs/heads/{branch_name}"
        try:
            worktrees = self.list_worktrees(raise_on_error=True, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"Cannot safely determine whether branch {branch_name!r} is already "
                "checked out in another worktree"
            ) from e

        for wt in worktrees:
            if wt.get("branch") == target_ref:
                return Path(wt["path"])
        return None

    def list_worktrees(
        self,
        *,
        raise_on_error: bool = False,
        timeout: int | None = None,
    ) -> list[dict[str, str]]:
        """List all git worktrees in the repository.

        Args:
            raise_on_error: When True, propagate git/listing failures so callers
                that would otherwise force-remove or collide fail closed.
            timeout: Optional timeout in seconds for the git command.

        Returns:
            List of worktree info dictionaries

        """
        try:
            result = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
                **_timeout_kw(timeout),
            )

            worktrees = []
            current: dict[str, str] = {}

            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    if current:
                        worktrees.append(current)
                        current = {}
                    continue

                if line.startswith("worktree "):
                    current["path"] = line.split(" ", 1)[1]
                elif line.startswith("branch "):
                    current["branch"] = line.split(" ", 1)[1]
                elif line.startswith("HEAD "):
                    current["commit"] = line.split(" ", 1)[1]

            if current:
                worktrees.append(current)

            return worktrees

        except Exception as e:
            logger.error("Failed to list worktrees: %s", e)
            if raise_on_error:
                raise RuntimeError("Failed to list git worktrees") from e
            return []

    def ensure_branch_deleted(self, branch_name: str, *, timeout: int | None = None) -> None:
        """Ensure a branch is deleted from local and remote.

        Args:
            branch_name: Branch name to delete
            timeout: Optional timeout in seconds for each git command.

        """
        # Delete local branch
        try:
            run(
                ["git", "branch", "-D", branch_name],
                cwd=self.repo_root,
                check=False,
                **_timeout_kw(timeout),
            )
            logger.debug("Deleted local branch %s", branch_name)
        except Exception as e:
            logger.warning("Failed to delete local branch %s: %s", branch_name, e)

        # Delete remote branch
        try:
            run(
                ["git", "push", "origin", "--delete", branch_name],
                cwd=self.repo_root,
                check=False,
                **_timeout_kw(timeout),
            )
            logger.debug("Deleted remote branch %s", branch_name)
        except Exception as e:
            logger.warning("Failed to delete remote branch %s: %s", branch_name, e)
