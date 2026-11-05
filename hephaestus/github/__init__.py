"""GitHub utilities for Hephaestus.

Provides utilities for working with GitHub repositories, PRs, and automation.

The ``gh`` CLI adapter (``gh_call``, the rate-limit utilities, and the
exception hierarchy) moved to :mod:`hephaestus.git.gh`; see its module
docstring for the public adapter contract. The interactive ``gh tidy``
Popen bypass stays with ``tidy`` in this package until W4.

Run ``hephaestus-fleet-sync`` and ``hephaestus-tidy`` as console scripts.
Their CLI entry points are not exported here.
"""

from hephaestus.github.mnemosyne_repo import (
    UPSTREAM_SLUG,
    MnemosyneTarget,
    fork_upstream,
    gh_authenticated_login,
    remote_repo_exists,
    resolve_mnemosyne_target,
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
    "UPSTREAM_SLUG",
    "MnemosyneTarget",
    "collect_stats",
    "fork_upstream",
    "format_stats_table",
    "get_commits_stats",
    "get_current_repo",
    "get_issues_stats",
    "get_prs_stats",
    "gh_authenticated_login",
    "remote_repo_exists",
    "resolve_mnemosyne_target",
    "validate_date",
]
