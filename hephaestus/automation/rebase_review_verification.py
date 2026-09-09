"""Check Git trees before a review continues after a rebase."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import hephaestus.automation.git_utils as git_utils

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def verify_rebase_tree(
    cwd: Path,
    *,
    reviewed_head_sha: str,
    reviewed_base_sha: str,
    target_base_sha: str,
    resulting_head_sha: str,
    timeout: int,
) -> str | None:
    """Require the exact tree from the initial change and the new base."""
    if any(
        _SHA.fullmatch(value) is None
        for value in (reviewed_head_sha, reviewed_base_sha, target_base_sha, resulting_head_sha)
    ):
        return None
    try:
        ancestor = git_utils.run(
            ["git", "merge-base", "--all", reviewed_head_sha, reviewed_base_sha],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        base = ancestor.stdout.strip()
        if ancestor.returncode != 0 or _SHA.fullmatch(base) is None:
            return None
        replay = git_utils.run(
            [
                "git",
                "merge-tree",
                "--write-tree",
                "--no-messages",
                f"--merge-base={base}",
                reviewed_head_sha,
                target_base_sha,
            ],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        if replay.returncode != 0:
            return None
        tree = replay.stdout.strip()
        if _SHA.fullmatch(tree) is None:
            return None
        actual = git_utils.run(
            ["git", "rev-parse", "--verify", f"{resulting_head_sha}^{{tree}}"],
            cwd=cwd,
            check=False,
            timeout=timeout,
        )
        if actual.returncode != 0 or actual.stdout.strip() != tree:
            return None
        return tree
    except (OSError, subprocess.SubprocessError):
        return None
