#!/usr/bin/env python3
"""
General utility functions for ProjectHephaestus.

This module provides backward compatibility by re-exporting functions
from the proper hephaestus.utils module location.
"""

# Re-export all core utilities from hephaestus.utils
from hephaestus.utils.helpers import (
    slugify,
    human_readable_size,
    flatten_dict,
    get_repo_root,
    run_subprocess,
    get_proj_root,
    install_package,
)

from hephaestus.utils.retry import (
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
