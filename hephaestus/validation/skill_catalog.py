"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.skills.skill_catalog`.
Retained so existing import sites and the ``hephaestus-check-skill-catalog``
console script need no changes.
"""

import sys

from hephaestus.validation.skills.skill_catalog import (
    check_claude_skill_arguments as check_claude_skill_arguments,
    check_skill_catalog as check_skill_catalog,
    check_skill_frontmatter as check_skill_frontmatter,
    extract_claude_skill_arguments as extract_claude_skill_arguments,
    extract_skill_table_rows as extract_skill_table_rows,
    main as main,
)

__all__ = [
    "check_claude_skill_arguments",
    "check_skill_catalog",
    "check_skill_frontmatter",
    "extract_claude_skill_arguments",
    "extract_skill_table_rows",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
