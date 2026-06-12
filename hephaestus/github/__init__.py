"""GitHub utilities for ProjectHephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.

Public adapter contract
-----------------------
``gh_call`` is the canonical entry point for invoking the ``gh`` CLI
anywhere in hephaestus. It wraps every call in the ``github-api`` circuit
breaker (opens after 5 sustained failures, fail-fast for 60s), enforces
per-thread throttling via ``GH_RATE_LIMIT_PER_SEC``, detects REST and
GraphQL rate limits and waits until reset, and translates Claude
per-period usage caps into ``ClaudeUsageCapError``.

Bare ``subprocess.run(["gh", ...])`` calls bypass the breaker and are a
reliability bug — route them through ``gh_call``.

Exception hierarchy::

    RuntimeError
    ├── GitHubRateLimitError      (rate limit; carries reset_epoch)
    ├── GitHubUnavailableError    (breaker open; sustained outage)
    └── ClaudeUsageCapError       (Claude per-period cap)

Out of scope for ``gh_call``: ``pr_merge`` uses the PyGithub object API
(not the gh CLI); ``tidy``'s interactive ``gh tidy`` Popen requires direct
stdin/stdout access.

CLI entry points (``hephaestus-merge-prs``, ``hephaestus-fleet-sync``,
``hephaestus-tidy``) are intentionally NOT exported here: they are
``argparse``-driven ``main()`` functions that call ``sys.exit()`` and are
not safe for programmatic use. Run them as console scripts, or import them
directly from their submodules (e.g. ``hephaestus.github.pr_merge:main``).
"""

from hephaestus.github.client import (
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call,
)
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
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
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
    "gh_call",
    "local_branch_exists",
    "parse_reset_epoch",
    "validate_date",
    "wait_until",
]
