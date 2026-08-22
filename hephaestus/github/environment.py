"""Compatibility import for the named GitHub subprocess environment."""

from __future__ import annotations

from hephaestus.config.child_environments import build_gh_child_env


def gh_child_environment() -> dict[str, str]:
    """Return only the host values needed to locate and authenticate ``gh``."""
    return build_gh_child_env()


__all__ = ["gh_child_environment"]
