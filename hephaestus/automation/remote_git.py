"""Compatibility exports for the trusted remote Git library module.

The implementation moved to ``hephaestus.github.remote_git`` so the
automation product layer keeps its one-way dependency on the library. These
exports preserve object identity: ``hephaestus.automation.remote_git`` is the
same function and class object as the library module.
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
