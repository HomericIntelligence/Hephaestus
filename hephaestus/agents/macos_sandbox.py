"""Host-enforced process isolation for direct automation providers.

The provider sandbox is not the authority for the host filesystem boundary.
This module creates the macOS Seatbelt wrapper used by direct automation
providers.  It deliberately lives in the library layer so product code can
reuse it without reversing the automation-to-library dependency direction.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path


class ProcessIsolationError(RuntimeError):
    """The requested host process boundary cannot be enforced."""


_SYSTEM_READ_ROOTS = (
    Path("/bin"),
    Path("/sbin"),
    Path("/usr"),
    Path("/System"),
    Path("/opt/homebrew"),
    Path("/usr/local"),
)


def _canonical_root(path: Path) -> Path:
    """Return a real directory root or reject an ambiguous path."""
    if not path.is_dir() or path.is_symlink():
        raise ProcessIsolationError("process isolation requires a regular directory root")
    return path.resolve(strict=True)


def _sandbox_string(path: Path) -> str:
    """Return a safely quoted canonical Seatbelt path literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def macos_sandbox_profile(
    *,
    read_roots: Sequence[Path],
    write_roots: Sequence[Path],
    executable: Path,
    allow_network: bool,
    metadata_roots: Sequence[Path] = (),
    literal_metadata_roots: Sequence[Path] = (),
    ipc_posix_sem_prefix: str | None = None,
) -> str:
    """Build a deny-by-default Seatbelt profile for fixed canonical roots."""
    roots = tuple(dict.fromkeys((*read_roots, *write_roots)))
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow signal (target same-sandbox))",
            *(
                (f'(allow ipc-posix-sem (ipc-posix-name-prefix "{ipc_posix_sem_prefix}"))',)
                if ipc_posix_sem_prefix is not None
                else ()
            ),
            "(allow file-read*",
            *(f'  (subpath "{_sandbox_string(path)}")' for path in read_roots),
            *(f'  (subpath "{_sandbox_string(path)}")' for path in write_roots),
            f'  (literal "{_sandbox_string(executable)}")',
            *(f'  (subpath "{_sandbox_string(path.resolve())}")' for path in _SYSTEM_READ_ROOTS),
            ")",
            *(
                f'(allow file-read-metadata (path-ancestors "{_sandbox_string(path)}"))'
                for path in (*roots, *metadata_roots, executable)
            ),
            *(
                f'(allow file-read-metadata (literal "{_sandbox_string(path)}"))'
                for path in literal_metadata_roots
            ),
            *(f'(allow file-write* (subpath "{_sandbox_string(path)}"))' for path in write_roots),
            "(allow network*)" if allow_network else "(deny network*)",
        )
    )


def macos_isolated_command(
    argv: Sequence[str],
    *,
    read_roots: Sequence[Path],
    write_roots: Sequence[Path],
    profile_directory: Path,
    allow_network: bool,
) -> list[str]:
    """Wrap ``argv`` in a macOS Seatbelt boundary or fail before execution."""
    if sys.platform != "darwin":
        raise ProcessIsolationError("macOS process isolation is unavailable on this host")
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not sandbox_exec.is_file() or not os.access(sandbox_exec, os.X_OK):
        raise ProcessIsolationError("macOS process isolation is unavailable on this host")
    if not argv:
        raise ProcessIsolationError("process isolation requires a command")
    profile_directory = _canonical_root(profile_directory)
    canonical_read = tuple(_canonical_root(path) for path in read_roots)
    canonical_write = tuple(_canonical_root(path) for path in write_roots)
    if not canonical_read or not canonical_write:
        raise ProcessIsolationError("process isolation requires explicit read and write roots")
    if any(
        left != right and (left.is_relative_to(right) or right.is_relative_to(left))
        for left in (*canonical_read, *canonical_write)
        for right in (*canonical_read, *canonical_write)
    ):
        raise ProcessIsolationError("process isolation rejects overlapping roots")
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file() or executable.is_symlink():
        raise ProcessIsolationError("process isolation requires an exact regular executable")
    profile_path = profile_directory / "codex-automation.sb"
    profile_path.write_text(
        macos_sandbox_profile(
            read_roots=canonical_read,
            write_roots=canonical_write,
            executable=executable.resolve(strict=True),
            allow_network=allow_network,
        ),
        encoding="utf-8",
    )
    profile_path.chmod(0o600)
    return [str(sandbox_exec), "-f", str(profile_path), *argv]


__all__ = ["ProcessIsolationError", "macos_isolated_command", "macos_sandbox_profile"]
