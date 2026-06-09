"""Git worktree management for parallel issue implementation.

Provides:
- Thread-safe worktree creation and cleanup
- Branch management
- Worktree state tracking

Layout note
-----------
Worktrees live under ``<repo_root>/build/.worktrees/issue-{N}``. Putting them
inside the repo (rather than ``~/.tmp``) keeps them visible to the implementer
process even after a force-kill: an interrupted run leaves the worktree on
disk so a subsequent invocation can either resume work or surface a
``WorktreeDirtyError`` to the operator. The trade-off is that ``git status``
in the parent repo can show ``build/.worktrees/`` as untracked if it isn't ignored;
ensure ``build/.worktrees/`` is in ``.gitignore`` (or a global ignore) for any repo
that runs the automation. Recovery procedure for a force-killed loop:

    git -C <repo> worktree list
    # Inspect each build/.worktrees/issue-N for unexpected modifications
    git -C <repo> worktree remove build/.worktrees/issue-N    # if abandoning
    rm -rf <repo>/build/.worktrees/issue-N                    # last resort
"""

import logging
import os
import shutil
import threading
from pathlib import Path

from .git_utils import get_repo_root, is_clean_working_tree, run

logger = logging.getLogger(__name__)

_AUTOMATION_PROMPT_PREFIXES = (
    ".claude-pr-review-",
    ".claude-address-review-",
    ".claude-prompt-",
    ".claude-followup-",
)


def _loop_trunk_githash() -> str | None:
    """Return the loop-provided trunk commit-ish when available."""
    trunk = os.environ.get("HEPH_TRUNK_GITHASH", "").strip()
    if not trunk or trunk == "unknown":
        return None
    return trunk


class WorktreeDirtyError(Exception):
    """Raised when a worktree cannot be removed because it contains uncommitted changes."""

    def __init__(self, issue_number: int, path: Path) -> None:
        """Initialize with the affected issue number and worktree path."""
        self.issue_number = issue_number
        self.path = path
        super().__init__(f"Worktree for issue #{issue_number} at {path} has uncommitted changes")


class WorktreeManager:
    """Thread-safe manager for git worktrees.

    Allows parallel issue implementation in isolated worktrees.
    """

    def __init__(self, base_dir: Path | None = None, base_branch: str | None = None):
        """Initialize worktree manager.

        Args:
            base_dir: Base directory for worktrees (default: repo_root/build/.worktrees)
            base_branch: Base branch for worktrees (default: auto-detect from origin/HEAD
                lazily on first use)

        """
        self.repo_root = get_repo_root()
        if base_dir is None:
            base_dir = self.repo_root / "build" / ".worktrees"
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Base-branch detection is deferred to first use so that constructing
        # a WorktreeManager in test fixtures or environments without origin/*
        # refs does not raise. The automation loop passes HEPH_TRUNK_GITHASH so
        # phase subprocesses create issue worktrees from the exact trunk commit
        # they are validating, including local signed commits not yet on origin.
        # The hard error from #382 / A4-05 still fires when neither explicit
        # base nor loop trunk is available.
        self._base_branch_override = base_branch or _loop_trunk_githash()
        self._base_branch_resolved: str | None = None
        self.worktrees: dict[int, Path] = {}
        self.preserved: list[tuple[int, Path]] = []
        self.lock = threading.Lock()

        logger.debug("Initialized WorktreeManager at %s", self.base_dir)

    @property
    def base_branch(self) -> str:
        """The base branch, auto-detected on first access."""
        if self._base_branch_resolved is not None:
            return self._base_branch_resolved
        if self._base_branch_override is not None:
            self._base_branch_resolved = self._base_branch_override
            return self._base_branch_resolved
        self._base_branch_resolved = self._detect_base_branch()
        return self._base_branch_resolved

    def _detect_base_branch(self) -> str:
        try:
            result = run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                cwd=self.repo_root,
                capture_output=True,
                log_errors=False,
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
    ) -> Path:
        """Create a new worktree for an issue.

        Args:
            issue_number: Issue number
            branch_name: Branch name (default: {issue_number}-auto)

        Returns:
            Path to worktree directory

        Raises:
            RuntimeError: If worktree creation fails

        """
        with self.lock:
            if issue_number in self.worktrees:
                logger.warning("Worktree for issue #%s already exists", issue_number)
                return self.worktrees[issue_number]

            if branch_name is None:
                branch_name = f"{issue_number}-auto"

            worktree_path = self.base_dir / f"issue-{issue_number}"

            # Reuse, don't collide: git forbids the same branch in two worktrees.
            # When ``branch_name`` is already checked out elsewhere (e.g. a PR
            # resolved to its real head branch ``708-auto-impl`` which the
            # issue-708 worktree already holds), ``git worktree add`` would fail
            # with "already used by worktree at ..." (exit 128). Return that
            # existing worktree and register it under this issue; the caller then
            # syncs it to the PR head (fetch + reset --hard origin/<branch>).
            existing = self._worktree_holding_branch(branch_name)
            if existing is not None and existing != worktree_path:
                logger.info(
                    "Branch %s already checked out at %s — reusing that worktree for issue #%s",
                    branch_name,
                    existing,
                    issue_number,
                )
                self.worktrees[issue_number] = existing
                return existing

            if self._reuse_existing_dirty_worktree(issue_number, worktree_path):
                return worktree_path

            # Remove existing clean worktree directory if present. Dirty
            # registered worktrees are reused above; non-worktree paths with
            # unknown contents fail closed there instead of being removed.
            if worktree_path.exists():
                logger.warning("Removing existing worktree directory: %s", worktree_path)
                # Try git worktree remove first to clean up git metadata
                try:
                    run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        cwd=self.repo_root,
                        check=False,
                    )
                except Exception as e:
                    logger.debug("git worktree remove failed (expected if not a worktree): %s", e)

                # Fallback to direct directory removal
                if worktree_path.exists():
                    shutil.rmtree(worktree_path)

                # Prune stale worktree metadata
                try:
                    run(["git", "worktree", "prune"], cwd=self.repo_root, check=False)
                except Exception as e:
                    logger.debug("git worktree prune failed: %s", e)

            try:
                self._add_worktree_for_branch(worktree_path, branch_name)
                self.worktrees[issue_number] = worktree_path
                logger.info("Created worktree for issue #%s at %s", issue_number, worktree_path)
                return worktree_path

            except Exception as e:
                raise RuntimeError(f"Failed to create worktree: {e}") from e

    def _add_worktree_for_branch(self, worktree_path: Path, branch_name: str) -> None:
        """Add a git worktree, choosing the right source for ``branch_name``.

        Resolution order:

        1. Branch exists locally → reuse it.
        2. Branch exists on origin only → fetch and extend ``origin/<branch>``,
           so a remote branch from a prior loop is not discarded and re-created
           from base (which would produce a divergent duplicate PR — #1018).
        3. Branch is new → create it from the base branch.

        Args:
            worktree_path: Destination path for the worktree.
            branch_name: Branch the worktree should track.

        """
        if self._local_branch_exists(branch_name):
            self._refresh_stale_local_branch_if_safe(branch_name)
            logger.info("Branch %s already exists, reusing it", branch_name)
            run(
                ["git", "worktree", "add", str(worktree_path), branch_name],
                cwd=self.repo_root,
            )
        elif self._remote_branch_exists(branch_name):
            logger.info(
                "Branch %s exists on origin, extending it from origin/%s",
                branch_name,
                branch_name,
            )
            run(["git", "fetch", "origin", branch_name], cwd=self.repo_root)
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
                    self.base_branch,
                ],
                cwd=self.repo_root,
            )

    def _refresh_stale_local_branch_if_safe(self, branch_name: str) -> None:
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
            result = run(
                [
                    "git",
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{self.base_branch}...{branch_name}",
                ],
                cwd=self.repo_root,
                capture_output=True,
                check=False,
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
            self.base_branch,
            behind_base,
        )
        run(["git", "branch", "-f", branch_name, self.base_branch], cwd=self.repo_root)

    def _is_issue_automation_branch(self, branch_name: str) -> bool:
        """Return True for branch names owned by the issue automation loop."""
        issue_prefix, sep, suffix = branch_name.partition("-")
        return bool(sep and issue_prefix.isdigit() and suffix in {"auto", "auto-impl"})

    def _local_branch_exists(self, branch_name: str) -> bool:
        """Return True if ``branch_name`` exists in the local repository.

        Args:
            branch_name: Branch name to verify locally.

        Returns:
            True if the branch resolves locally, False otherwise.

        """
        try:
            result = run(
                ["git", "rev-parse", "--verify", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _remote_branch_exists(self, branch_name: str) -> bool:
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

    def _path_is_registered_worktree(self, worktree_path: Path) -> bool:
        """Return True when ``worktree_path`` appears in git's worktree list."""
        target = worktree_path.resolve()
        for wt in self.list_worktrees(raise_on_error=True):
            path = wt.get("path")
            if path and Path(path).resolve() == target:
                return True
        return False

    def _path_has_contents(self, path: Path) -> bool:
        """Return True if a path exists and contains any directory entries."""
        return path.exists() and path.is_dir() and any(path.iterdir())

    def _reuse_existing_dirty_worktree(self, issue_number: int, worktree_path: Path) -> bool:
        """Register and reuse an existing dirty worktree instead of deleting it.

        A previous automation process may have preserved dirty work for an
        issue. A new manager instance has an empty in-memory ``worktrees`` map,
        so create_worktree must inspect the on-disk path before force-removal.
        """
        if not worktree_path.exists():
            return False

        self._cleanup_automation_prompt_artifacts(worktree_path)

        if self._path_is_registered_worktree(worktree_path):
            if not is_clean_working_tree(worktree_path):
                logger.info(
                    "Reusing dirty existing worktree for issue #%s at %s",
                    issue_number,
                    worktree_path,
                )
                self.worktrees[issue_number] = worktree_path
                return True
            return False

        if self._path_has_contents(worktree_path):
            raise RuntimeError(
                f"Existing path {worktree_path} is not a registered git worktree and "
                "contains files; refusing to remove it automatically"
            )

        return False

    def remove_worktree(self, issue_number: int, force: bool = False) -> None:
        """Remove a worktree.

        Args:
            issue_number: Issue number
            force: Force removal even with uncommitted changes

        Raises:
            WorktreeDirtyError: If the worktree has uncommitted changes and force=False
            RuntimeError: If worktree removal fails for another reason

        """
        with self.lock:
            if issue_number not in self.worktrees:
                logger.warning("No worktree found for issue #%s", issue_number)
                return

            worktree_path = self.worktrees[issue_number]
            self._cleanup_automation_prompt_artifacts(worktree_path)

            if not force and not is_clean_working_tree(worktree_path):
                raise WorktreeDirtyError(issue_number, worktree_path)

            try:
                cmd = ["git", "worktree", "remove", str(worktree_path)]
                if force:
                    cmd.append("--force")

                run(cmd, cwd=self.repo_root)

                del self.worktrees[issue_number]
                logger.info("Removed worktree for issue #%s", issue_number)

            except Exception as e:
                raise RuntimeError(f"Failed to remove worktree: {e}") from e

    def get_worktree(self, issue_number: int) -> Path | None:
        """Get worktree path for an issue.

        Args:
            issue_number: Issue number

        Returns:
            Worktree path or None if not found

        """
        with self.lock:
            return self.worktrees.get(issue_number)

    def cleanup_all(self, force: bool = False) -> None:
        """Remove all managed worktrees.

        Dirty worktrees (uncommitted changes) are skipped rather than force-removed.
        They are recorded in ``self.preserved`` so callers can surface a rerun command.

        Args:
            force: Force removal even with uncommitted changes

        Note:
            Known limitation: Releases lock between iterations to avoid
            holding it during slow git operations. If concurrent create_worktree
            is called, new worktrees may be added during cleanup. This is
            acceptable since cleanup_all is typically called during shutdown.

        """
        with self.lock:
            issue_numbers = list(self.worktrees.keys())

        for issue_num in issue_numbers:
            try:
                self.remove_worktree(issue_num, force=force)
            except WorktreeDirtyError as e:
                logger.info("Preserved dirty worktree for issue #%s at %s", e.issue_number, e.path)
                self.preserved.append((e.issue_number, e.path))
            except Exception as e:
                logger.error("Failed to remove worktree for issue #%s: %s", issue_num, e)

    def prune_worktrees(self) -> None:
        """Prune stale worktree administrative files.

        Useful for cleaning up after manual worktree deletion.
        """
        try:
            run(["git", "worktree", "prune"], cwd=self.repo_root)
            logger.info("Pruned stale worktrees")
        except Exception as e:
            logger.error("Failed to prune worktrees: %s", e)

    def _worktree_holding_branch(self, branch_name: str) -> Path | None:
        """Return the path of the worktree that has ``branch_name`` checked out.

        git refuses to check out the same branch in two worktrees, so before
        adding a worktree we must detect an existing one holding the branch and
        reuse it. ``list_worktrees`` reports the branch as the full ref
        ``refs/heads/<name>``; match on that. Returns ``None`` if no worktree
        holds the branch.
        """
        target_ref = f"refs/heads/{branch_name}"
        try:
            worktrees = self.list_worktrees(raise_on_error=True)
        except Exception as e:
            raise RuntimeError(
                f"Cannot safely determine whether branch {branch_name!r} is already "
                "checked out in another worktree"
            ) from e

        for wt in worktrees:
            if wt.get("branch") == target_ref:
                return Path(wt["path"])
        return None

    def list_worktrees(self, *, raise_on_error: bool = False) -> list[dict[str, str]]:
        """List all git worktrees in the repository.

        Args:
            raise_on_error: When True, propagate git/listing failures so callers
                that would otherwise force-remove or collide fail closed.

        Returns:
            List of worktree info dictionaries

        """
        try:
            result = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                capture_output=True,
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

    def ensure_branch_deleted(self, branch_name: str) -> None:
        """Ensure a branch is deleted from local and remote.

        Args:
            branch_name: Branch name to delete

        """
        # Delete local branch
        try:
            run(
                ["git", "branch", "-D", branch_name],
                cwd=self.repo_root,
                check=False,
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
            )
            logger.debug("Deleted remote branch %s", branch_name)
        except Exception as e:
            logger.warning("Failed to delete remote branch %s: %s", branch_name, e)
