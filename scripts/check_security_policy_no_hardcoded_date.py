#!/usr/bin/env python3
"""Reject hard-coded ``As of YYYY-MM-DD`` stamps in SECURITY.md.

Hard-coded absolute dates in a rolling security policy rot — see issue #730 —
because nothing forces them to be re-stamped. This check fails the pre-commit
hook whenever such a stamp reappears.

Usage:
    python scripts/check_security_policy_no_hardcoded_date.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HARDCODED_DATE_RE = re.compile(r"As of \d{4}-\d{2}-\d{2}")


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def find_hardcoded_dates(security_md: Path) -> list[tuple[int, str]]:
    """Return (line_number, line) pairs containing an ``As of YYYY-MM-DD`` stamp."""
    if not security_md.exists():
        return []
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(security_md.read_text(encoding="utf-8").splitlines(), start=1):
        if HARDCODED_DATE_RE.search(line):
            hits.append((lineno, line))
    return hits


def main() -> int:
    """Scan SECURITY.md and exit non-zero if a hard-coded date stamp is found."""
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        return 0
    repo_root = get_repo_root()
    security_md = repo_root / "SECURITY.md"
    hits = find_hardcoded_dates(security_md)
    if hits:
        print("ERROR: SECURITY.md contains hard-coded 'As of YYYY-MM-DD' stamps:")
        for lineno, line in hits:
            print(f"  SECURITY.md:{lineno}: {line.strip()}")
        print(
            "\nReplace the date with rolling policy language that remains true "
            "across releases. See issue #730."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
