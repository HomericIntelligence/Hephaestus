"""Resolve bounded Node and npm reads for the learning validator."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError

NodeInspector = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]
_MAX_FILES = 128
_TIMEOUT_S = 10.0
_MAX_PACKAGE_ENTRIES = 100_000
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class NodePackageTree:
    """Bind one complete npm package tree for a sandbox read grant."""

    root: Path
    digest: str

    def verify(self) -> None:
        """Reject a package tree that changed after its admission."""
        if _package_tree_digest(self.root) != self.digest:
            raise LearnDeliveryError("Node package dependency tree changed")


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


def _manifest_bin_target(package: Path, cli: Path) -> Path:
    """Return the manifest bin target or reject an invalid CLI package."""
    manifest_path = package / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise LearnDeliveryError("Node package manifest is unavailable") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "markdownlint-cli2":
        raise LearnDeliveryError("Node package manifest is invalid")
    binaries = manifest.get("bin")
    if not isinstance(binaries, dict) or not isinstance(binaries.get("markdownlint-cli2"), str):
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    raw_target = Path(binaries["markdownlint-cli2"])
    if raw_target.is_absolute():
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    target = (package / raw_target).resolve()
    if not target.is_relative_to(package) or target != cli or not target.is_file():
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    return target


def _cli_root_candidates(cli: Path) -> tuple[Path, ...]:
    """Return every ancestor npm root that declares the CLI package."""
    candidates: list[Path] = []
    current = cli.parent
    while current != current.parent:
        if current.name == "node_modules":
            package = current / "markdownlint-cli2"
            if (package / "package.json").is_file():
                candidates.append(current)
        current = current.parent
    return tuple(candidates)


def _package_tree_entry_record(
    root: Path, child: os.DirEntry[str], remaining_bytes: int
) -> tuple[bytes, int, Path | None]:
    """Return one entry record, its file size, and any directory to visit."""
    path = Path(child.path)
    try:
        metadata = child.stat(follow_symlinks=False)
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > remaining_bytes:
                raise LearnDeliveryError("Node package dependency tree is too large")
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            return f"file\0{relative}\0{mode:o}\0{content}".encode(), metadata.st_size, None
        if stat.S_ISDIR(metadata.st_mode):
            return f"directory\0{relative}\0{mode:o}".encode(), 0, path
        if stat.S_ISLNK(metadata.st_mode):
            target_text = os.readlink(path)
            target = (path.parent / target_text).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise LearnDeliveryError("Node package dependency link escapes its root")
            return f"link\0{relative}\0{mode:o}\0{target_text}".encode(), 0, None
        raise LearnDeliveryError("Node package dependency tree contains a special entry")
    except (OSError, RuntimeError, ValueError):
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None


def _package_tree_records(root: Path) -> tuple[bytes, ...]:
    """Return deterministic records for every entry in one npm root."""
    try:
        root_metadata = root.lstat()
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    records: list[bytes] = [f"directory\0.\0{stat.S_IMODE(root_metadata.st_mode):o}".encode()]
    pending = [root]
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        for child in children:
            record, file_bytes, child_directory = _package_tree_entry_record(
                root, child, _MAX_PACKAGE_BYTES - total_bytes
            )
            total_bytes += file_bytes
            if total_bytes > _MAX_PACKAGE_BYTES:
                raise LearnDeliveryError("Node package dependency tree is too large")
            records.append(record)
            if child_directory is not None:
                pending.append(child_directory)
            if len(records) > _MAX_PACKAGE_ENTRIES:
                raise LearnDeliveryError("Node package dependency tree is too large")
    return tuple(sorted(records))


def _package_tree_digest(root: Path) -> str:
    """Hash every entry and mode in one admitted package tree."""
    digest = hashlib.sha256()
    for record in _package_tree_records(root):
        digest.update(record)
        digest.update(b"\n")
    return digest.hexdigest()


def node_package_tree(cli: Path) -> NodePackageTree:
    """Resolve and bind the canonical npm tree for one Markdown lint CLI."""
    cli = cli.resolve()
    if not cli.is_file() or cli.is_symlink():
        raise LearnDeliveryError("Node package CLI is not a regular file")
    package = cli.parent
    root = package.parent
    if package.name != "markdownlint-cli2" or root.name != "node_modules":
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    if not root.is_dir() or len(_cli_root_candidates(cli)) != 1:
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    _manifest_bin_target(package, cli)
    digest = _package_tree_digest(root)
    return NodePackageTree(root=root, digest=digest)
