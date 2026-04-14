"""YAML frontmatter extraction and validation for agent markdown files.

Provides generic utilities for working with ``---`` delimited YAML frontmatter
blocks in markdown files.  No agent-schema assumptions are baked in — callers
pass their own required/optional field specifications.

Usage::

    from hephaestus.agents.frontmatter import extract_frontmatter_parsed, validate_frontmatter

    parsed = extract_frontmatter_parsed(content)
    if parsed is not None:
        _raw, data = parsed
        errors = validate_frontmatter(data, required_fields={"name": str, "description": str})
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
except ModuleNotFoundError:
    _yaml = None  # type: ignore[assignment]

#: Compiled regex that matches a ``---`` frontmatter block at the start of a file.
FRONTMATTER_PATTERN: re.Pattern[str] = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_frontmatter_raw(content: str) -> str | None:
    """Extract the raw YAML frontmatter text from *content*.

    Args:
        content: Full markdown file content.

    Returns:
        The YAML block between the ``---`` markers, or ``None`` if not found.

    """
    match = FRONTMATTER_PATTERN.match(content)
    return match.group(1) if match else None


def extract_frontmatter_with_lines(
    content: str,
) -> tuple[str, int, int] | None:
    """Extract frontmatter with 1-indexed line numbers.

    Args:
        content: Full markdown file content.

    Returns:
        ``(frontmatter_text, start_line, end_line)`` or ``None`` if not found.
        *start_line* is always 1 (the opening ``---``).

    """
    match = FRONTMATTER_PATTERN.match(content)
    if match:
        frontmatter = match.group(1)
        end_line = content[: match.end()].count("\n")
        return (frontmatter, 1, end_line)
    return None


def extract_frontmatter_parsed(
    content: str,
) -> tuple[str, dict[str, Any]] | None:
    """Extract and parse frontmatter to a dict.

    Args:
        content: Full markdown file content.

    Returns:
        ``(raw_text, parsed_dict)`` or ``None`` if not found, not a mapping,
        or YAML is invalid.

    """
    if _yaml is None:
        return None
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return None
    frontmatter = match.group(1)
    try:
        parsed = _yaml.safe_load(frontmatter)
        if isinstance(parsed, dict):
            return (frontmatter, parsed)
    except _yaml.YAMLError:
        pass
    return None


def extract_frontmatter_full(
    content: str,
) -> tuple[str, dict[str, Any], int, int] | None:
    """Extract frontmatter with both parsed dict and line numbers.

    Args:
        content: Full markdown file content.

    Returns:
        ``(raw_text, parsed_dict, start_line, end_line)`` or ``None``.

    """
    if _yaml is None:
        return None
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return None
    frontmatter = match.group(1)
    try:
        parsed = _yaml.safe_load(frontmatter)
        if isinstance(parsed, dict):
            end_line = content[: match.end()].count("\n")
            return (frontmatter, parsed, 1, end_line)
    except _yaml.YAMLError:
        pass
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_frontmatter(
    frontmatter: dict[str, Any],
    required_fields: dict[str, type] | None = None,
    optional_fields: dict[str, type] | None = None,
) -> list[str]:
    """Validate frontmatter structure against required and optional field specs.

    Args:
        frontmatter: Parsed YAML frontmatter dict.
        required_fields: Mapping of field name → expected type that must be present.
            Defaults to ``{"name": str, "description": str, "tools": str, "model": str}``.
        optional_fields: Mapping of field name → expected type that may be present.
            Defaults to ``{"level": int, "section": str, "workflow_phase": str}``.

    Returns:
        List of error strings; empty means valid.

    """
    if required_fields is None:
        required_fields = {"name": str, "description": str, "tools": str, "model": str}
    if optional_fields is None:
        optional_fields = {"level": int, "section": str, "workflow_phase": str}

    errors: list[str] = []

    for field, expected_type in required_fields.items():
        if field not in frontmatter:
            errors.append(f"Missing required field: '{field}'")
        elif not isinstance(frontmatter[field], expected_type):
            actual = type(frontmatter[field]).__name__
            errors.append(f"Field '{field}' should be {expected_type.__name__}, got {actual}")

    for field, expected_type in optional_fields.items():
        if field in frontmatter and not isinstance(frontmatter[field], expected_type):
            actual = type(frontmatter[field]).__name__
            errors.append(f"Field '{field}' should be {expected_type.__name__}, got {actual}")

    return errors


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


def check_agent_file(
    file_path: Path,
    required_fields: dict[str, type] | None = None,
    optional_fields: dict[str, type] | None = None,
) -> tuple[bool, list[str]]:
    """Check a single agent markdown file for valid frontmatter.

    Args:
        file_path: Path to the agent markdown file.
        required_fields: Field spec passed to :func:`validate_frontmatter`.
        optional_fields: Field spec passed to :func:`validate_frontmatter`.

    Returns:
        ``(is_valid, error_messages)``

    """
    if _yaml is None:
        return False, ["pyyaml is required: pip install pyyaml"]

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, [f"Failed to read file: {exc}"]

    result = extract_frontmatter_with_lines(content)
    if result is None:
        return False, ["No YAML frontmatter found (should start with --- and end with ---)"]

    frontmatter_text, _start, _end = result

    try:
        frontmatter = _yaml.safe_load(frontmatter_text)
    except _yaml.YAMLError as exc:
        return False, [f"YAML syntax error: {exc}"]

    if frontmatter is None:
        return False, ["Empty frontmatter"]
    if not isinstance(frontmatter, dict):
        return False, [f"Frontmatter should be a YAML mapping, got {type(frontmatter).__name__}"]

    errors = validate_frontmatter(frontmatter, required_fields, optional_fields)
    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def validate_agents_main(argv: list[str] | None = None) -> int:
    """CLI entry point: validate agent frontmatter files in a directory.

    Args:
        argv: Argument list (default: ``sys.argv[1:]``).

    Returns:
        Exit code: 0 if all files are valid, 1 if any are invalid.

    """
    parser = argparse.ArgumentParser(
        description="Validate frontmatter in agent markdown files",
        epilog="Example: %(prog)s --agents-dir .claude/agents",
    )
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=None,
        help="Path to agents directory (default: <repo-root>/.claude/agents)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors",
    )
    args = parser.parse_args(argv)

    if args.agents_dir is not None:
        agents_dir = args.agents_dir
    else:
        from hephaestus.utils.helpers import get_repo_root

        agents_dir = get_repo_root() / ".claude" / "agents"

    if not agents_dir.is_dir():
        print(f"ERROR: agents directory not found: {agents_dir}", file=sys.stderr)
        return 1

    md_files = sorted(agents_dir.glob("*.md"))
    if not md_files:
        print(f"No agent files found in {agents_dir}", file=sys.stderr)
        return 1

    invalid_count = 0
    for file_path in md_files:
        is_valid, errors = check_agent_file(file_path)
        if is_valid:
            print(f"  OK  {file_path.name}")
        else:
            invalid_count += 1
            print(f"FAIL  {file_path.name}")
            for err in errors:
                print(f"      - {err}")

    total = len(md_files)
    print(f"\n{total - invalid_count}/{total} agents valid")

    return 0 if invalid_count == 0 else 1


if __name__ == "__main__":
    sys.exit(validate_agents_main())
