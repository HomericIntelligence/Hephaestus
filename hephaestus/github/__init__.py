"""GitHub utilities for ProjectHephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.
"""

from .pr_merge import (
    detect_repo_from_remote,
    local_branch_exists,
    main as merge_prs,
)

__all__ = [
    "detect_repo_from_remote",
    "local_branch_exists",
    "merge_prs",
]
