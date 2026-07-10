"""Compatibility exports for shared git subprocess helpers.

The implementation lives in :mod:`hephaestus.utils.git` so library packages and
``hephaestus.automation`` can share one git subprocess path without making
``hephaestus.github`` depend on the automation product layer.
"""

from __future__ import annotations

from hephaestus.utils.git import (
    git_branch_exists,
    git_config_get,
    git_ls_remote_contains,
    git_ls_remote_sha,
    git_push,
    git_remote_url,
    git_rev_list_count,
    git_unmerged_files,
    in_git_repo,
    repo_root,
    run_git,
    working_tree_clean,
)

__all__ = [
    "git_branch_exists",
    "git_config_get",
    "git_ls_remote_contains",
    "git_ls_remote_sha",
    "git_push",
    "git_remote_url",
    "git_rev_list_count",
    "git_unmerged_files",
    "in_git_repo",
    "repo_root",
    "run_git",
    "working_tree_clean",
]
