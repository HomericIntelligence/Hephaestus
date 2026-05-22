"""GitHub utilities for ProjectHephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.

The CLI entry points for this subpackage (``hephaestus-merge-prs``,
``hephaestus-fleet-sync``, ``hephaestus-tidy``) are intentionally NOT exported
here: they are ``argparse``-driven ``main()`` functions that call ``sys.exit()``
and are not safe for programmatic use. Run them as console scripts, or import
them directly from their submodules (e.g. ``hephaestus.github.pr_merge:main``).
"""

from hephaestus.github.pr_merge import detect_repo_from_remote, local_branch_exists
from hephaestus.github.rate_limit import (
    detect_claude_usage_cap,
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

__all__ = [
    "collect_stats",
    "detect_claude_usage_cap",
    "detect_claude_usage_limit",
    "detect_rate_limit",
    "detect_repo_from_remote",
    "format_stats_table",
    "get_commits_stats",
    "get_current_repo",
    "get_issues_stats",
    "get_prs_stats",
    "local_branch_exists",
    "parse_reset_epoch",
    "validate_date",
    "wait_until",
]
