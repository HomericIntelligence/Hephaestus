"""Shared git subprocess helpers for library and product layers."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT, run_subprocess
from hephaestus.utils.retry import is_network_error, retry_with_backoff

logger = logging.getLogger(__name__)

_NETWORK_GIT_COMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "push"})


def _cwd_arg(cwd: Path | str | None) -> str | None:
    """Return a subprocess-compatible cwd value."""
    return str(cwd) if isinstance(cwd, Path) else cwd


def _git_command(args: list[str]) -> list[str]:
    """Normalize a git argument vector to exactly one leading ``git``."""
    normalized = list(args)
    if normalized and normalized[0] == "git":
        normalized = normalized[1:]
    return ["git", *normalized]


def _git_args(args: list[str]) -> list[str]:
    """Return git arguments without the leading executable."""
    return _git_command(args)[1:]


def _is_retryable_git_error(error: BaseException) -> bool:
    """Return True for transient Git subprocess failures."""
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    blob = "\n".join(part for part in (error.stdout, error.stderr, str(error)) if part)
    return is_network_error(Exception(blob))


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
    env: dict[str, str] | None = None,
    retries: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git through the repository's standard subprocess helper."""
    if not capture_output or not text:
        raise ValueError("run_git always captures text output through run_subprocess")

    normalized_args = _git_args(args)
    if retries is None:
        retries = 2 if normalized_args and normalized_args[0] in _NETWORK_GIT_COMMANDS else 0

    def _call() -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "cwd": _cwd_arg(cwd),
            "check": check,
            "timeout": timeout,
            "dry_run": dry_run,
            "log_on_error": log_on_error,
        }
        if env is not None:
            kwargs["env"] = env
        return run_subprocess(["git", *normalized_args], **kwargs)

    if retries <= 0:
        return _call()

    @retry_with_backoff(
        max_retries=retries,
        initial_delay=1.0,
        backoff_factor=2,
        retry_on=(subprocess.CalledProcessError, subprocess.TimeoutExpired),
        logger=logger.warning,
        jitter=True,
        retry_predicate=_is_retryable_git_error,
    )
    def _retrying_call() -> subprocess.CompletedProcess[str]:
        return _call()

    return _retrying_call()


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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return bool(result.stdout.strip())


def git_push(
    cwd: Path | str | None,
    remote: str,
    refspec: str,
    *,
    dry_run: bool = False,
    retries: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Push ``refspec`` to ``remote`` from ``cwd``."""
    return run_git(
        ["push", remote, refspec],
        cwd=cwd,
        dry_run=dry_run,
        timeout=NETWORK_TIMEOUT,
        retries=retries,
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


def _remote_ref_candidates(ref: str) -> set[str]:
    """Return exact advertised refs that satisfy ``ref``."""
    candidates = {ref}
    if ref != "HEAD" and not ref.startswith("refs/"):
        candidates.add(f"refs/heads/{ref}")
    return candidates


def git_ls_remote_contains(cwd: Path | str, remote: str, ref: str) -> bool:
    """Return whether ``remote`` advertises ``ref`` as an exact ref."""
    try:
        result = run_git(
            ["ls-remote", remote, ref],
            cwd=cwd,
            timeout=NETWORK_TIMEOUT,
            check=True,
            log_on_error=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False

    candidates = _remote_ref_candidates(ref)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] in candidates:
            return True
    return False


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


__all__ = [
    "git_branch_exists",
    "git_config_get",
    "git_ls_remote_contains",
    "git_push",
    "git_remote_url",
    "git_rev_list_count",
    "git_unmerged_files",
    "in_git_repo",
    "repo_root",
    "run_git",
    "working_tree_clean",
]
