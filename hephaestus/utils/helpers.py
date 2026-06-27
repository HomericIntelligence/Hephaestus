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

from packaging.requirements import InvalidRequirement, Requirement

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

# Subprocess timeouts for different operation types.
# METADATA_TIMEOUT: local, non-network queries (git status, git config, pixi list)
# NETWORK_TIMEOUT: operations touching the network (gh calls, git clone/fetch/push)
# Both support env-var overrides for CI tuning.
METADATA_TIMEOUT: int = int(os.environ.get("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "10"))
NETWORK_TIMEOUT: int = int(os.environ.get("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "120"))


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


def strip_null_bytes(text: str) -> str:
    r"""Remove NUL (``\x00``) bytes from text destined for a subprocess.

    :func:`subprocess.run` raises ``ValueError: embedded null byte`` if any argv
    element (or text passed via stdin) contains a NUL. Agent output and malformed
    GitHub issue bodies can carry stray NULs, which would otherwise permanently
    strand the affected work item in the automation loop. Strip them defensively
    at the invoke/data boundary.

    Args:
        text: Text that may contain embedded NUL bytes.

    Returns:
        ``text`` with every NUL byte removed; the same object when none are
        present (clean text is byte-identical).

    """
    return text.replace("\x00", "") if "\x00" in text else text


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
    """Find repository root by walking up to a ``.git`` or ``pyproject.toml`` marker.

    This is the single canonical repository-root resolver for the codebase. It
    accepts an optional starting path so callers anchored to a file (e.g.
    ``Path(__file__)``) and callers relying on the current working directory can
    share one implementation. A directory is treated as the repository root if it
    contains either a ``.git`` entry (git checkout) or a ``pyproject.toml`` file
    (project marker), covering both git-based and packaging-based callers.

    Args:
        start_path: Starting path to search from. Defaults to current directory.

    Returns:
        Path to repository root if found, otherwise the resolved start path as a
        fallback.

    """
    start_path = Path.cwd().resolve() if start_path is None else Path(start_path).resolve()

    path = start_path
    while path != path.parent:  # Stop at filesystem root
        if (path / ".git").exists() or (path / "pyproject.toml").exists():
            return path
        path = path.parent

    # No marker found anywhere on the path to the filesystem root; fall back to
    # the original start path.
    return start_path


_LOG_ARG_MAX = 200


def _format_cmd_for_log(cmd: list[str]) -> str:
    """Render *cmd* for a log line, truncating any argument longer than 200 chars.

    Defense-in-depth: large argv values (e.g. a forgotten ``--body`` with a
    multi-KB string) would otherwise dump straight into ERROR logs on
    subprocess failure. Truncation keeps each log line bounded while still
    leaving enough of each argument to identify the command.
    """
    parts: list[str] = []
    for arg in cmd:
        if len(arg) > _LOG_ARG_MAX:
            parts.append(f"{arg[:_LOG_ARG_MAX]}…({len(arg) - _LOG_ARG_MAX} more chars)")
        else:
            parts.append(arg)
    return " ".join(parts)


def run_subprocess(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    check: bool = True,
    dry_run: bool = False,
    log_on_error: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run subprocess command with proper error handling.

    Args:
        cmd: Command and arguments as list
        cwd: Working directory for command execution
        timeout: Optional timeout in seconds
        check: Whether to raise on non-zero exit code
        dry_run: If True, log the command but do not execute it
        log_on_error: If False, suppress ERROR logging when the command fails.
            Use when failure is expected and already handled by the caller.
        env: Optional environment dict to pass to subprocess.run().
            If provided, replaces the current process environment.

    Returns:
        Completed process object

    Raises:
        subprocess.CalledProcessError: If command fails and check=True

    """
    if dry_run:
        logger.info("[DRY-RUN] $ %s", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Inject correlation ID into subprocess environment if set.
    # Function-local import to keep module import graph clean.
    effective_env = env.copy() if env is not None else os.environ.copy()
    from hephaestus.logging.utils import get_current_correlation_id

    cid = get_current_correlation_id()
    if cid:
        effective_env["GH_TRACE_ID"] = cid

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=effective_env,
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error(
            "Command timed out after %ds: %s",
            timeout,
            _format_cmd_for_log(cmd),
        )
        raise
    except subprocess.CalledProcessError as e:
        if log_on_error:
            logger.error("Command failed: %s", _format_cmd_for_log(cmd))
            stderr = e.stderr or ""
            logger.error("stderr: %s", stderr[:_LOG_ARG_MAX])
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
    """Install a single Python package with pip.

    Validates the package name using the PEP 508 requirement parser from
    the ``packaging`` library. Supports extras (e.g. ``pkg[extra1,extra2]``)
    and version specifiers (e.g. ``pkg>=1.0,<2``), but rejects URL-based
    requirements for security.

    Args:
        package_name: A single PEP 508 requirement string
            (e.g. ``"requests"``, ``"pkg[extra]>=1.0"``).
        upgrade: Whether to upgrade if already installed.

    Returns:
        True if installation successful, False otherwise.

    Raises:
        ValueError: If package_name is not a valid PEP 508 requirement
            or uses a URL-based requirement.

    """
    if not package_name or not package_name.strip():
        raise ValueError(f"Invalid package requirement: {package_name!r}")

    # Validate using the canonical PEP 508 requirement parser
    try:
        req = Requirement(package_name)
    except InvalidRequirement as e:
        raise ValueError(f"Invalid package requirement: {package_name!r}") from e

    # Reject URL-based requirements for security
    if req.url is not None:
        raise ValueError(f"URL-based requirements are not supported: {package_name!r}")

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
