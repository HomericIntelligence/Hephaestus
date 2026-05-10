#!/usr/bin/env python3
"""Check that README.md CLI table is in sync with pyproject.toml [project.scripts].

This script is wired into pre-commit (see .pre-commit-config.yaml) and runs
whenever README.md or pyproject.toml changes.  It succeeds silently when the
README mentions every command declared in [project.scripts] and exits non-zero
with a clear diff otherwise.

Usage:
    python3 scripts/check_cli_table_sync.py

Exit codes:
    0  All pyproject.toml scripts are documented in README.md.
    1  One or more scripts are missing from the README CLI section.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover — only on Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef, unused-ignore]
    except ModuleNotFoundError:
        print(
            "ERROR: tomllib (stdlib, Python 3.11+) or tomli (pip install tomli) required.",
            file=sys.stderr,
        )
        sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"

# Regex that matches a backtick-quoted command name anywhere in the README.
_BACKTICK_CMD_RE = re.compile(r"`(hephaestus-[a-z0-9-]+)`")


def _load_scripts() -> set[str]:
    """Return the set of command names from pyproject.toml [project.scripts]."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("project", {}).get("scripts", {})
    return set(scripts.keys())


def _readme_documented_commands() -> set[str]:
    """Return all ``hephaestus-*`` commands mentioned in README.md."""
    readme_text = README.read_text(encoding="utf-8")
    return set(_BACKTICK_CMD_RE.findall(readme_text))


def main() -> int:
    """Run the sync check and print a diff if out of sync.

    Returns:
        0 if in sync, 1 if out of sync.

    """
    declared = _load_scripts()
    documented = _readme_documented_commands()

    missing = sorted(declared - documented)
    extra = sorted(documented - declared)

    ok = True

    if missing:
        ok = False
        print("ERROR: The following commands are declared in pyproject.toml but NOT documented")
        print("       in README.md.  Add them to the CLI Commands section and run this script")
        print("       to verify.\n")
        for cmd in missing:
            print(f"  - {cmd}")
        print()

    if extra:
        # Extra entries are not an error — they could appear in prose / examples
        # that postdate a removal.  Just warn.
        print("WARNING: The following commands appear in README.md but NOT in pyproject.toml.")
        print("         They may be stale; consider removing them.\n")
        for cmd in extra:
            print(f"  - {cmd}")
        print()

    if ok:
        print(f"OK: all {len(declared)} pyproject.toml scripts are documented in README.md.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
