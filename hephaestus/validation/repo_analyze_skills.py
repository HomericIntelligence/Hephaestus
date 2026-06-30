"""Backward-compatibility shim.

Canonical implementation:
:mod:`hephaestus.validation.skills.repo_analyze_skills`.
Retained so existing import sites and the
``hephaestus-check-repo-analyze-skills`` console script need no changes.
"""

import sys

from hephaestus.validation.skills.repo_analyze_skills import (
    COMMON_DIR as COMMON_DIR,
    REPO_ROOT as REPO_ROOT,
    SKILLS_DIR as SKILLS_DIR,
    main as main,
)

__all__ = [
    "COMMON_DIR",
    "REPO_ROOT",
    "SKILLS_DIR",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
