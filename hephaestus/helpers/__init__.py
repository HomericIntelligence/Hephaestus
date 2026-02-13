"""Utility functions for ProjectHephaestus.

Backward compatibility layer - imports from hephaestus.utils.
"""

from hephaestus.helpers.utils import (
    # From helpers
    slugify,
    human_readable_size,
    flatten_dict,
    get_repo_root,
    run_subprocess,
    get_proj_root,
    install_package,
    # From retry
    retry_with_backoff,
    retry_on_network_error,
    retry_with_jitter,
    is_network_error,
)

__all__ = [
    # From helpers
    "slugify",
    "human_readable_size",
    "flatten_dict",
    "get_repo_root",
    "run_subprocess",
    "get_proj_root",
    "install_package",
    # From retry
    "retry_with_backoff",
    "retry_on_network_error",
    "retry_with_jitter",
    "is_network_error",
]
