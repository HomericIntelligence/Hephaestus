"""Git utilities for ProjectHephaestus.

Provides utilities for working with git repositories, including changelog generation
and commit analysis.
"""

from .changelog import (
    parse_commit,
    categorize_commits,
    generate_changelog,
)

__all__ = [
    "parse_commit",
    "categorize_commits",
    "generate_changelog",
]
