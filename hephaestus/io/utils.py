#!/usr/bin/env python3
"""Input/output utilities for ProjectHephaestus.

Standardized interfaces for file operations, data serialization,
and resource management.

Usage:
    from hephaestus.io.utils import safe_write, ensure_directory

    ensure_directory('/path/to/dir')
    safe_write('/path/to/file.txt', 'content')
"""

import json
from pathlib import Path
from typing import Any, cast

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)

# Formats that require unsafe deserialization (e.g. pickle)
_UNSAFE_FORMATS = {"pickle"}

# All supported serialization formats
_SUPPORTED_FORMATS = {"json", "yaml", "pickle"}


def read_file(filepath: str | Path, mode: str = "r") -> str | bytes:
    """Read content from a file.

    Args:
        filepath: Path to file
        mode: File open mode ('r' for text, 'rb' for binary)

    Returns:
        File content as string or bytes

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read

    """
    filepath = Path(filepath)
    with open(filepath, mode) as f:
        return cast(str | bytes, f.read())


def write_file(
    filepath: str | Path,
    content: str | bytes,
    mode: str = "w",
) -> bool:
    """Write content to a file.

    Args:
        filepath: Path to file
        content: Content to write
        mode: File open mode ('w' for text, 'wb' for binary)

    Returns:
        True if successful

    Raises:
        OSError: If the file cannot be written

    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, mode) as f:
        f.write(content)
    return True


def ensure_directory(path: str | Path) -> bool:
    """Ensure directory exists, creating it if necessary.

    Args:
        path: Path to directory

    Returns:
        True if successful

    Raises:
        OSError: If the directory cannot be created

    """
    Path(path).mkdir(parents=True, exist_ok=True)
    return True


def safe_write(
    filepath: str | Path,
    content: str | bytes,
    backup: bool = True,
) -> bool:
    """Write content to file safely with optional backup.

    Args:
        filepath: Path to file
        content: Content to write
        backup: Whether to create backup of existing file

    Returns:
        True if successful

    Raises:
        OSError: If the file cannot be written

    """
    filepath = Path(filepath)

    if backup and filepath.exists():
        backup_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            backup_path.write_bytes(filepath.read_bytes())
        except OSError as e:
            logger.warning("Could not create backup: %s", e)

    ensure_directory(filepath.parent)
    if isinstance(content, str):
        filepath.write_text(content)
    else:
        filepath.write_bytes(content)
    return True


def write_secure(
    filepath: str | Path,
    content: str,
    permissions: int = 0o600,
) -> None:
    """Write content to file with restrictive permissions.

    Args:
        filepath: Path to file
        content: Text content to write
        permissions: File permission bits (default: 0o600, owner read/write only)

    Raises:
        OSError: If the file cannot be written or permissions cannot be set

    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content)
    filepath.chmod(permissions)


def _detect_format(filepath: Path, format_hint: str | None) -> str:
    """Detect serialization format from file extension or hint.

    Args:
        filepath: Path to file
        format_hint: Caller-supplied format override

    Returns:
        Format string ('json', 'yaml', or 'pickle')

    Raises:
        ValueError: If format_hint is not in the supported formats set
            ('json', 'yaml', 'pickle'), or if format cannot be determined
            from the file extension when no hint is provided.

    """
    if format_hint is not None:
        if format_hint not in _SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported format: '{format_hint}'. "
                f"Supported formats: {sorted(_SUPPORTED_FORMATS)}"
            )
        return format_hint
    ext = filepath.suffix.lower()
    if ext == ".json":
        return "json"
    if ext in {".yml", ".yaml"}:
        return "yaml"
    if ext == ".pkl":
        return "pickle"
    raise ValueError(f"Could not determine serialization format for '{filepath}'")


def load_data(
    filepath: str | Path,
    format_hint: str | None = None,
    allow_unsafe_deserialization: bool = False,
) -> Any:
    """Load data from file with automatic format detection.

    Args:
        filepath: Path to file
        format_hint: Optional format hint ('json', 'yaml', 'pickle')
        allow_unsafe_deserialization: If False (default), raise ValueError for
            formats that can execute arbitrary code (e.g. pickle).

    Returns:
        Loaded data object

    Raises:
        ValueError: If format is unsafe and allow_unsafe_deserialization is False,
            or if format cannot be determined.
        FileNotFoundError: If the file does not exist.

    """
    filepath = Path(filepath)
    fmt = _detect_format(filepath, format_hint)

    if fmt in _UNSAFE_FORMATS and not allow_unsafe_deserialization:
        raise ValueError(
            f"Format '{fmt}' uses unsafe deserialization. "
            "Set allow_unsafe_deserialization=True to proceed at your own risk."
        )

    if fmt == "json":
        with open(filepath) as f:
            return json.load(f)
    if fmt == "yaml":
        import yaml  # lazy import — optional dependency

        with open(filepath) as f:
            return yaml.safe_load(f)
    if fmt == "pickle":
        import pickle

        with open(filepath, "rb") as f:
            return pickle.load(f)

    raise ValueError(f"Unsupported format: '{fmt}'")


def save_data(
    data: Any,
    filepath: str | Path,
    format_hint: str | None = None,
    allow_unsafe_deserialization: bool = False,
) -> None:
    """Save data to file with automatic format detection.

    Args:
        data: Data to save
        filepath: Path to file
        format_hint: Optional format hint ('json', 'yaml', 'pickle')
        allow_unsafe_deserialization: If False (default), raise ValueError for
            formats that can execute arbitrary code (e.g. pickle).

    Raises:
        ValueError: If format is unsafe and allow_unsafe_deserialization is False,
            or if format is unsupported or cannot be determined.

    """
    filepath = Path(filepath)
    fmt = _detect_format(filepath, format_hint)

    if fmt in _UNSAFE_FORMATS and not allow_unsafe_deserialization:
        raise ValueError(
            f"Format '{fmt}' uses unsafe deserialization. "
            "Set allow_unsafe_deserialization=True to proceed at your own risk."
        )

    filepath.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        filepath.write_text(json.dumps(data, indent=2))
    elif fmt == "yaml":
        import yaml  # lazy import

        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
    elif fmt == "pickle":
        import pickle

        with open(filepath, "wb") as f:
            pickle.dump(data, f)
    else:
        raise ValueError(f"Unsupported format: '{fmt}'")
