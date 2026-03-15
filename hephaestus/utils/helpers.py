"""Helper functions for ProjectHephaestus.

General utility functions that don't fit in other specific modules.
"""

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Args:
        text: Text to convert to slug

    Returns:
        URL-friendly slug string

    """
    # Normalize unicode characters
    text = unicodedata.normalize("NFKD", text)
    # Convert to ASCII
    text = text.encode("ascii", "ignore").decode("ascii")
    # Convert to lowercase and replace spaces/underscores/dots with hyphens
    text = re.sub(r"[\s_.]+", "-", text.lower())
    # Remove non-alphanumeric characters (except hyphens)
    text = re.sub(r"[^a-z0-9-]", "", text)
    # Remove leading/trailing hyphens
    text = text.strip("-")
    # Replace multiple consecutive hyphens with single hyphen
    text = re.sub(r"-+", "-", text)
    return text


def human_readable_size(size_bytes: int | float) -> str:
    """Convert byte size to human readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Human readable size string with appropriate unit

    """
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)

    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1

    return f"{size:.1f} {size_names[i]}"


def flatten_dict(d: dict[str, Any], parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dictionary using dot notation for keys.

    Args:
        d: Dictionary to flatten
        parent_key: Parent key prefix
        sep: Separator for nested keys

    Returns:
        Flattened dictionary

    """
    items: list[Any] = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def get_repo_root(start_path: str | Path | None = None) -> Path:
    """Find repository root by looking for .git directory.

    Args:
        start_path: Starting path to search from. Defaults to current directory.

    Returns:
        Path to repository root if found, otherwise the original start_path as fallback.

    """
    start_path = Path.cwd() if start_path is None else Path(start_path).resolve()

    path = start_path
    while path != path.parent:  # Stop at filesystem root
        if (path / ".git").exists():
            return path
        path = path.parent

    # If we get here, we didn't find a .git directory
    # Return the original start path as fallback
    return start_path


def run_subprocess(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess command with proper error handling.

    Args:
        cmd: Command and arguments as list
        cwd: Working directory for command execution
        timeout: Optional timeout in seconds
        check: Whether to raise on non-zero exit code
        dry_run: If True, log the command but do not execute it

    Returns:
        Completed process object

    Raises:
        subprocess.CalledProcessError: If command fails and check=True

    """
    if dry_run:
        logger.info("[DRY-RUN] $ %s", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
        return result
    except subprocess.CalledProcessError as e:
        logger.error("Command failed: %s", " ".join(cmd))
        logger.error("stderr: %s", e.stderr)
        raise


def get_proj_root(proj_name: str) -> str:
    """Get absolute path to project root by name.

    First checks for PROJECT_ROOT environment variable, then searches
    filesystem for a git repository with matching name.

    Args:
        proj_name: Name of the project (e.g., 'ProjectHephaestus')

    Returns:
        Absolute path to project root

    Raises:
        ValueError: If project root cannot be determined

    """
    proj_env_var = f"{proj_name.upper()}_ROOT"
    proj_root = os.environ.get(proj_env_var)

    if not proj_root:
        # Fallback to relative path approach
        current_dir = Path.cwd()
        while current_dir != current_dir.parent:
            if (current_dir / ".git").exists() and current_dir.name == proj_name:
                proj_root = str(current_dir)
                break
            current_dir = current_dir.parent

    if not proj_root:
        raise ValueError(
            f"Could not determine {proj_name} root. Please set {proj_env_var} environment variable."
        )

    return proj_root


def install_package(package_name: str, upgrade: bool = False) -> bool:
    """Install Python package with pip.

    Args:
        package_name: Name of package to install (must be a valid PyPI package name)
        upgrade: Whether to upgrade if already installed

    Returns:
        True if installation successful, False otherwise

    Raises:
        ValueError: If package_name contains invalid characters

    """
    # Validate package name: only alphanumerics, hyphens, underscores, dots, brackets, ==, >=, <=
    if not re.match(r"^[A-Za-z0-9_\-\.\[\],>=<!\s]+$", package_name):
        raise ValueError(f"Invalid package name: {package_name!r}")

    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package_name)

    try:
        run_subprocess(cmd)
        logger.info("Successfully installed %s", package_name)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to install %s: %s", package_name, e)
        return False
