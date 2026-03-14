#!/usr/bin/env python3

"""Version management utilities for updating and verifying version files.

Supports updating version numbers across:
- VERSION (root file)
- __init__.py (__version__ attribute)
"""

import re
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root


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
    ):
        """Initialize the version manager.

        Args:
            repo_root: Repository root path. If None, will attempt to detect.
            version_files: List of VERSION file paths. Defaults to [repo_root/VERSION].
            init_files: List of __init__.py files to update.
                Defaults to [repo_root/<package>/__init__.py].

        """
        self.repo_root = repo_root or get_repo_root()
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

    def update_version_file(self, version_file: Path, version: str, verbose: bool = True) -> None:
        """Update VERSION file.

        Args:
            version_file: Path to VERSION file
            version: New version string
            verbose: Print status messages

        """
        if verbose:
            print(f"Updating {version_file}...")
        version_file.write_text(f"{version}\n")
        if verbose:
            print(f"  ✓ Updated to {version}")

    def update_init_file(self, init_file: Path, version: str, verbose: bool = True) -> None:
        """Update __version__ in __init__.py file.

        Args:
            init_file: Path to __init__.py file
            version: New version string
            verbose: Print status messages

        """
        if not init_file.exists():
            if verbose:
                print(f"  ⚠️  Warning: {init_file} not found, skipping")
            return

        if verbose:
            print(f"Updating {init_file}...")

        content = init_file.read_text()

        # Update __version__ = "x.y.z" pattern
        new_content = re.sub(
            r'__version__\s*=\s*["\']([^"\']+)["\']', f'__version__ = "{version}"', content
        )

        if new_content == content:
            if verbose:
                print(f"  ⚠️  Warning: No __version__ attribute found in {init_file}")
            return

        init_file.write_text(new_content)
        if verbose:
            print(f'  ✓ Updated __version__ = "{version}"')

    def update(self, version: str, verbose: bool = True) -> None:
        """Update all configured version files.

        Args:
            version: New version string
            verbose: Print status messages

        """
        # Parse and validate version
        major, minor, patch = parse_version(version)
        if verbose:
            print(f"Parsed version: {version} (major={major}, minor={minor}, patch={patch})\n")

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
            print("\nVerifying version files...")

        success = True

        # Check VERSION files
        for version_file in self.version_files:
            if version_file.exists():
                content = version_file.read_text().strip()
                if content == version:
                    if verbose:
                        print(f"  ✓ {version_file.relative_to(self.repo_root)}: {content}")
                else:
                    if verbose:
                        print(
                            f"  ✗ {version_file.relative_to(self.repo_root)}: {content}"
                            f" (expected {version})"
                        )
                    success = False
            else:
                if verbose:
                    print(f"  ✗ {version_file.relative_to(self.repo_root)} not found")
                success = False

        # Check __init__.py files
        for init_file in self.init_files:
            if init_file.exists():
                content = init_file.read_text()
                # Match __version__ = "x.y.z" pattern
                match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
                if match and match.group(1) == version:
                    if verbose:
                        print(f"  ✓ {init_file.relative_to(self.repo_root)}: {match.group(1)}")
                else:
                    if verbose:
                        print(f"  ✗ {init_file.relative_to(self.repo_root)}: version mismatch")
                    success = False
            else:
                if verbose:
                    print(f"  ⚠️  {init_file.relative_to(self.repo_root)} not found (optional)")

        return success
