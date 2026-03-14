"""Shared constants for the ProjectHephaestus package."""

# Default directories to exclude when scanning files recursively.
# Used across markdown, validation, and other file-traversal utilities.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "venv",
        "__pycache__",
        ".tox",
        ".pixi",
        ".pytest_cache",
        "dist",
        "build",
        ".mypy_cache",
        ".eggs",
    }
)

# Standard log format used across all logging utilities.
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
