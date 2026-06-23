#!/usr/bin/env python3
"""Reject local private tokens in tracked or staged text files.

Operators can create an untracked ``.heph-private-denylist`` at the repository
root with one fixed string per line. When present, this guard scans supplied
paths (pre-commit mode) or git-tracked files (manual mode) and fails if any
denylisted string appears.

Usage:
    python scripts/check_private_denylist.py [paths...]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

DENYLIST_FILENAME = ".heph-private-denylist"


class Finding(NamedTuple):
    """One denylist match in a text file."""

    path: Path
    line_number: int
    token: str


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def load_denylist(repo_root: Path) -> list[str]:
    """Return local denylist tokens, ignoring blank lines and comments."""
    path = repo_root / DENYLIST_FILENAME
    if not path.exists():
        return []
    tokens: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            tokens.append(token)
    return tokens


def tracked_files(repo_root: Path) -> list[Path]:
    """Return git-tracked files for manual scans."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [repo_root / line for line in result.stdout.splitlines() if line.strip()]


def _relative(repo_root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return path


def scan_paths(repo_root: Path, paths: list[Path], tokens: list[str]) -> list[Finding]:
    """Return denylist matches in text files under *paths*."""
    findings: list[Finding] = []
    if not tokens:
        return findings
    denylist_path = (repo_root / DENYLIST_FILENAME).resolve()
    for path in paths:
        candidate = path if path.is_absolute() else repo_root / path
        if candidate.resolve() == denylist_path or not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        rel_path = _relative(repo_root, candidate)
        for line_number, line in enumerate(lines, start=1):
            for token in tokens:
                if token in line:
                    findings.append(Finding(rel_path, line_number, token))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Fail (exit 1) if a scanned text file contains a local denylist token."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--help", "-h"):
        print(__doc__)
        return 0

    repo_root = get_repo_root()
    tokens = load_denylist(repo_root)
    if not tokens:
        return 0
    paths = [Path(arg) for arg in args] if args else tracked_files(repo_root)
    findings = scan_paths(repo_root, paths, tokens)
    if not findings:
        return 0

    print("ERROR: local private denylist token(s) found. Remove these values before committing:")
    for finding in findings:
        print(f"  {finding.path}:{finding.line_number}")
    print(f"\nDenylist source: {DENYLIST_FILENAME} (local, untracked)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
