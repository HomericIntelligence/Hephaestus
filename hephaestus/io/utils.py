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
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from hephaestus.logging.utils import get_logger

_logger = get_logger(__name__)

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
        # cast is required: open() with a str `mode` returns IO[Any], so mypy
        # cannot infer str vs bytes from the runtime argument.
        return cast(str | bytes, f.read())


def write_file(
    filepath: str | Path,
    content: str | bytes,
    mode: str = "w",
) -> None:
    """Write content to a file.

    Args:
        filepath: Path to file
        content: Content to write
        mode: File open mode ('w' for text, 'wb' for binary)

    Raises:
        OSError: If the file cannot be written

    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, mode) as f:
        f.write(content)


def ensure_directory(path: str | Path) -> None:
    """Ensure directory exists, creating it if necessary.

    Args:
        path: Path to directory

    Raises:
        OSError: If the directory cannot be created

    """
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_write(
    filepath: str | Path,
    content: str | bytes,
    backup: bool = True,
) -> None:
    """Write content to a file atomically, with an optional backup.

    The content is first written to a temporary file in the same directory and
    then moved into place with :func:`os.replace`, which is atomic on POSIX and
    Windows. An interrupted write (process kill, OOM, disk-full) therefore never
    leaves a partially written file at ``filepath`` — the target either still
    holds its previous contents or does not exist.

    Args:
        filepath: Path to file
        content: Content to write
        backup: Whether to create a ``.bak`` copy of an existing file first

    Raises:
        OSError: If the file cannot be written

    """
    filepath = Path(filepath)

    if backup and filepath.exists():
        backup_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            backup_path.write_bytes(filepath.read_bytes())
        except OSError as e:
            _logger.warning("Could not create backup: %s", e)

    ensure_directory(filepath.parent)

    data = content.encode() if isinstance(content, str) else content

    # Write to a temp file in the same directory so os.replace() stays on one
    # filesystem (cross-device renames are not atomic and raise OSError).
    fd, tmp_name = tempfile.mkstemp(
        dir=filepath.parent,
        prefix=f".{filepath.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, filepath)
    except BaseException:
        # On any failure, do not leave the temp file behind.
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_secure(
    filepath: str | Path,
    content: str,
    permissions: int = 0o600,
) -> None:
    """Write content to a file atomically and with restrictive permissions.

    The content is written to a temporary file in the same directory —
    ``chmod``-ed to ``permissions`` before any content is written so it is never
    world-readable — then moved into place with :func:`os.replace`, which is
    atomic on POSIX and Windows. An interrupted write therefore never leaves a
    partial or wrongly-permissioned file at ``filepath``.

    Args:
        filepath: Path to file
        content: Text content to write
        permissions: File permission bits (default: 0o600, owner read/write only)

    Raises:
        OSError: If the file cannot be written or permissions cannot be set

    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Create the temp file in the same directory so os.replace() stays on one
    # filesystem, then restrict its permissions before writing any content.
    fd, tmp_name = tempfile.mkstemp(
        dir=filepath.parent,
        prefix=f".{filepath.name}.",
        suffix=".tmp",
    )
    try:
        os.chmod(tmp_name, permissions)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, filepath)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


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
            return pickle.load(f)  # nosec B301 - caller explicitly opted in via allow_unsafe_deserialization=True; default False raises ValueError before this line

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
