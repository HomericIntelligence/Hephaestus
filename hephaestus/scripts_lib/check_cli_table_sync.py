#!/usr/bin/env python3
"""Check that documentation reflects every packaged console script.

This script is wired into pre-commit (see .pre-commit-config.yaml) and runs
whenever a guarded documentation surface or pyproject.toml changes. It verifies
that every declared command has a README reference, that the three documented
source-derived script counts agree with pyproject.toml, and that every
backticked ``hephaestus-*`` reference in the guarded docs names a registered
console script. Accepted ADR bodies retain their historical command names.
Draft decisions and current documentation remain in the reference check.

Usage:
    python3 -m hephaestus.scripts_lib.check_cli_table_sync

Exit codes:
    0  The documented console-script inventory matches pyproject.toml.
    1  A documented command or source-derived count is out of sync.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hephaestus.io.toml import import_tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"

# Regex that matches a backtick-quoted command name in a guarded document.
_BACKTICK_CMD_RE = re.compile(r"`(hephaestus-[a-z0-9-]+)`")

# Regexes for source-derived count prose in the public documentation.
_CONSOLE_SCRIPT_PROSE_RE = re.compile(r"(\d+)\s+console scripts")
_DOCS_INDEX_PROSE_RE = re.compile(r"(\d+)\+?\s+CLI entry points")

_PROSE_COUNT_CHECKS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("README.md", _CONSOLE_SCRIPT_PROSE_RE, "console scripts"),
    ("COMPATIBILITY.md", _CONSOLE_SCRIPT_PROSE_RE, "console scripts"),
    ("docs/index.md", _DOCS_INDEX_PROSE_RE, "CLI entry points"),
)

_DOC_SCAN_GLOBS = ("README.md", "COMPATIBILITY.md", "AGENTS.md", "docs/**/*.md")
_ADR_PATH_RE = re.compile(r"docs/adr/[0-9]{4}-[a-z0-9-]+\.md")
_ADR_STATUS_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*Status\*\*\s*:|\*\*Status:\*\*|Status\s*:)"
    r"\s*(?P<status>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ACCEPTED_ADR_STATUS_RE = re.compile(
    r"Accepted(?:\s*\([^\r\n)]+\))?",
    re.IGNORECASE,
)
_MARKDOWN_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)


def has_accepted_adr_status(content: str) -> bool:
    """Return whether all status fields in the document header declare acceptance."""
    first_section = _MARKDOWN_SECTION_RE.search(content)
    metadata = content[: first_section.start()] if first_section else content
    statuses = [
        match.group("status").strip().strip("*_` ") for match in _ADR_STATUS_RE.finditer(metadata)
    ]
    return bool(statuses) and all(
        _ACCEPTED_ADR_STATUS_RE.fullmatch(status) is not None for status in statuses
    )


def _load_scripts(repo_root: Path | None = None) -> set[str]:
    """Return the set of command names from pyproject.toml [project.scripts]."""
    pyproject = (repo_root / "pyproject.toml") if repo_root is not None else PYPROJECT
    tomllib = import_tomllib()
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("project", {}).get("scripts", {})
    return set(scripts)


def _readme_documented_commands(repo_root: Path | None = None) -> set[str]:
    """Return all ``hephaestus-*`` commands mentioned in README.md."""
    readme = (repo_root / "README.md") if repo_root is not None else README
    readme_text = readme.read_text(encoding="utf-8")
    return set(_BACKTICK_CMD_RE.findall(readme_text))


def check_prose_counts(repo_root: Path, expected_count: int) -> tuple[bool, list[str]]:
    """Return whether each source-derived documentation count is current."""
    mismatches: list[str] = []

    for relative_path, pattern, count_label in _PROSE_COUNT_CHECKS:
        path = repo_root / relative_path
        if not path.is_file():
            mismatches.append(f"{relative_path} not found at {path}")
            continue

        match = pattern.search(path.read_text(encoding="utf-8"))
        if match is None:
            mismatches.append(
                f"{relative_path}: missing prose sentence matching r'{pattern.pattern}'"
            )
            continue

        actual = int(match.group(1))
        if actual != expected_count:
            mismatches.append(
                f"{relative_path}: prose says '{actual} {count_label}' but "
                f"pyproject.toml [project.scripts] has {expected_count} entries"
            )

    return (not mismatches, mismatches)


def check_docs_command_references(repo_root: Path, declared: set[str]) -> list[str]:
    """Return guarded docs references that do not name registered commands."""
    problems: list[str] = []

    for pattern in _DOC_SCAN_GLOBS:
        for path in sorted(repo_root.glob(pattern)):
            relative_path = path.relative_to(repo_root).as_posix()
            content = path.read_text(encoding="utf-8")
            if _ADR_PATH_RE.fullmatch(relative_path) and has_accepted_adr_status(content):
                continue
            commands = set(_BACKTICK_CMD_RE.findall(content))
            for command in sorted(commands - declared):
                problems.append(
                    f"{relative_path}: references `{command}` "
                    "which is not in pyproject.toml [project.scripts]"
                )

    return problems


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

    prose_ok, prose_mismatches = check_prose_counts(REPO_ROOT, len(declared))
    if not prose_ok:
        ok = False
        print("ERROR: Prose counts disagree with pyproject.toml [project.scripts]:\n")
        for mismatch in prose_mismatches:
            print(f"  - {mismatch}")
        print()

    reference_problems = check_docs_command_references(REPO_ROOT, declared)
    if reference_problems:
        ok = False
        print("ERROR: Documentation references unregistered console scripts:\n")
        for problem in reference_problems:
            print(f"  - {problem}")
        print()

    if ok:
        print(
            f"OK: all {len(declared)} pyproject.toml scripts are documented, "
            "source-derived counts agree, and guarded docs reference registered commands."
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
