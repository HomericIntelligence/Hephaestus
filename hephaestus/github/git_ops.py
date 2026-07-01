"""Shared git subprocess helpers for GitHub-facing CLIs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = NETWORK_TIMEOUT,
    capture_output: bool = True,
    text: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git with the repository's standard subprocess defaults."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
    )


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
