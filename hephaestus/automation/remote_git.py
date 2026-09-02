"""Build trusted command-scoped configuration for remote Git operations."""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Sequence
from pathlib import Path

_SYSTEM_GH_CANDIDATES = (
    Path("/opt/homebrew/bin/gh"),
    Path("/usr/local/bin/gh"),
    Path("/usr/bin/gh"),
)
_SYSTEM_GH_ROOTS = (Path("/opt/homebrew"), Path("/usr/local"), Path("/usr"))


def trusted_gh_executable(
    extra_path_root: Path | None = None,
    *,
    system_candidates: Sequence[Path] = _SYSTEM_GH_CANDIDATES,
    system_roots: Sequence[Path] = _SYSTEM_GH_ROOTS,
) -> str | None:
    """Return an allowed absolute ``gh`` executable without use of ``PATH``."""
    candidates = tuple(system_candidates)
    if extra_path_root is not None:
        candidates = (*candidates, extra_path_root / "bin" / "gh")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        if any(resolved.is_relative_to(root) for root in system_roots):
            return str(resolved)
        if extra_path_root is None:
            continue
        try:
            resolved_root = extra_path_root.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_relative_to(resolved_root):
            return str(resolved)
    return None


def trusted_remote_git_config(gh_command: str) -> tuple[str, ...] | None:
    """Return isolated GitHub HTTPS and SSH transport configuration."""
    executable = shutil.which("ssh", path=os.defpath)
    if executable is None:
        return None
    ssh_command = str(Path(executable).resolve())
    ssh_config = " ".join(
        (
            shlex.quote(ssh_command),
            "-F",
            shlex.quote(os.devnull),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
        )
    )
    return (
        "-c",
        f"core.sshCommand={ssh_config}",
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper=!{shlex.quote(gh_command)} auth git-credential",
        "-c",
        "core.askPass=",
        "-c",
        "http.sslVerify=true",
    )
