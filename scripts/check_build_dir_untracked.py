#!/usr/bin/env python3
"""Reject tracked files under ``build/``.

``build/`` is the sanctioned, gitignored scratch location for the automation
loop (see ``hephaestus/automation/loop_runner.py`` and CLAUDE.md). Its name
collides with the packaging-output convention, so a stray ``git add build/...``
or a widened sdist allowlist could sweep automation logs into a distribution
(issue #1214). This guard hard-fails if anything becomes tracked under
``build/`` so the collision can never be realized.

Usage:
    python scripts/check_build_dir_untracked.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def tracked_build_files(repo_root: Path) -> list[str]:
    """Return git-tracked paths under ``build/`` (empty when clean)."""
    result = subprocess.run(
        ["git", "ls-files", "build/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    """Fail (exit 1) if any file is tracked under ``build/``."""
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        return 0
    repo_root = get_repo_root()
    tracked = tracked_build_files(repo_root)
    if tracked:
        print("ERROR: files are tracked under build/, which is gitignored")
        print("automation scratch, not packaging output (issue #1214).")
        for path in tracked:
            print(f"  {path}")
        print("\nUntrack them with: git rm --cached " + " ".join(tracked))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
