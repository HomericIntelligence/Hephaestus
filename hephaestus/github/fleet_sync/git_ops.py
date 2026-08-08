"""Fleet-specific git operations for PR rebasing and worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hephaestus.github.fleet_sync.gpg import get_resign_exec
from hephaestus.github.fleet_sync.models import UNICODE_SYMBOLS, PRInfo, Symbols
from hephaestus.github.git_ops import run_git
from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import NETWORK_TIMEOUT

logger = get_logger(__name__)


def _git(
    args: list[str],
    cwd: Path,
    dry_run: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in a working directory."""
    if dry_run:
        logger.info("[dry-run] git %s (in %s)", " ".join(args), cwd)
        return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
    return run_git(args, cwd=cwd, check=check, timeout=NETWORK_TIMEOUT)


def ensure_repo_clone(repo: str, org: str, clone_dir: Path, dry_run: bool = False) -> Path:
    """Return a single reusable clone of ``repo``, cloning once or fetching if present."""
    repo_url = f"https://github.com/{org}/{repo}.git"
    clone_path = clone_dir / repo
    git_dir = clone_path / ".git"

    if git_dir.exists():
        logger.info("  Reusing existing clone of %s; fetching latest...", repo)
        _git(["fetch", "--prune", "origin"], cwd=clone_path, dry_run=dry_run, check=False)
        return clone_path

    logger.info("  Cloning %s (once, reused for all its PRs)...", repo)
    _git(["clone", "--filter=blob:none", repo_url, str(clone_path)], cwd=clone_dir, dry_run=dry_run)
    return clone_path


def add_pr_worktree(
    repo_clone: Path,
    work: Path,
    branch: str,
    base: str,
    dry_run: bool = False,
) -> None:
    """Create a worktree for ``branch`` off the shared repo clone."""
    _git(["fetch", "origin", branch], cwd=repo_clone, dry_run=dry_run)
    _git(["fetch", "origin", base], cwd=repo_clone, dry_run=dry_run)

    remove_worktree(repo_clone, work, dry_run=dry_run)
    _git(
        ["worktree", "add", "--force", "-B", branch, str(work), f"origin/{branch}"],
        cwd=repo_clone,
        dry_run=dry_run,
    )


def remove_worktree(repo_clone: Path, work: Path, dry_run: bool = False) -> None:
    """Remove a per-PR worktree, leaving the shared clone intact."""
    if not work.exists():
        return
    _git(
        ["worktree", "remove", "--force", str(work)],
        cwd=repo_clone,
        dry_run=dry_run,
        check=False,
    )


def rebase_and_resign(
    pr: PRInfo,
    repo_clone: Path,
    dry_run: bool = False,
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
) -> bool:
    """Fetch a PR branch, rebase it on origin/base, re-sign all commits, and push."""
    branch = pr.head_ref
    base = pr.base_ref
    work = repo_clone.parent / f"{pr.repo}-{pr.number}"

    try:
        add_pr_worktree(repo_clone, work, branch, base, dry_run=dry_run)

        result = _git(
            ["rebase", f"origin/{base}", "--exec", get_resign_exec()],
            cwd=work,
            dry_run=dry_run,
            check=False,
        )

        if result.returncode != 0:
            logger.warning(
                "  Rebase failed for PR #%d %s conflict detected", pr.number, symbols.dash
            )
            _git(["rebase", "--abort"], cwd=work, dry_run=dry_run, check=False)
            return False

        _git(["push", "--force-with-lease", "origin", branch], cwd=work, dry_run=dry_run)
        logger.info("  %s Rebased and re-signed PR #%d", symbols.check, pr.number)
        return True

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error("  Rebase/push failed for PR #%d: %s", pr.number, e.stderr or str(e))
        return False
    finally:
        remove_worktree(repo_clone, work, dry_run=dry_run)
