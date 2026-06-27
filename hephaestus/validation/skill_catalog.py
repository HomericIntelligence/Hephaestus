r"""Validate that docs/plugin-installation.md lists every shipped skill.

Reads the markdown table under the "What the Plugin Provides" section and
asserts each row matches a skill discovered by
:func:`hephaestus.discovery.skills.discover_skills`. It also validates each
shipped skill has loadable YAML frontmatter with a name matching its
directory.

The check is wired into pre-commit (see ``.pre-commit-config.yaml``) and runs
whenever ``docs/plugin-installation.md`` or anything under ``skills/`` changes,
preventing silent drift between the shipped skill set and its catalog.

Usage::

    hephaestus-check-skill-catalog
    hephaestus-check-skill-catalog --table docs/plugin-installation.md \
                                   --skills-dir skills/
    hephaestus-check-skill-catalog --json

Exit codes:
    0: Table lists every shipped skill and no extras.
    1: Mismatch — table is missing skills, lists removed skills, or a skill has invalid frontmatter.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hephaestus.agents.frontmatter import check_agent_file, extract_frontmatter_parsed
from hephaestus.cli.utils import create_validation_parser, emit_json_status, resolve_repo_root
from hephaestus.discovery.skills import discover_skills

# Matches a markdown table row of the form ``| cell | cell | ... |``. We pull
# out the leftmost non-empty cell as the skill name. Table header and the
# ``|---|---|`` divider rows are filtered out by checking for a dash-only
# leftmost cell.
_TABLE_ROW_RE = re.compile(r"^\s*\|([^|]+)\|")


def extract_skill_table_rows(markdown_path: Path) -> set[str]:
    """Parse the first markdown table in *markdown_path* and return skill names.

    A "skill name" is the content of the leftmost column for each data row.
    The header row, the divider row, and any rows whose leftmost cell is the
    literal string ``Skill`` (case-insensitive) are skipped.

    Args:
        markdown_path: Path to the markdown file containing the table.

    Returns:
        Set of skill names in the leftmost column of the first table.

    """
    if not markdown_path.exists():
        return set()

    content = markdown_path.read_text(encoding="utf-8")
    rows: set[str] = set()
    in_table = False

    for raw_line in content.splitlines():
        match = _TABLE_ROW_RE.match(raw_line)
        if not match:
            # First non-table line after we entered a table ends the parse —
            # we only consult the first table in the file.
            if in_table:
                break
            continue

        cell = match.group(1).strip()

        # Skip divider rows like ``|---|---|`` or ``| :--- |``.
        if not cell or set(cell.replace(":", "").strip()) <= {"-"}:
            in_table = True
            continue

        # Skip the header row.
        if cell.lower() == "skill":
            in_table = True
            continue

        # Strip backticks the docs sometimes wrap skill names in.
        cell = cell.strip("`").strip()
        if cell:
            in_table = True
            rows.add(cell)

    return rows


def _discover_skill_names(skills_dir: Path) -> set[str]:
    """Return the set of skill names shipped under *skills_dir*.

    Each plugin skill is a directory containing a ``SKILL.md`` file. We use
    :func:`hephaestus.discovery.skills.discover_skills` for traversal, then
    keep only the entries that resolve to such a directory — sibling files
    such as ``THIRD_PARTY_LICENSES.md`` are not skills and must be ignored.
    """
    if not skills_dir.exists():
        return set()
    discovered = discover_skills(skills_dir)
    names: set[str] = set()
    for paths in discovered.values():
        for path in paths:
            if path.is_dir() and (path / "SKILL.md").exists():
                names.add(path.name)
    return names


def check_skill_catalog(table_path: Path, skills_dir: Path) -> tuple[set[str], set[str]]:
    """Compare the catalog table with the discovered skills directory.

    Args:
        table_path: Path to ``docs/plugin-installation.md`` (or similar).
        skills_dir: Path to the ``skills/`` directory.

    Returns:
        ``(missing_in_table, extra_in_table)`` — sets of skill names that are
        shipped but undocumented, and documented but no longer shipped.

    """
    documented = extract_skill_table_rows(table_path)
    shipped = _discover_skill_names(skills_dir)
    missing = shipped - documented
    extra = documented - shipped
    return missing, extra


def check_skill_frontmatter(skills_dir: Path) -> dict[str, list[str]]:
    """Validate each shipped skill has loadable plugin metadata.

    Args:
        skills_dir: Path to the ``skills/`` directory.

    Returns:
        Mapping of skill directory name to validation errors. Empty means all
        shipped skills have valid frontmatter.

    """
    if not skills_dir.exists():
        return {}

    errors_by_skill: dict[str, list[str]] = {}
    required_fields: dict[str, type] = {"name": str, "description": str}
    optional_fields: dict[str, type] = {"argument-hint": str, "allowed-tools": list}

    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_file.parent.name
        is_valid, errors = check_agent_file(
            skill_file,
            required_fields=required_fields,
            optional_fields=optional_fields,
        )
        skill_errors = list(errors)

        if is_valid:
            parsed = extract_frontmatter_parsed(skill_file.read_text(encoding="utf-8"))
            if parsed is None:
                skill_errors.append("No parseable YAML frontmatter found")
            else:
                _raw, frontmatter = parsed
                frontmatter_name = frontmatter.get("name")
                if frontmatter_name != skill_name:
                    skill_errors.append(
                        f"Frontmatter name {frontmatter_name!r} must match directory {skill_name!r}"
                    )
                description = frontmatter.get("description", "")
                if isinstance(description, str) and not description.strip():
                    skill_errors.append("Field 'description' must not be empty")

        if skill_errors:
            errors_by_skill[skill_name] = skill_errors

    return errors_by_skill


def _format_diff(missing: set[str], extra: set[str]) -> str:
    """Format the missing/extra diff as a human-readable report."""
    lines: list[str] = []
    if missing:
        lines.append("Missing from catalog (shipped but not documented):")
        for name in sorted(missing):
            lines.append(f"  - {name}")
    if extra:
        if lines:
            lines.append("")
        lines.append("Extra in catalog (documented but not shipped):")
        for name in sorted(extra):
            lines.append(f"  - {name}")
    return "\n".join(lines)


def _format_frontmatter_errors(errors_by_skill: dict[str, list[str]]) -> str:
    """Format skill frontmatter validation errors for text output."""
    lines: list[str] = []
    if errors_by_skill:
        lines.append("Invalid skill frontmatter:")
        for name in sorted(errors_by_skill):
            lines.append(f"  - {name}:")
            for error in errors_by_skill[name]:
                lines.append(f"      - {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the skill-catalog sync check.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code (0 on match, 1 on mismatch).

    """
    parser = create_validation_parser(
        "Verify docs/plugin-installation.md lists every shipped skill.",
        prog="hephaestus-check-skill-catalog",
    )
    parser.add_argument(
        "--table",
        type=Path,
        default=None,
        help="Path to the catalog markdown (default: docs/plugin-installation.md)",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Path to the skills directory (default: skills/)",
    )
    args = parser.parse_args(argv)

    repo_root: Path = resolve_repo_root(args)
    table_path: Path = args.table or (repo_root / "docs" / "plugin-installation.md")
    skills_dir: Path = args.skills_dir or (repo_root / "skills")

    missing, extra = check_skill_catalog(table_path, skills_dir)
    frontmatter_errors = check_skill_frontmatter(skills_dir)
    ok = not missing and not extra and not frontmatter_errors
    exit_code = 0 if ok else 1

    if args.json:
        emit_json_status(
            exit_code,
            message=("skill catalog is in sync" if ok else "skill catalog is out of sync"),
            missing=sorted(missing),
            extra=sorted(extra),
            frontmatter_errors=frontmatter_errors,
            table=str(table_path),
            skills_dir=str(skills_dir),
        )
    elif ok:
        print(
            f"OK: skill catalog matches {len(_discover_skill_names(skills_dir))} shipped skill(s)."
        )
    else:
        print("ERROR: docs/plugin-installation.md is out of sync with skills/.")
        print()
        diff = _format_diff(missing, extra)
        frontmatter_report = _format_frontmatter_errors(frontmatter_errors)
        if diff:
            print(diff)
            print()
        if frontmatter_report:
            print(frontmatter_report)
            print()
        print(
            "Fix by updating the catalog table, removing the deleted skill, "
            "or correcting skill frontmatter."
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
