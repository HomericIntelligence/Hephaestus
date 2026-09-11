"""Keep Fleet runtime storage and tool environments separate from the operator."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hephaestus.config.child_environments import build_codex_child_env

_TOOL_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_PRIVATE_PATHS = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "xdg/config",
    "XDG_CACHE_HOME": "xdg/cache",
    "XDG_DATA_HOME": "xdg/data",
    "TMPDIR": "tmp",
    "TMP": "tmp",
    "TEMP": "tmp",
}


def require_execution_platform(platform: str) -> None:
    """Reject native modes whose pinned process policy cannot isolate scratch."""
    if platform == "darwin":
        raise ValueError("native_macos_requires_isolated_linux_worker")
    if platform == "linux":
        raise ValueError("linux_execution_requires_verified_boundary")
    if platform != "linux":
        raise ValueError("unsupported_execution_platform")


def validate_worker_storage(
    codex_home: Path,
    state_dir: Path,
    workspace_root: Path,
    *,
    scratch_roots: tuple[Path, ...] | None = None,
) -> None:
    """Reject shared scratch and overlapping authority/workspace directories."""
    if scratch_roots is None:
        scratch_roots = (Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()))
    paths = [path.resolve() for path in (codex_home, state_dir, workspace_root)]
    for private in paths[:2]:
        if any(private.is_relative_to(root.resolve()) for root in scratch_roots):
            raise ValueError("private_storage_in_shared_scratch")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError("worker_storage_overlap")


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("private_directory_symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    stat = path.stat()
    if not path.is_dir() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise ValueError("private_directory_permissions")


def _private_environment(root: Path) -> dict[str, str]:
    _private_directory(root)
    result = {"PATH": _TOOL_PATH, "SHELL": "/bin/sh", "LANG": "C.UTF-8"}
    for name, relative in _PRIVATE_PATHS.items():
        path = root
        for component in Path(relative).parts:
            path /= component
            _private_directory(path)
        result[name] = str(path)
    return result


def provider_environment(codex_home: Path) -> dict[str, str]:
    """Retain approved OS substrate but replace all operator configuration roots."""
    environment = build_codex_child_env(codex_home=codex_home)
    environment.update(_private_environment(codex_home / "fleet-runtime"))
    # These alternate selectors must not redirect a Linux tool to host storage.
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        environment.pop(name, None)
    return environment


def session_environment(workspace: Path) -> dict[str, str]:
    """Create each conversation's private home/cache/scratch under its workspace."""
    return _private_environment(workspace / ".fleet-runtime")


def shell_environment_policy(workspace: Path) -> dict[str, object]:
    """Use an explicit tool environment without provider or controller context."""
    return {
        "inherit": "none",
        "ignore_default_excludes": False,
        "experimental_use_profile": False,
        "set": session_environment(workspace),
    }
