"""Resolve bounded Mach-O library reads for the learning validator."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError

NodeInspector = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]
_MAX_FILES = 128
_TIMEOUT_S = 10.0


def _inspect(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin"},
    )


def _expand_path(value: str, loader: Path, executable: Path) -> Path | None:
    """Expand a fixed loader path without using the working directory."""
    for prefix, base in (("@loader_path", loader.parent), ("@executable_path", executable.parent)):
        if value == prefix or value.startswith(prefix + "/"):
            return (base / value[len(prefix) :].lstrip("/")).resolve()
    path = Path(value)
    return path.resolve() if path.is_absolute() else None


def _rpaths(output: str, loader: Path, executable: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    pending = False
    for line in output.splitlines():
        value = line.strip()
        if value.startswith("cmd "):
            pending = value == "cmd LC_RPATH"
        elif pending and value.startswith("path "):
            raw = value[5:].split(" (offset ", 1)[0]
            expanded = _expand_path(raw, loader, executable)
            if expanded is None:
                raise LearnDeliveryError("Node runtime dependency has an unsupported rpath")
            paths.append(expanded)
            pending = False
    return tuple(paths)


def _dependency(value: str, loader: Path, executable: Path, rpaths: tuple[Path, ...]) -> Path:
    """Resolve one library or reject an incomplete runtime."""
    if value.startswith("@rpath/"):
        candidates = (root / value[len("@rpath/") :] for root in rpaths)
        target = next((path.resolve() for path in candidates if path.is_file()), None)
    else:
        target = _expand_path(value, loader, executable)
    if target is None:
        raise LearnDeliveryError("Node runtime dependency cannot be resolved")
    return target


def node_runtime_files(node: Path, *, runner: NodeInspector = _inspect) -> tuple[Path, ...]:
    """Return exact executable and library paths within one bounded inspection."""
    node = node.resolve()
    deadline = time.monotonic() + _TIMEOUT_S
    pending: list[tuple[Path, tuple[Path, ...]]] = [(node, ())]
    visited: set[Path] = set()
    while pending:
        current, inherited = pending.pop()
        if current in visited:
            continue
        if len(visited) >= _MAX_FILES or not current.is_file():
            raise LearnDeliveryError("Node runtime dependency closure is unavailable or too large")
        visited.add(current)
        outputs: list[str] = []
        for option in ("-l", "-L"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LearnDeliveryError("Node runtime dependency inspection timed out")
            try:
                result = runner(("/usr/bin/otool", option, str(current)), remaining)
            except (OSError, subprocess.TimeoutExpired):
                raise LearnDeliveryError("Node runtime dependency inspection failed") from None
            if result.returncode != 0 or len(result.stdout or "") > 1_048_576:
                raise LearnDeliveryError("Node runtime dependency inspection failed")
            outputs.append(result.stdout or "")
        search = _rpaths(outputs[0], current, node) + inherited
        for line in outputs[1].splitlines():
            if not line.startswith(("\t", " ")) or " (" not in line:
                continue
            value = line.strip().split(" (", 1)[0]
            target = _dependency(value, current, node, search)
            # macOS supplies these libraries from its shared cache.
            if target.is_relative_to("/usr/lib") or target.is_relative_to("/System/Library"):
                continue
            pending.append((target, search))
    return tuple(sorted(visited))
