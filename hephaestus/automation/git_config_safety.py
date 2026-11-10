"""Classify repository-local Git configuration that can change trusted commands.

The classifier moved to :mod:`hephaestus.utils.git` so the library layer and
the automation product layer share one rule. This module keeps the automation
import path for current callers and test seams.
"""

from __future__ import annotations

from hephaestus.utils.git import unsafe_local_git_config_key

__all__ = ["unsafe_local_git_config_key"]
