"""GitHub utilities for ProjectHephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.
"""

from .pr_merge import (
    detect_repo_from_remote,
    local_branch_exists,
)
from .pr_merge import (
    main as merge_prs,
)
from .rate_limit import (
    detect_rate_limit,
    parse_reset_epoch,
    wait_until,
)

__all__ = [
    "detect_rate_limit",
    "detect_repo_from_remote",
    "local_branch_exists",
    "merge_prs",
    "parse_reset_epoch",
    "wait_until",
]
