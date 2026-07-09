"""Shared git subprocess helpers for GitHub-facing CLIs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT, run_subprocess


def _cwd_arg(cwd: Path | str | None) -> str | None:
    """Return a subprocess-compatible cwd value."""
    return str(cwd) if isinstance(cwd, Path) else cwd


def _git_command(args: list[str]) -> list[str]:
    """Normalize a git argument vector to exactly one leading ``git``."""
    normalized = list(args)
    if normalized and normalized[0] == "git":
        normalized = normalized[1:]
    return ["git", *normalized]


def run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int | None = NETWORK_TIMEOUT,
    capture_output: bool = True,
    text: bool = True,
    check: bool = True,
    dry_run: bool = False,
    log_on_error: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git through the repository's standard subprocess helper."""
    if not capture_output or not text:
        raise ValueError("run_git always captures text output through run_subprocess")
    return run_subprocess(
        _git_command(args),
        cwd=_cwd_arg(cwd),
        check=check,
        timeout=timeout,
        dry_run=dry_run,
        log_on_error=log_on_error,
    )


def git_config_get(key: str, *, global_: bool = False, cwd: Path | str | None = None) -> str | None:
    """Return a git config value, or None when the key is unset."""
    args = ["config"]
    if global_:
        args.append("--global")
    args.extend(["--get", key])
    try:
        result = run_git(
            args,
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return value


def git_remote_url(remote: str = "origin", *, cwd: Path | str | None = None) -> str | None:
    """Return a git remote URL, or None when it cannot be read."""
    try:
        result = run_git(
            ["remote", "get-url", remote],
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return value


def git_branch_exists(branch_name: str, *, cwd: Path | str | None = None) -> bool:
    """Return whether a local branch exists."""
    try:
        result = run_git(
            ["branch", "--list", branch_name],
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return bool(result.stdout.strip())


def git_push(
    cwd: Path | str | None,
    remote: str,
    refspec: str,
    *,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Push ``refspec`` to ``remote`` from ``cwd``."""
    return run_git(
        ["push", remote, refspec],
        cwd=cwd,
        dry_run=dry_run,
        timeout=NETWORK_TIMEOUT,
    )


def git_unmerged_files(cwd: Path | str) -> list[str]:
    """Return files with unresolved merge conflicts in ``cwd``."""
    result = run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=cwd,
        timeout=METADATA_TIMEOUT,
    )
    return [path.strip() for path in result.stdout.splitlines() if path.strip()]


def git_rev_list_count(cwd: Path | str, revspec: str) -> int:
    """Return ``git rev-list --count`` for ``revspec`` in ``cwd``."""
    result = run_git(
        ["rev-list", "--count", revspec],
        cwd=cwd,
        timeout=METADATA_TIMEOUT,
    )
    return int(result.stdout.strip())


def git_ls_remote_contains(cwd: Path | str, remote: str, ref: str) -> bool:
    """Return whether ``remote`` advertises ``ref``."""
    result = run_git(
        ["ls-remote", remote, ref],
        cwd=cwd,
        timeout=NETWORK_TIMEOUT,
        check=False,
    )
    return result.returncode == 0 and ref in result.stdout


def working_tree_clean() -> bool:
    """Return True if the current git working tree has no uncommitted changes."""
    result = run_git(
        ["status", "--porcelain"],
        timeout=METADATA_TIMEOUT,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def in_git_repo() -> bool:
    """Return True if the current directory is inside a git repository."""
    result = run_git(
        ["rev-parse", "--git-dir"],
        timeout=METADATA_TIMEOUT,
        check=False,
    )
    return result.returncode == 0


def repo_root() -> Path:
    """Return the root directory of the current git repository."""
    result = run_git(
        ["rev-parse", "--show-toplevel"],
        timeout=METADATA_TIMEOUT,
    )
    return Path(result.stdout.strip())
