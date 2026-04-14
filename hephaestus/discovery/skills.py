"""Skill discovery and organisation utilities.

Generic filesystem walker for discovering skill directories and files,
optionally classifying them by category.  Category-to-skill mappings are
caller-supplied so no project-specific names are baked in.

Usage::

    from hephaestus.discovery.skills import discover_skills, get_skill_category

    skills = discover_skills(Path(".claude/skills"))
    # {"other": [Path(...), ...]}  — all skills in the "other" bucket

    # With custom mappings:
    mappings = {"github": ["gh-review-pr", "gh-create-pr-linked"]}
    skills = discover_skills(Path(".claude/skills"), category_mappings=mappings)
"""

from __future__ import annotations

import shutil
from pathlib import Path


def get_skill_category(
    skill_name: str,
    category_mappings: dict[str, list[str]] | None = None,
) -> str:
    """Determine the category for *skill_name*.

    First checks *category_mappings* for an explicit entry, then tries
    well-known prefix conventions (``gh-``, ``mojo-``, ``phase-``, etc.).
    Falls back to ``"other"`` when no match is found.

    Args:
        skill_name: Skill directory or file name (without ``.md`` extension).
        category_mappings: Optional ``{category: [skill_name, ...]}`` dict.

    Returns:
        Category string.

    """
    if category_mappings:
        for category, skills in category_mappings.items():
            if skill_name in skills:
                return category

    # Prefix-based fallback
    prefix_map: list[tuple[str, str]] = [
        ("gh-", "github"),
        ("mojo-", "mojo"),
        ("phase-", "workflow"),
        ("quality-", "quality"),
        ("worktree-", "worktree"),
        ("doc-", "documentation"),
        ("agent-", "agent"),
    ]
    for prefix, category in prefix_map:
        if skill_name.startswith(prefix):
            return category

    return "other"


def discover_skills(
    source_dir: Path,
    category_mappings: dict[str, list[str]] | None = None,
) -> dict[str, list[Path]]:
    """Scan *source_dir* and classify skills by category.

    Skill directories and ``*.md`` files (excluding templates) are discovered.

    Args:
        source_dir: Directory containing skill subdirectories and/or files.
        category_mappings: Optional explicit ``{category: [skill_name, ...]}``
            mapping passed through to :func:`get_skill_category`.

    Returns:
        Dict mapping category name to a sorted list of skill paths.
        Always includes an ``"other"`` key.

    """
    skill_dirs = sorted(
        d for d in source_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    skill_files = sorted(
        f
        for f in source_dir.iterdir()
        if f.is_file() and f.suffix == ".md" and "TEMPLATE" not in f.name
    )
    all_skills = skill_dirs + skill_files

    categories: set[str] = {"other"}
    if category_mappings:
        categories |= set(category_mappings.keys())

    result: dict[str, list[Path]] = {cat: [] for cat in sorted(categories)}

    for skill_path in all_skills:
        skill_name = skill_path.stem if skill_path.is_file() else skill_path.name
        category = get_skill_category(skill_name, category_mappings)
        if category not in result:
            result[category] = []
        result[category].append(skill_path)

    return result


def organize_skills(
    source_dir: Path,
    dest_dir: Path,
    category_mappings: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Copy skills from *source_dir* into *dest_dir*, organised by category.

    Creates a subdirectory per category under *dest_dir* and copies each
    skill directory or file into it.

    Args:
        source_dir: Directory containing skill subdirs/files.
        dest_dir: Destination root directory; category subdirs are created here.
        category_mappings: Optional explicit mapping for :func:`get_skill_category`.

    Returns:
        Dict mapping category to list of organised skill names.

    """
    skills_by_category = discover_skills(source_dir, category_mappings)

    for category in skills_by_category:
        (dest_dir / category).mkdir(parents=True, exist_ok=True)

    stats: dict[str, list[str]] = {cat: [] for cat in skills_by_category}
    for category, skill_paths in skills_by_category.items():
        for skill_path in skill_paths:
            skill_name = skill_path.stem if skill_path.is_file() else skill_path.name
            dest_path = dest_dir / category / skill_name
            if skill_path.is_dir():
                if dest_path.exists():
                    shutil.rmtree(dest_path)
                shutil.copytree(skill_path, dest_path)
            else:
                shutil.copy2(skill_path, dest_path.with_suffix(skill_path.suffix))
            stats[category].append(skill_name)

    return stats
