#!/usr/bin/env python3
"""Check that README.md documents every packaged console script.

This script is wired into pre-commit (see .pre-commit-config.yaml) and runs
whenever README.md or pyproject.toml changes. It succeeds when the README
mentions every command declared in [project.scripts] and exits non-zero with a
clear diff otherwise. It deliberately does not enforce prose counts or turn
the README into a generated full catalog: both are editorial material without
a source-derived update mechanism.

Usage:
    python3 -m hephaestus.scripts_lib.check_cli_table_sync

Exit codes:
    0  All pyproject.toml scripts are documented in README.md.
    1  One or more scripts are missing from README.md.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path


def _get_tomllib() -> types.ModuleType:
    """Return the ``tomllib`` module, falling back to ``tomli`` on Python 3.10.

    Raises:
        RuntimeError: When neither ``tomllib`` nor ``tomli`` is importable.

    """
    # WHY justified: tomllib is stdlib only on Python 3.11+; on 3.10 we fall
    # back to the `tomli` backport. [no-any-return] — the imported module object
    # is typed Any; [no-redef] — `tomli as tomllib` rebinds the same name.
    try:
        import tomllib  # Python 3.11+

        return tomllib  # type: ignore[no-any-return]
    except ModuleNotFoundError:  # pragma: no cover — only on Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef, unused-ignore]

            return tomllib  # type: ignore[no-any-return]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "tomllib (stdlib, Python 3.11+) or tomli (pip install tomli) required."
            ) from exc


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"

# Regex that matches a backtick-quoted command name anywhere in the README.
_BACKTICK_CMD_RE = re.compile(r"`(hephaestus-[a-z0-9-]+)`")


def _load_scripts(repo_root: Path | None = None) -> set[str]:
    """Return the set of command names from pyproject.toml [project.scripts]."""
    tomllib = _get_tomllib()
    pyproject = (repo_root / "pyproject.toml") if repo_root is not None else PYPROJECT
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("project", {}).get("scripts", {})
    return set(scripts.keys())


def _readme_documented_commands(repo_root: Path | None = None) -> set[str]:
    """Return all ``hephaestus-*`` commands mentioned in README.md."""
    readme = (repo_root / "README.md") if repo_root is not None else README
    readme_text = readme.read_text(encoding="utf-8")
    return set(_BACKTICK_CMD_RE.findall(readme_text))


def main() -> int:
    """Run the sync check and print a diff if out of sync.

    Returns:
        0 if in sync, 1 if out of sync.

    """
    try:
        declared = _load_scripts()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    documented = _readme_documented_commands()

    missing = sorted(declared - documented)

    ok = True

    if missing:
        ok = False
        print("ERROR: The following commands are declared in pyproject.toml but NOT documented")
        print("       in README.md.  Add them to the CLI Commands section and run this script")
        print("       to verify.\n")
        for cmd in missing:
            print(f"  - {cmd}")
        print()

    if ok:
        print(f"OK: all {len(declared)} pyproject.toml scripts are documented in README.md.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
