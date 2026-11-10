"""Build trusted command-scoped configuration for remote Git operations.

The trusted remote Git implementation moved to :mod:`hephaestus.github.remote_git`
so the library layer owns it and the automation product layer consumes it
(Automation→library direction, ADR-0001). This module is an
identity-preserving compatibility export for current callers and test seams.
"""

from __future__ import annotations

from hephaestus.github.remote_git import (
    TrustedRemoteGit,
    trusted_gh_authenticated,
    trusted_gh_executable,
    trusted_remote_git_config,
)

__all__ = [
    "TrustedRemoteGit",
    "trusted_gh_authenticated",
    "trusted_gh_executable",
    "trusted_remote_git_config",
]
