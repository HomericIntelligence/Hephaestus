"""Skill definition validators.

Merges the skill-catalog, repo-analyze-skills, and skill-merge-method checks
into one focused subpackage. The original ``hephaestus.validation.<module>``
paths remain as thin re-export shims for backward compatibility.
"""

from hephaestus.validation.skills.repo_analyze_skills import (
    COMMON_DIR as COMMON_DIR,
    REPO_ROOT as REPO_ROOT,
    SKILLS_DIR as SKILLS_DIR,
)
from hephaestus.validation.skills.skill_catalog import (
    check_skill_catalog as check_skill_catalog,
    check_skill_frontmatter as check_skill_frontmatter,
    extract_skill_table_rows as extract_skill_table_rows,
)
from hephaestus.validation.skills.skill_merge_method import (
    FENCE as FENCE,
    HARDCODED as HARDCODED,
    MARKER as MARKER,
    scan as scan,
)

__all__ = [
    "COMMON_DIR",
    "FENCE",
    "HARDCODED",
    "MARKER",
    "REPO_ROOT",
    "SKILLS_DIR",
    "check_skill_catalog",
    "check_skill_frontmatter",
    "extract_skill_table_rows",
    "scan",
]
