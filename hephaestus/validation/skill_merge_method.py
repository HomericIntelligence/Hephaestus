#!/usr/bin/env python3
"""Lint: skill files must not hardcode the gh pr merge method.

Scans skills/*/SKILL.md for the pattern
    gh pr merge ... --auto --(rebase|squash|merge)
and fails on any match unless it is inside a fenced code block whose
preceding non-blank line is exactly the marker
    <!-- merge-method-allowed: example -->

Usage: python -m hephaestus.validation.skill_merge_method
"""

import re
import sys
from pathlib import Path

HARDCODED = re.compile(r"gh\s+pr\s+merge\b[^\n]*--auto\b[^\n]*--(rebase|squash|merge)\b")
MARKER = "<!-- merge-method-allowed: example -->"
FENCE = re.compile(r"^\s*```")
# This module lives at hephaestus/validation/, so the repo root is two parents up.
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


def _block_is_marked(lines: list[str], idx: int) -> bool:
    """Return True if the fenced block containing 1-indexed line ``idx`` is marked."""
    fence_open = None
    for i in range(idx - 1, -1, -1):
        if FENCE.match(lines[i]):
            fence_open = i
            break
    if fence_open is None:
        return False
    for j in range(fence_open - 1, -1, -1):
        if lines[j].strip() == "":
            continue
        return lines[j].strip() == MARKER
    return False


def scan(root: Path) -> list[tuple[Path, int, str]]:
    """Scan skill files for hardcoded gh pr merge methods.

    Args:
        root: Repository root directory

    Returns:
        List of (path, line_number, line_text) tuples for violations

    """
    findings: list[tuple[Path, int, str]] = []
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            if not HARDCODED.search(line):
                continue
            if _block_is_marked(lines, idx):
                continue
            findings.append((path, idx, line.rstrip()))
    return findings


def main() -> int:
    """Entry point for skill merge method linter.

    Returns:
        0 if no violations found, 1 otherwise

    """
    repo_root = _DEFAULT_REPO_ROOT
    findings = scan(repo_root)
    if not findings:
        return 0
    for path, lineno, text in findings:
        rel = path.relative_to(repo_root)
        print(f"{rel}:{lineno}: hardcoded gh pr merge method — use choose_merge_flag")
        print(f"    {text}")
    print(
        "\nReplace with the choose_merge_flag snippet (see "
        "skills/finish-branch/SKILL.md) or mark an instructional example "
        f"with '{MARKER}' on the line above its fence."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
