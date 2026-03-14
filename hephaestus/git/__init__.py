"""Git utilities for ProjectHephaestus.

Provides utilities for working with git repositories, including changelog generation
and commit analysis.
"""

from .changelog import (
    categorize_commits,
    generate_changelog,
    parse_commit,
)

__all__ = [
    "categorize_commits",
    "generate_changelog",
    "parse_commit",
]
