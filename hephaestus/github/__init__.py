"""GitHub utilities for ProjectHephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.
"""

from hephaestus.github.fleet_sync import main as fleet_sync
from hephaestus.github.pr_merge import detect_repo_from_remote, local_branch_exists
from hephaestus.github.pr_merge import main as merge_prs
from hephaestus.github.rate_limit import (
    detect_claude_usage_limit,
    detect_rate_limit,
    parse_reset_epoch,
    wait_until,
)
from hephaestus.github.stats import (
    collect_stats,
    format_stats_table,
    get_commits_stats,
    get_current_repo,
    get_issues_stats,
    get_prs_stats,
    validate_date,
)
from hephaestus.github.tidy import main as tidy

__all__ = [
    "collect_stats",
    "detect_claude_usage_limit",
    "detect_rate_limit",
    "detect_repo_from_remote",
    "fleet_sync",
    "format_stats_table",
    "get_commits_stats",
    "get_current_repo",
    "get_issues_stats",
    "get_prs_stats",
    "local_branch_exists",
    "merge_prs",
    "parse_reset_epoch",
    "tidy",
    "validate_date",
    "wait_until",
]
