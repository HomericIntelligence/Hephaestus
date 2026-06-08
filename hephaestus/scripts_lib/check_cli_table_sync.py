#!/usr/bin/env python3
"""Check that README.md CLI table is in sync with pyproject.toml [project.scripts].

This script is wired into pre-commit (see .pre-commit-config.yaml) and runs
whenever README.md or pyproject.toml changes.  It succeeds silently when the
README mentions every command declared in [project.scripts] and exits non-zero
with a clear diff otherwise.

Beyond verifying that every command name appears in README.md, the script also
verifies the prose counts in ``README.md`` (e.g. "44 console scripts") and in
``docs/index.md`` (e.g. "44 CLI entry points") agree with the actual length of
``[project.scripts]``.  This prevents documentation drift like #857, where
README.md claimed 42 and docs/index.md claimed 37+ even though the real count
was 44.

Usage:
    python3 scripts/check_cli_table_sync.py

Exit codes:
    0  All pyproject.toml scripts are documented in README.md AND the prose
       counts in README.md / docs/index.md match the actual count.
    1  One or more scripts are missing from the README CLI section OR a prose
       count is wrong.
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
DOCS_INDEX = REPO_ROOT / "docs" / "index.md"

# Regex that matches a backtick-quoted command name anywhere in the README.
_BACKTICK_CMD_RE = re.compile(r"`(hephaestus-[a-z0-9-]+)`")

# Regex that matches the README prose "<N> console scripts".
_README_PROSE_RE = re.compile(r"(\d+)\s+console scripts")

# Regex that matches the docs/index.md prose "<N>+? CLI entry points".  The
# optional ``+`` is intentionally allowed in the pattern so legacy text like
# "37+" parses cleanly, but the check below treats the bare integer as
# authoritative.
_DOCS_INDEX_PROSE_RE = re.compile(r"(\d+)\+?\s+CLI entry points")


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


def check_prose_counts(repo_root: Path, expected_count: int) -> tuple[bool, list[str]]:
    """Verify the prose counts in README.md and docs/index.md match expected_count.

    The check runs the regex against the full file text and treats a missing
    match as a mismatch — silently dropping the prose sentence would erode the
    guard rail that this checker exists to provide.

    Args:
        repo_root: Repository root to search under.  Used so tests can point at
            a temporary scratch directory.
        expected_count: The authoritative count of ``[project.scripts]`` keys.

    Returns:
        ``(ok, mismatches)``.  ``ok`` is ``True`` iff every prose count matches
        ``expected_count``.  ``mismatches`` is a list of human-readable strings,
        one per offending file, that explain what was found vs. what was
        expected.  An empty list means everything passed.

    """
    mismatches: list[str] = []

    readme = repo_root / "README.md"
    docs_index = repo_root / "docs" / "index.md"

    if not readme.is_file():
        mismatches.append(f"README.md not found at {readme}")
    else:
        readme_text = readme.read_text(encoding="utf-8")
        match = _README_PROSE_RE.search(readme_text)
        if match is None:
            mismatches.append(
                "README.md: missing prose sentence matching "
                "r'(\\d+)\\s+console scripts' — has the wording changed?"
            )
        else:
            actual = int(match.group(1))
            if actual != expected_count:
                mismatches.append(
                    f"README.md: prose says '{actual} console scripts' but "
                    f"pyproject.toml [project.scripts] has {expected_count} entries"
                )

    if not docs_index.is_file():
        mismatches.append(f"docs/index.md not found at {docs_index}")
    else:
        docs_text = docs_index.read_text(encoding="utf-8")
        match = _DOCS_INDEX_PROSE_RE.search(docs_text)
        if match is None:
            mismatches.append(
                "docs/index.md: missing prose sentence matching "
                "r'(\\d+)\\+?\\s+CLI entry points' — has the wording changed?"
            )
        else:
            actual = int(match.group(1))
            if actual != expected_count:
                mismatches.append(
                    f"docs/index.md: prose says '{actual} CLI entry points' but "
                    f"pyproject.toml [project.scripts] has {expected_count} entries"
                )

    return (len(mismatches) == 0, mismatches)


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

    prose_ok, prose_mismatches = check_prose_counts(REPO_ROOT, len(declared))
    if not prose_ok:
        ok = False
        print("ERROR: Prose counts disagree with pyproject.toml [project.scripts]:\n")
        for line in prose_mismatches:
            print(f"  - {line}")
        print()

    if ok:
        print(
            f"OK: all {len(declared)} pyproject.toml scripts are documented in README.md, "
            "and prose counts in README.md and docs/index.md agree."
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
