"""Compatibility exports for the trusted remote Git library module.

The implementation lives in ``hephaestus.git.github.remote_git`` so the library
keeps one trusted definition. These exports preserve object identity:
``hephaestus.git.remote_git`` holds the same function and class objects as
the library module.
"""

from __future__ import annotations

from hephaestus.git.github.remote_git import (
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
