"""Dependency-neutral Git execution and repository identity helpers."""

import logging
import subprocess
from pathlib import Path

from hephaestus.config.child_environments import read_approved_parent_env
from hephaestus.utils.cache import ThreadSafeCache
from hephaestus.utils.git import run_git as _shared_run_git
from hephaestus.utils.helpers import get_repo_root as get_repo_root, run_subprocess

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
    """Run a subprocess command with consistent, redacted error handling."""
    logger.debug("Running subprocess")
    try:
        if cmd and cmd[0] == "git":
            return _shared_run_git(
                cmd,
                cwd=cwd,
                timeout=timeout,
                check=check,
                log_on_error=False,
                env=env,
                retries=0,
            )
        return run_subprocess(
            cmd,
            env=env if env is not None else read_approved_parent_env(),
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            check=check,
            log_on_error=False,
        )
    except subprocess.TimeoutExpired:
        if log_errors:
            logger.error("Subprocess timed out")
        raise
    except subprocess.CalledProcessError as error:
        if log_errors:
            logger.error("Subprocess failed with exit code %s", error.returncode)
        raise


_repo_info_cache: ThreadSafeCache[Path | None, tuple[str, str]] = ThreadSafeCache()
_repo_slug_cache: ThreadSafeCache[Path | None, str] = ThreadSafeCache()


def get_repo_info(repo_root: Path | None = None) -> tuple[str, str]:
    """Get repository owner and name from the Git ``origin`` remote."""
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

            # Parse SSH (git@github.com:owner/repo.git) and HTTPS remotes.
            if "@" in remote_url and ":" in remote_url:
                parts = remote_url.split(":")[-1].replace(".git", "").split("/")
                owner, repo = parts[-2], parts[-1]
            elif remote_url.startswith("https://"):
                parts = remote_url.replace(".git", "").split("/")
                owner, repo = parts[-2], parts[-1]
            else:
                raise RuntimeError(f"Unable to parse git remote URL: {remote_url}")

            logger.debug("Detected repo: %s/%s", owner, repo)
            return owner, repo
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Failed to get git remote URL: {error}") from error

    return _repo_info_cache.get_or_compute(key, _compute)


def get_repo_slug(repo_root: Path | None = None) -> str:
    """Return the cached short repository name, falling back to ``repo``."""
    key = repo_root.resolve() if repo_root is not None else None

    def _compute() -> str:
        try:
            _, repo = get_repo_info(repo_root)
        except (RuntimeError, subprocess.CalledProcessError):
            return "repo"
        return repo

    return _repo_slug_cache.get_or_compute(key, _compute)


def clear_repo_caches() -> None:
    """Clear repository identity caches."""
    _repo_info_cache.clear()
    _repo_slug_cache.clear()


def issue_ref(issue_number: int | str) -> str:
    """Return a repository-qualified issue reference."""
    return f"{get_repo_slug()}#{issue_number}"


def pr_ref(pr_number: int | str) -> str:
    """Return a repository-qualified pull-request reference."""
    return f"{get_repo_slug()}#{pr_number}"
