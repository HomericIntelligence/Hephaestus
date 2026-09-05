"""Build fail-closed macOS Seatbelt commands for fixed provider processes."""

from __future__ import annotations

import platform
import stat
from collections.abc import Iterable
from pathlib import Path


class MacOSSandboxError(RuntimeError):
    """A requested macOS sandbox boundary cannot be proved."""


def _seatbelt_string(path: Path) -> str:
    """Return one safely quoted canonical Seatbelt path."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _canonical_directory(path: Path) -> Path:
    """Return one existing non-link directory or reject it."""
    if not path.is_absolute():
        raise MacOSSandboxError("sandbox root must be absolute")
    if path.is_symlink():
        raise MacOSSandboxError("sandbox root must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MacOSSandboxError("sandbox root is unavailable") from exc
    if not resolved.is_dir():
        raise MacOSSandboxError("sandbox root must be a directory")
    return resolved


def _canonical_executable(path: Path, *, label: str) -> Path:
    """Return one existing non-link executable regular file or reject it."""
    if not path.is_absolute():
        raise MacOSSandboxError(f"{label} must be absolute")
    if path.is_symlink():
        raise MacOSSandboxError(f"{label} must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise MacOSSandboxError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(mode) or not mode & 0o111:
        raise MacOSSandboxError(f"{label} must be an executable regular file")
    return resolved


def _canonical_roots(paths: Iterable[Path], *, label: str) -> tuple[Path, ...]:
    """Validate roots and reject duplicate or nested roots in one grant class."""
    roots = tuple(_canonical_directory(path) for path in paths)
    if len(set(roots)) != len(roots):
        raise MacOSSandboxError(f"{label} roots must not duplicate")
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise MacOSSandboxError(f"{label} roots must not overlap")
    return roots


def isolated_command(
    *,
    command: tuple[str, ...],
    read_roots: tuple[Path, ...],
    write_roots: tuple[Path, ...],
    sandbox_executable: Path = Path("/usr/bin/sandbox-exec"),
    allow_network: bool = False,
) -> tuple[str, ...]:
    """Return a fixed macOS Seatbelt command with explicit file grants.

    The host must call this before provider process creation.  It rejects
    unsupported hosts and unprovable paths instead of constructing a weaker
    command.  Write roots may also be read roots because a writable worktree
    must remain readable by the provider.
    """
    if platform.system() != "Darwin":
        raise MacOSSandboxError("macOS Seatbelt is required for this boundary")
    if not command:
        raise MacOSSandboxError("sandbox command must not be empty")

    provider = _canonical_executable(Path(command[0]), label="provider executable")
    sandbox_exec = _canonical_executable(sandbox_executable, label="sandbox-exec")
    reads = _canonical_roots(read_roots, label="read")
    writes = _canonical_roots(write_roots, label="write")
    metadata_paths = (*reads, *writes, provider, sandbox_exec)
    policy = "\n".join(
        (
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow signal (target same-sandbox))",
            "(allow file-read*",
            *(f'  (subpath "{_seatbelt_string(path)}")' for path in reads),
            f'  (literal "{_seatbelt_string(provider)}")',
            ")",
            *(
                f'(allow file-read-metadata (path-ancestors "{_seatbelt_string(path)}"))'
                for path in metadata_paths
            ),
            *(f'(allow file-write* (subpath "{_seatbelt_string(path)}"))' for path in writes),
            "(allow network*)" if allow_network else "(deny network*)",
        )
    )
    return (str(sandbox_exec), "-p", policy, str(provider), *command[1:])


__all__ = ["MacOSSandboxError", "isolated_command"]
