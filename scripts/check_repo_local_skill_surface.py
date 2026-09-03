#!/usr/bin/env python3
"""Reject repo-local skill copies, skill symlinks, and ``skills-lock.json``.

Hephaestus uses the Athena marketplace plugins declared in
``.claude/settings.json`` as the source of truth for skills. Repo-local skill
copies, repo-local skill symlinks, and the unused ``skills-lock.json`` file are
not part of that source model and must not reappear.

Usage:
    python scripts/check_repo_local_skill_surface.py
"""

from __future__ import annotations

import sys
from pathlib import Path

FORBIDDEN_SURFACES: tuple[Path, ...] = (
    Path(".agents/skills"),
    Path(".claude/skills"),
    Path("skills-lock.json"),
)


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def find_repo_local_skill_surfaces(repo_root: Path) -> list[str]:
    """Return repo-local skill surfaces that exist under *repo_root*."""
    findings: list[str] = []
    for relative_path in FORBIDDEN_SURFACES:
        path = repo_root / relative_path
        if path.exists() or path.is_symlink():
            findings.append(relative_path.as_posix())
    return findings


def main() -> int:
    """Fail if any repo-local skill surface exists."""
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        return 0
    repo_root = get_repo_root()
    findings = find_repo_local_skill_surfaces(repo_root)
    if findings:
        print("ERROR: repo-local skill surfaces are not allowed.")
        print("Use the Athena marketplace plugins declared in .claude/settings.json.")
        for path in findings:
            print(f"  {path}")
        print("Remove the local copy, symlink, or lockfile before you commit.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
