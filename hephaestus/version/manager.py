#!/usr/bin/env python3

"""Version management utilities for updating and verifying version files.

The authoritative project version lives in pyproject.toml under [project].version.
This module keeps secondary version files in sync:
- VERSION (root file)
- __init__.py (__version__ attribute)

Note: pixi.toml intentionally has no version field. The package version is
provided to pixi via the editable install from pyproject.toml.
"""

import re
from pathlib import Path

from hephaestus.logging.utils import get_logger
from hephaestus.utils.helpers import get_repo_root

logger = get_logger(__name__)


class _UnsetType:
    """Sentinel type for distinguishing "not provided" from explicit None."""


_UNSET = _UnsetType()  # sentinel: use default pyproject_file path


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse version string into components.

    Args:
        version: Version string in format "MAJOR.MINOR.PATCH"

    Returns:
        Tuple of (major, minor, patch)

    Raises:
        ValueError: If version format is invalid

    """
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
    if not match:
        raise ValueError(
            f"Invalid version format: {version}. Expected format: MAJOR.MINOR.PATCH (e.g., 0.1.0)"
        )

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3))

    return (major, minor, patch)


class VersionManager:
    """Manages version updates across project files."""

    def __init__(
        self,
        repo_root: Path | None = None,
        version_files: list[Path] | None = None,
        init_files: list[Path] | None = None,
        pyproject_file: Path | None | _UnsetType = _UNSET,
    ):
        """Initialize the version manager.

        Args:
            repo_root: Repository root path. If None, will attempt to detect.
            version_files: List of VERSION file paths. Defaults to [repo_root/VERSION].
            init_files: List of __init__.py files to update.
                Defaults to [repo_root/<package>/__init__.py].
            pyproject_file: Path to pyproject.toml. Defaults to repo_root/pyproject.toml.
                Pass ``None`` explicitly to skip pyproject.toml updates entirely.

        """
        self.repo_root = repo_root or get_repo_root()
        if pyproject_file is _UNSET:
            self.pyproject_file: Path | None = self.repo_root / "pyproject.toml"
        else:
            # At this point pyproject_file is Path | None (not _UnsetType)
            self.pyproject_file = pyproject_file  # type: ignore[assignment]
        self.version_files = version_files or [self.repo_root / "VERSION"]

        # Auto-detect init files if not provided
        if init_files is None:
            # Look for common package __init__.py patterns
            potential_inits: list[Path] = []
            for pattern in ["*/__init__.py", "*/*/__init__.py"]:
                potential_inits.extend(self.repo_root.glob(pattern))

            # Filter to main package init (usually in repo_root/<package>/__init__.py)
            self.init_files = []
            for init_file in potential_inits:
                # Skip test, build, and hidden directories
                if any(
                    part.startswith(".") or part in {"tests", "build", "dist", "__pycache__"}
                    for part in init_file.parts
                ):
                    continue
                # Check if it has a __version__ attribute
                if init_file.exists():
                    content = init_file.read_text()
                    if "__version__" in content:
                        self.init_files.append(init_file)
        else:
            self.init_files = init_files

    def update_pyproject_file(
        self, pyproject_file: Path, version: str, verbose: bool = True
    ) -> None:
        """Update version in pyproject.toml [project].version.

        Args:
            pyproject_file: Path to pyproject.toml
            version: New version string
            verbose: Print status messages

        """
        if not pyproject_file.exists():
            if verbose:
                logger.warning("  %s not found, skipping", pyproject_file)
            return

        if verbose:
            logger.info("Updating %s...", pyproject_file)

        content = pyproject_file.read_text()

        # Replace version = "x.y.z" only within the [project] section.
        # The negative lookahead (?!\[) stops the section match at the next header.
        new_content = re.sub(
            r'(\[project\]\n(?:(?!\[).+\n)*?version\s*=\s*")[^"]+(")',
            rf"\g<1>{version}\g<2>",
            content,
        )

        if new_content == content:
            if verbose:
                logger.warning("  No version field found under [project] in %s", pyproject_file)
            return

        pyproject_file.write_text(new_content)
        if verbose:
            logger.info('  Updated [project].version = "%s"', version)

    def update_version_file(self, version_file: Path, version: str, verbose: bool = True) -> None:
        """Update VERSION file.

        Args:
            version_file: Path to VERSION file
            version: New version string
            verbose: Print status messages

        """
        if verbose:
            logger.info("Updating %s...", version_file)
        version_file.write_text(f"{version}\n")
        if verbose:
            logger.info("  Updated to %s", version)

    def update_init_file(self, init_file: Path, version: str, verbose: bool = True) -> None:
        """Update __version__ in __init__.py file.

        Args:
            init_file: Path to __init__.py file
            version: New version string
            verbose: Print status messages

        """
        if not init_file.exists():
            if verbose:
                logger.warning("  %s not found, skipping", init_file)
            return

        if verbose:
            logger.info("Updating %s...", init_file)

        content = init_file.read_text()

        # Update __version__ = "x.y.z" pattern
        new_content = re.sub(
            r'__version__\s*=\s*["\']([^"\']+)["\']', f'__version__ = "{version}"', content
        )

        if new_content == content:
            if verbose:
                logger.warning("  No __version__ attribute found in %s", init_file)
            return

        init_file.write_text(new_content)
        if verbose:
            logger.info('  Updated __version__ = "%s"', version)

    def update(self, version: str, verbose: bool = True) -> None:
        """Update all configured version files.

        Updates pyproject.toml first (primary source), then VERSION and __init__.py.

        Args:
            version: New version string
            verbose: Print status messages

        """
        # Parse and validate version
        major, minor, patch = parse_version(version)
        if verbose:
            logger.info(
                "Parsed version: %s (major=%d, minor=%d, patch=%d)\n", version, major, minor, patch
            )

        # Update pyproject.toml first (primary source of truth)
        if self.pyproject_file is not None:
            self.update_pyproject_file(self.pyproject_file, version, verbose=verbose)

        # Update VERSION files
        for version_file in self.version_files:
            self.update_version_file(version_file, version, verbose=verbose)

        # Update __init__.py files
        for init_file in self.init_files:
            self.update_init_file(init_file, version, verbose=verbose)

    def verify(self, version: str, verbose: bool = True) -> bool:  # noqa: C901
        """Verify that all version files are consistent.

        Args:
            version: Expected version string
            verbose: Print status messages

        Returns:
            True if all files consistent, False otherwise

        """
        if verbose:
            logger.info("\nVerifying version files...")

        success = True

        # Check pyproject.toml (primary source of truth)
        if self.pyproject_file is not None:
            if self.pyproject_file.exists():
                content = self.pyproject_file.read_text()
                match = re.search(
                    r'\[project\]\n(?:(?!\[).+\n)*?version\s*=\s*"([^"]+)"',
                    content,
                )
                if match and match.group(1) == version:
                    if verbose:
                        logger.info(
                            "  %s [project].version: %s",
                            self.pyproject_file.relative_to(self.repo_root),
                            match.group(1),
                        )
                else:
                    if verbose:
                        found = match.group(1) if match else "<not found>"
                        logger.error(
                            "  %s [project].version: %s (expected %s)",
                            self.pyproject_file.relative_to(self.repo_root),
                            found,
                            version,
                        )
                    success = False
            else:
                if verbose:
                    logger.warning("  %s not found (skipping)", self.pyproject_file)

        # Check VERSION files
        for version_file in self.version_files:
            if version_file.exists():
                content = version_file.read_text().strip()
                if content == version:
                    if verbose:
                        logger.info("  %s: %s", version_file.relative_to(self.repo_root), content)
                else:
                    if verbose:
                        logger.error(
                            "  %s: %s (expected %s)",
                            version_file.relative_to(self.repo_root),
                            content,
                            version,
                        )
                    success = False
            else:
                if verbose:
                    logger.error("  %s not found", version_file.relative_to(self.repo_root))
                success = False

        # Check __init__.py files
        for init_file in self.init_files:
            if init_file.exists():
                content = init_file.read_text()
                # Match __version__ = "x.y.z" pattern
                match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
                if match and match.group(1) == version:
                    if verbose:
                        logger.info(
                            "  %s: %s", init_file.relative_to(self.repo_root), match.group(1)
                        )
                else:
                    if verbose:
                        logger.error(
                            "  %s: version mismatch", init_file.relative_to(self.repo_root)
                        )
                    success = False
            else:
                if verbose:
                    logger.warning(
                        "  %s not found (optional)", init_file.relative_to(self.repo_root)
                    )

        return success
