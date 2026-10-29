# ruff: noqa: N818
"""Release test guard for mutation boundaries."""

from __future__ import annotations

import builtins
import importlib
import socket
import subprocess
from os import PathLike, fspath
from pathlib import Path
from typing import Any, cast

import pytest


class ReleaseEffectDenied(RuntimeError):
    """Raised when a release test touches a denied effect boundary."""


def _deny_effect(*args: object, **kwargs: object) -> None:
    raise ReleaseEffectDenied("release effect denied")


def _is_trap_path(value: object, trap_root: Path) -> bool:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str):
        path = Path(value)
    elif isinstance(value, PathLike):
        raw_path = fspath(cast("PathLike[str]", value))
        if not isinstance(raw_path, str):
            return False
        path = Path(raw_path)
    else:
        return False
    absolute = path if path.is_absolute() else Path.cwd() / path
    return absolute.resolve(strict=False).is_relative_to(trap_root)


@pytest.fixture
def deny_release_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    trap_root = tmp_path / "comet-root-trap"
    trap_root.mkdir()
    monkeypatch.setenv("COMET_ROOT", str(trap_root))

    real_open = builtins.open

    def guarded_open(file: object, *args: Any, **kwargs: Any) -> object:
        if _is_trap_path(file, trap_root):
            raise ReleaseEffectDenied("release file effect denied")
        return cast("Any", real_open)(file, *args, **kwargs)

    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "comet.slurm" or name.startswith("comet.slurm."):
            raise ReleaseEffectDenied("release scheduler effect denied")
        return real_import(name, globals, locals, fromlist, level)

    def guarded_import_module(name: str, package: str | None = None) -> object:
        if name == "comet.slurm" or name.startswith("comet.slurm."):
            raise ReleaseEffectDenied("release scheduler effect denied")
        return real_import_module(name, package)

    real_path_open = Path.open

    def guarded_path_open(self: Path, *args: Any, **kwargs: Any) -> object:
        if _is_trap_path(self, trap_root):
            raise ReleaseEffectDenied("release file effect denied")
        return cast("Any", real_path_open)(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(importlib, "import_module", guarded_import_module)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(subprocess, "Popen", _deny_effect)
    monkeypatch.setattr(subprocess, "run", _deny_effect)
    monkeypatch.setattr(socket.socket, "connect", _deny_effect)
    monkeypatch.setattr(socket, "create_connection", _deny_effect)

    try:
        import asyncpg
    except ImportError:
        pass
    else:
        monkeypatch.setattr(asyncpg, "connect", _deny_effect)

    try:
        import httpx
    except ImportError:
        pass
    else:
        monkeypatch.setattr(httpx.Client, "send", _deny_effect)
        monkeypatch.setattr(httpx.AsyncClient, "send", _deny_effect)

    return trap_root
