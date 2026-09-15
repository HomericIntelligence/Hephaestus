"""Resolve bounded Node and npm reads for the learning validator."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError

NodeInspector = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]
_MAX_FILES = 128
_TIMEOUT_S = 10.0
_MAX_PACKAGE_ENTRIES = 100_000
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
_MAX_PACKAGE_MANIFEST_BYTES = 1024 * 1024
_MAX_PACKAGE_DEPTH = 64
_MAX_PACKAGE_COMPONENT_BYTES = 255
_MAX_PACKAGE_RELATIVE_PATH_BYTES = 4096
_MAX_PACKAGE_LINK_TARGET_BYTES = 4096
_MAX_PACKAGE_LINK_HOPS = 40
_MAX_PACKAGE_METADATA_BYTES = 16 * 1024 * 1024
_MAX_PACKAGE_ACTIVE_DESCRIPTORS = 128
_PACKAGE_WALK_TIMEOUT_S = 10.0
_PACKAGE_TIMEOUT_MESSAGE = "Node package dependency tree timed out"
_SNAPSHOT_SCANDIR = os.scandir
_SNAPSHOT_UNLINK = os.unlink
_SNAPSHOT_RMDIR = os.rmdir


@dataclass
class NodePackageTree:
    """Bind one complete npm package tree for a sandbox read grant."""

    root: Path
    digest: str
    snapshot_root: Path
    snapshot_cli: Path
    _snapshot_parent: Path
    _snapshot_digest: str
    _closed: bool = False

    def verify(self) -> None:
        """Reject a package tree that changed after its admission."""
        deadline = _package_deadline()
        if (
            _package_tree_digest(self.root, deadline=deadline) != self.digest
            or _package_tree_digest(self.snapshot_root, deadline=deadline) != self._snapshot_digest
        ):
            raise LearnDeliveryError("Node package dependency tree changed")

    def close(self) -> None:
        """Remove the private package snapshot."""
        if self._closed:
            return
        _remove_package_snapshot(self._snapshot_parent)
        self._closed = True

    def __enter__(self) -> NodePackageTree:
        """Return this active package snapshot."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: object,
    ) -> None:
        """Remove the snapshot and preserve an active validation error."""
        try:
            self.close()
        except LearnDeliveryError:
            if exception_type is None:
                raise

    def __del__(self) -> None:
        """Make a best effort to remove an unused test snapshot."""
        with suppress(BaseException):
            self.close()


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


def _manifest_bin_target(payload: bytes, cli_relative: str) -> str:
    """Return the validated manifest CLI path relative to the npm root."""
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise LearnDeliveryError("Node package manifest is unavailable") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "markdownlint-cli2":
        raise LearnDeliveryError("Node package manifest is invalid")
    binaries = manifest.get("bin")
    if not isinstance(binaries, dict) or not isinstance(binaries.get("markdownlint-cli2"), str):
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    raw_value = binaries["markdownlint-cli2"]
    raw_target = Path(raw_value)
    if (
        raw_target.is_absolute()
        or raw_value in {"", "."}
        or ".." in raw_target.parts
        or len(raw_target.parts) == 0
    ):
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    target = Path(_CLI_PACKAGE_NAME, *raw_target.parts).as_posix()
    if target != cli_relative:
        raise LearnDeliveryError("Node package manifest bin target is invalid")
    return target


def _cli_root_candidates(cli: Path) -> tuple[Path, ...]:
    """Return every ancestor npm root that declares the CLI package."""
    candidates: list[Path] = []
    current = cli.parent
    while current != current.parent:
        if current.name == _PACKAGE_ROOT_NAME:
            package = current / _CLI_PACKAGE_NAME
            if (package / "package.json").is_file():
                candidates.append(current)
        current = current.parent
    return tuple(candidates)


_POSIX_PACKAGE_DESCRIPTOR_WALK = os.name == "posix" and all(
    hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
)
_PACKAGE_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_PACKAGE_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_SNAPSHOT_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass
class _PackageDirectoryFrame:
    """Keep the state for one active package directory."""

    descriptor: int
    relative_parent: Path
    children: list[os.DirEntry[str]]
    initial: os.stat_result
    name: str | None = None
    index: int = 0
    close_descriptor: bool = True


@dataclass
class _BoundPackageRoot:
    """Keep each canonical path component bound during one package read."""

    descriptors: list[int]
    names: tuple[str, ...]
    identities: tuple[os.stat_result, ...]

    @property
    def descriptor(self) -> int:
        """Return the descriptor for the final root component."""
        return self.descriptors[-1]

    def verify(self) -> None:
        """Reject a replaced component in the canonical root path."""
        try:
            for index, identity in enumerate(self.identities):
                opened = os.fstat(self.descriptors[index])
                if _package_node_identity(identity) != _package_node_identity(opened):
                    raise LearnDeliveryError("Node package dependency tree is unavailable")
                if index:
                    named = os.stat(
                        self.names[index - 1],
                        dir_fd=self.descriptors[index - 1],
                        follow_symlinks=False,
                    )
                    if _package_node_identity(identity) != _package_node_identity(named):
                        raise LearnDeliveryError("Node package dependency tree is unavailable")
        except LearnDeliveryError:
            raise
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None

    def close(self, *, preserve_error: bool) -> None:
        """Close all canonical path descriptors."""
        descriptors = tuple(reversed(self.descriptors))
        self.descriptors.clear()
        _close_package_descriptors(
            descriptors,
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=preserve_error,
        )


@dataclass
class _SnapshotCleanupFrame:
    """Keep one bounded iterative snapshot cleanup frame."""

    descriptor: int
    parent_descriptor: int | None
    name: str | None
    children: list[os.DirEntry[str]]
    index: int = 0


@dataclass(frozen=True)
class _ResolvedPackageLink:
    """Keep one descriptor-resolved internal link target."""

    relative_path: Path
    is_directory: bool


@dataclass
class _PackageEntryBudget:
    """Track processed and materialized entries across one package walk."""

    limit: int
    processed: int = 0
    pending: int = 0

    @property
    def remaining(self) -> int:
        """Return entry slots that no record or pending child owns."""
        return self.limit - self.processed - self.pending

    def consume_unreserved(self) -> None:
        """Consume one slot for a record that has no pending child."""
        if self.remaining <= 0:
            raise _too_large()
        self.processed += 1

    def reserve_pending(self) -> None:
        """Reserve one slot before a scan stores a pending child."""
        if self.remaining <= 0:
            raise _too_large()
        self.pending += 1

    def release_pending(self, count: int) -> None:
        """Release stored children when their scan cannot return them."""
        self.pending -= count

    def consume_pending(self) -> None:
        """Convert one pending child reservation to a processed record."""
        if self.pending <= 0:
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        self.pending -= 1
        self.processed += 1


_PACKAGE_ROOT_NAME: Final = "node_modules"
_CLI_PACKAGE_NAME: Final = "markdownlint-cli2"
_PACKAGE_MANIFEST_RELATIVE: Final = f"{_CLI_PACKAGE_NAME}/package.json"


def _package_entry_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the stable identity fields for one package entry."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _package_node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return stable identity fields for a canonical path component."""
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _package_deadline(deadline: float | None = None) -> float:
    """Return the supplied deadline or make one package-walk deadline."""
    return deadline if deadline is not None else time.monotonic() + _PACKAGE_WALK_TIMEOUT_S


def _check_package_deadline(deadline: float) -> None:
    """Stop a package walk that exceeded its elapsed-time limit."""
    if time.monotonic() > deadline:
        raise LearnDeliveryError(_PACKAGE_TIMEOUT_MESSAGE)


def _too_large() -> LearnDeliveryError:
    """Return the stable package resource-limit error."""
    return LearnDeliveryError("Node package dependency tree is too large")


def _bounded_relative_path(relative_parent: Path, name: str) -> tuple[Path, bytes]:
    """Return one encoded relative path within component and path limits."""
    try:
        component = name.encode("utf-8")
    except UnicodeEncodeError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if not component or len(component) > _MAX_PACKAGE_COMPONENT_BYTES:
        raise _too_large()
    relative = relative_parent / name
    try:
        encoded = relative.as_posix().encode("utf-8")
    except UnicodeEncodeError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if len(encoded) > _MAX_PACKAGE_RELATIVE_PATH_BYTES:
        raise _too_large()
    return relative, encoded


def _bounded_package_record(parts: tuple[bytes, ...], remaining_bytes: int) -> bytes:
    """Build one package record only when aggregate metadata permits it."""
    record_size = sum(len(part) for part in parts) + len(parts) - 1
    if record_size > remaining_bytes:
        raise _too_large()
    return b"\0".join(parts)


def _close_package_descriptors(
    descriptors: tuple[int, ...],
    *,
    unavailable_message: str,
    preserve_error: bool,
) -> None:
    """Attempt all descriptor closes and apply one stable error contract."""
    failed = False
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    if failed and not preserve_error:
        raise LearnDeliveryError(unavailable_message)


def _bounded_snapshot_children(descriptor: int) -> list[os.DirEntry[str]]:
    """Return cleanup children without an unbounded directory allocation."""
    with _SNAPSHOT_SCANDIR(descriptor) as entries:
        children: list[os.DirEntry[str]] = []
        for child in entries:
            if len(children) >= _MAX_PACKAGE_ENTRIES:
                raise LearnDeliveryError("Node package snapshot cleanup failed")
            children.append(child)
    return children


def _append_snapshot_cleanup_frame(
    frames: list[_SnapshotCleanupFrame],
    *,
    parent_descriptor: int | None,
    name: str | None,
    metadata: os.stat_result | None = None,
    root: Path | None = None,
) -> None:
    """Acquire and transfer one cleanup descriptor to the frame stack."""
    descriptor = -1
    transferred = False
    primary_error = False
    try:
        if parent_descriptor is None:
            if root is None:
                raise LearnDeliveryError("Node package snapshot cleanup failed")
            descriptor = os.open(root, _PACKAGE_DIRECTORY_FLAGS)
        else:
            if name is None or metadata is None:
                raise LearnDeliveryError("Node package snapshot cleanup failed")
            descriptor = _open_package_directory(parent_descriptor, name, metadata)
        os.fchmod(descriptor, 0o700)
        children = _bounded_snapshot_children(descriptor)
        frames.append(_SnapshotCleanupFrame(descriptor, parent_descriptor, name, children))
        transferred = True
    except BaseException:
        primary_error = True
        raise
    finally:
        if descriptor >= 0 and not transferred:
            _close_package_descriptors(
                (descriptor,),
                unavailable_message="Node package snapshot cleanup failed",
                preserve_error=primary_error,
            )


def _remove_package_snapshot(snapshot_parent: Path) -> None:
    """Remove one private snapshot with a bounded descriptor walk."""
    try:
        snapshot_parent.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise LearnDeliveryError("Node package snapshot cleanup failed") from None
    frames: list[_SnapshotCleanupFrame] = []
    primary_error = False
    try:
        _append_snapshot_cleanup_frame(
            frames,
            parent_descriptor=None,
            name=None,
            root=snapshot_parent,
        )
        while frames:
            frame = frames[-1]
            if frame.index < len(frame.children):
                child = frame.children[frame.index]
                frame.index += 1
                metadata = os.stat(child.name, dir_fd=frame.descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    if len(frames) > _MAX_PACKAGE_DEPTH + 1:
                        raise LearnDeliveryError("Node package snapshot cleanup failed")
                    _append_snapshot_cleanup_frame(
                        frames,
                        parent_descriptor=frame.descriptor,
                        name=child.name,
                        metadata=metadata,
                    )
                else:
                    _SNAPSHOT_UNLINK(child.name, dir_fd=frame.descriptor)
                continue
            completed = frames.pop()
            os.close(completed.descriptor)
            if completed.parent_descriptor is not None and completed.name is not None:
                _SNAPSHOT_RMDIR(completed.name, dir_fd=completed.parent_descriptor)
        _SNAPSHOT_RMDIR(snapshot_parent)
    except LearnDeliveryError:
        primary_error = True
        raise LearnDeliveryError("Node package snapshot cleanup failed") from None
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package snapshot cleanup failed") from None
    finally:
        descriptors = tuple(frame.descriptor for frame in reversed(frames))
        _close_package_descriptors(
            descriptors,
            unavailable_message="Node package snapshot cleanup failed",
            preserve_error=primary_error,
        )


def _write_snapshot_file(path: Path, payload: bytes, mode: int, deadline: float) -> None:
    """Write descriptor-read bytes to one exclusive snapshot file."""
    _check_package_deadline(deadline)
    try:
        descriptor = os.open(path, _SNAPSHOT_FILE_FLAGS, 0o600)
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    primary_error = False
    try:
        offset = 0
        while offset < len(payload):
            _check_package_deadline(deadline)
            written = os.write(descriptor, payload[offset:])
            _check_package_deadline(deadline)
            if written <= 0:
                raise LearnDeliveryError("Node package dependency tree is unavailable")
            offset += written
        os.fchmod(descriptor, 0o500 if mode & 0o111 else 0o400)
    except BaseException:
        primary_error = True
        raise
    finally:
        _close_package_descriptors(
            (descriptor,),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=primary_error,
        )


def _snapshot_package_link(
    snapshot_root: Path,
    relative_path: Path,
    target_relative: Path,
    *,
    target_is_directory: bool,
    deadline: float,
) -> None:
    """Create one internal snapshot link from its validated source target."""
    _check_package_deadline(deadline)
    snapshot_path = snapshot_root / relative_path
    snapshot_target = snapshot_root / target_relative
    target_text = os.path.relpath(snapshot_target, start=snapshot_path.parent)
    try:
        snapshot_path.symlink_to(target_text, target_is_directory=target_is_directory)
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    _check_package_deadline(deadline)


def _freeze_package_snapshot(snapshot_root: Path, deadline: float) -> None:
    """Remove write permission from all directories in one private snapshot."""
    try:
        for directory, _children, _files in os.walk(snapshot_root, topdown=False):
            _check_package_deadline(deadline)
            os.chmod(directory, 0o500)
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None


def _read_package_regular_file(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
    remaining_bytes: int,
    deadline: float,
) -> bytes:
    """Read one stable regular file within the remaining byte limit."""
    return _read_bounded_regular_file(
        parent_descriptor,
        name,
        metadata,
        remaining_bytes,
        deadline=deadline,
        unavailable_message="Node package dependency tree is unavailable",
        too_large_message="Node package dependency tree is too large",
    )


def _read_bounded_regular_file(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
    remaining_bytes: int,
    *,
    deadline: float | None = None,
    unavailable_message: str,
    too_large_message: str,
) -> bytes:
    """Read one descriptor-bound regular file within one byte limit."""
    effective_deadline = _package_deadline(deadline)
    _check_package_deadline(effective_deadline)
    if metadata.st_size > remaining_bytes:
        raise LearnDeliveryError(too_large_message)
    try:
        descriptor = os.open(name, _PACKAGE_FILE_FLAGS, dir_fd=parent_descriptor)
    except OSError:
        raise LearnDeliveryError(unavailable_message) from None
    primary_error = False
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            metadata.st_dev,
            metadata.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise LearnDeliveryError(unavailable_message)
        if opened.st_size > remaining_bytes:
            raise LearnDeliveryError(too_large_message)
        chunks: list[bytes] = []
        size = 0
        while True:
            _check_package_deadline(effective_deadline)
            chunk = os.read(descriptor, min(1024 * 1024, remaining_bytes + 1 - size))
            _check_package_deadline(effective_deadline)
            if not chunk:
                break
            size += len(chunk)
            if size > remaining_bytes:
                raise LearnDeliveryError(too_large_message)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        identities = tuple(
            _package_entry_identity(value) for value in (metadata, opened, after, current)
        )
        if len(set(identities)) != 1:
            raise LearnDeliveryError(unavailable_message)
        return b"".join(chunks)
    except LearnDeliveryError:
        primary_error = True
        raise
    except OSError:
        primary_error = True
        raise LearnDeliveryError(unavailable_message) from None
    except BaseException:
        primary_error = True
        raise
    finally:
        _close_package_descriptors(
            (descriptor,),
            unavailable_message=unavailable_message,
            preserve_error=primary_error,
        )


def _read_package_manifest(
    package_descriptor: int, deadline: float
) -> tuple[bytes, os.stat_result]:
    """Read and bind one manifest below the admitted package directory."""
    try:
        metadata = os.stat("package.json", dir_fd=package_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise LearnDeliveryError("Node package manifest is unavailable")
        payload = _read_bounded_regular_file(
            package_descriptor,
            "package.json",
            metadata,
            _MAX_PACKAGE_MANIFEST_BYTES,
            deadline=deadline,
            unavailable_message="Node package manifest is unavailable",
            too_large_message="Node package manifest is too large",
        )
        return payload, metadata
    except LearnDeliveryError:
        raise
    except OSError:
        raise LearnDeliveryError("Node package manifest is unavailable") from None


def _open_package_directory(parent_descriptor: int, name: str, metadata: os.stat_result) -> int:
    """Open one directory and match it to its enumerated identity."""
    descriptor = -1
    try:
        descriptor = os.open(name, _PACKAGE_DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            _close_package_descriptors(
                (descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=True,
            )
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if _package_entry_identity(metadata) != _package_entry_identity(opened):
        _close_package_descriptors(
            (descriptor,),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=True,
        )
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    return descriptor


def _transfer_package_descriptor(
    descriptors: list[int], descriptor: int, *, unavailable_message: str
) -> None:
    """Transfer one descriptor to a list or close it after a failed transfer."""
    try:
        descriptors.append(descriptor)
    except BaseException:
        _close_package_descriptors(
            (descriptor,),
            unavailable_message=unavailable_message,
            preserve_error=True,
        )
        raise


def _bind_package_inputs(
    root: _BoundPackageRoot,
    cli_relative: str,
    deadline: float,
) -> tuple[bytes, dict[str, os.stat_result]]:
    """Bind the package, manifest, and CLI below one open npm root."""
    package_descriptor = -1
    opened_directories: list[int] = []
    cli_descriptor = -1
    primary_error = False
    expected: dict[str, os.stat_result] = {}
    try:
        package_metadata = os.stat(
            _CLI_PACKAGE_NAME,
            dir_fd=root.descriptor,
            follow_symlinks=False,
        )
        if len(root.descriptors) + 1 > _MAX_PACKAGE_ACTIVE_DESCRIPTORS:
            raise _too_large()
        package_descriptor = _open_package_directory(
            root.descriptor, _CLI_PACKAGE_NAME, package_metadata
        )
        _transfer_package_descriptor(
            opened_directories,
            package_descriptor,
            unavailable_message="Node package manifest is unavailable",
        )
        expected[_CLI_PACKAGE_NAME] = package_metadata

        cli_parts = Path(cli_relative).parts
        if not cli_parts or cli_parts[0] != _CLI_PACKAGE_NAME:
            raise LearnDeliveryError("Node package CLI has an ambiguous root")
        relative_parent = Path(_CLI_PACKAGE_NAME)
        parent_descriptor = package_descriptor
        for name in cli_parts[1:-1]:
            relative_path, _encoded = _bounded_relative_path(relative_parent, name)
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if len(root.descriptors) + len(opened_directories) + 1 > (
                _MAX_PACKAGE_ACTIVE_DESCRIPTORS
            ):
                raise _too_large()
            descriptor = _open_package_directory(parent_descriptor, name, metadata)
            _transfer_package_descriptor(
                opened_directories,
                descriptor,
                unavailable_message="Node package manifest is unavailable",
            )
            expected[relative_path.as_posix()] = metadata
            relative_parent = relative_path
            parent_descriptor = descriptor
        cli_name = cli_parts[-1]
        cli_path, _encoded = _bounded_relative_path(relative_parent, cli_name)
        cli_metadata = os.stat(cli_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(cli_metadata.st_mode):
            raise LearnDeliveryError("Node package CLI is not a regular file")
        if len(root.descriptors) + len(opened_directories) + 1 > (_MAX_PACKAGE_ACTIVE_DESCRIPTORS):
            raise _too_large()
        try:
            cli_descriptor = os.open(cli_name, _PACKAGE_FILE_FLAGS, dir_fd=parent_descriptor)
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        opened_cli = os.fstat(cli_descriptor)
        if not stat.S_ISREG(opened_cli.st_mode) or _package_entry_identity(
            cli_metadata
        ) != _package_entry_identity(opened_cli):
            raise LearnDeliveryError("Node package CLI is not a regular file")
        expected[cli_path.as_posix()] = cli_metadata
        closing_descriptor = cli_descriptor
        cli_descriptor = -1
        _close_package_descriptors(
            (closing_descriptor,),
            unavailable_message="Node package CLI is not a regular file",
            preserve_error=False,
        )

        manifest_payload, manifest_metadata = _read_package_manifest(package_descriptor, deadline)
        expected[_PACKAGE_MANIFEST_RELATIVE] = manifest_metadata
        _manifest_bin_target(manifest_payload, cli_relative)
        return manifest_payload, expected
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    except BaseException:
        primary_error = True
        raise
    finally:
        descriptors = []
        if cli_descriptor >= 0:
            descriptors.append(cli_descriptor)
        descriptors.extend(reversed(opened_directories))
        _close_package_descriptors(
            tuple(descriptors),
            unavailable_message="Node package manifest is unavailable",
            preserve_error=primary_error,
        )


_PackageChildDirectory = tuple[int, Path, str, os.stat_result]
_PackageEntryResult = tuple[bytes, int, _PackageChildDirectory | None]


def _regular_package_entry(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
    relative_path: Path,
    relative: bytes,
    mode: int,
    remaining_bytes: int,
    remaining_metadata_bytes: int,
    snapshot_root: Path | None,
    expected_identities: dict[str, os.stat_result],
    captured_payloads: dict[str, bytes],
    deadline: float,
) -> _PackageEntryResult:
    """Read, record, and optionally snapshot one regular package file."""
    payload = _read_package_regular_file(
        parent_descriptor, name, metadata, remaining_bytes, deadline
    )
    record = _bounded_package_record(
        (
            b"file",
            relative,
            f"{mode:o}".encode(),
            hashlib.sha256(payload).hexdigest().encode(),
        ),
        remaining_metadata_bytes,
    )
    if snapshot_root is not None:
        _write_snapshot_file(snapshot_root / relative_path, payload, mode, deadline)
    relative_text = relative_path.as_posix()
    if relative_text == _PACKAGE_MANIFEST_RELATIVE or relative_text in expected_identities:
        captured_payloads[relative_text] = payload
    return record, len(payload), None


def _directory_package_entry(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
    relative_path: Path,
    relative: bytes,
    mode: int,
    remaining_metadata_bytes: int,
    snapshot_root: Path | None,
) -> _PackageEntryResult:
    """Open, record, and optionally allocate one package directory."""
    if len(relative_path.parts) > _MAX_PACKAGE_DEPTH:
        raise _too_large()
    record = _bounded_package_record(
        (b"directory", relative, f"{mode:o}".encode()), remaining_metadata_bytes
    )
    descriptor = _open_package_directory(parent_descriptor, name, metadata)
    transferred = False
    try:
        if snapshot_root is not None:
            (snapshot_root / relative_path).mkdir(mode=0o700)
        result = (record, 0, (descriptor, relative_path, name, metadata))
        transferred = True
        return result
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    finally:
        if not transferred:
            _close_package_descriptors(
                (descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=True,
            )


def _bounded_link_components(base: tuple[str, ...], target_text: str) -> tuple[str, ...]:
    """Normalize one internal link target within every path byte limit."""
    try:
        target_bytes = target_text.encode("utf-8")
    except UnicodeEncodeError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if len(target_bytes) > _MAX_PACKAGE_LINK_TARGET_BYTES:
        raise _too_large()
    if target_text.startswith("/"):
        raise LearnDeliveryError("Node package dependency link escapes its root")
    components = list(base)
    for component in target_text.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not components:
                raise LearnDeliveryError("Node package dependency link escapes its root")
            components.pop()
            continue
        try:
            encoded = component.encode("utf-8")
        except UnicodeEncodeError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        if not encoded or len(encoded) > _MAX_PACKAGE_COMPONENT_BYTES:
            raise _too_large()
        components.append(component)
    if len(components[:-1]) > _MAX_PACKAGE_DEPTH:
        raise _too_large()
    relative = "/".join(components).encode("utf-8")
    if len(relative) > _MAX_PACKAGE_RELATIVE_PATH_BYTES:
        raise _too_large()
    return tuple(components)


def _expanded_package_link(
    parent_descriptor: int,
    component: str,
    metadata: os.stat_result,
    prefix: tuple[str, ...],
    remainder: tuple[str, ...],
) -> tuple[str, ...]:
    """Read one stable link hop and return its bounded complete path."""
    nested_target = os.readlink(component, dir_fd=parent_descriptor)
    current = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
    if _package_entry_identity(metadata) != _package_entry_identity(current):
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    pending = (*_bounded_link_components(prefix, nested_target), *remainder)
    if len(pending[:-1]) > _MAX_PACKAGE_DEPTH:
        raise _too_large()
    if len("/".join(pending).encode("utf-8")) > _MAX_PACKAGE_RELATIVE_PATH_BYTES:
        raise _too_large()
    return pending


def _advance_package_link_directory(
    current_descriptor: int,
    component: str,
    metadata: os.stat_result,
    owned_descriptor: int,
    descriptor_slots: int,
) -> int:
    """Open the next link-path directory and close the prior one."""
    required_slots = 1 if owned_descriptor < 0 else 2
    if required_slots > descriptor_slots:
        raise _too_large()
    next_descriptor = -1
    owns_previous = owned_descriptor >= 0
    transferred = False
    primary_error = False
    try:
        next_descriptor = _open_package_directory(current_descriptor, component, metadata)
        if owns_previous:
            owns_previous = False
            _close_package_descriptors(
                (owned_descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=False,
            )
        transferred = True
        return next_descriptor
    except BaseException:
        primary_error = True
        raise
    finally:
        if next_descriptor >= 0 and not transferred:
            _close_package_descriptors(
                (next_descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=True,
            )
        if owns_previous:
            _close_package_descriptors(
                (owned_descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=primary_error,
            )


def _resolve_package_link(
    root_descriptor: int,
    source_parent: tuple[str, ...],
    target_text: str,
    *,
    descriptor_slots: int,
    deadline: float,
) -> _ResolvedPackageLink:
    """Resolve all link hops through descriptors below one bound root."""
    pending = _bounded_link_components(source_parent, target_text)
    link_hops = 0
    while True:
        _check_package_deadline(deadline)
        if not pending:
            return _ResolvedPackageLink(Path(), True)
        current_descriptor = root_descriptor
        owned_descriptor = -1
        prefix: list[str] = []
        restart = False
        primary_error = False
        try:
            for index, component in enumerate(pending):
                metadata = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(metadata.st_mode):
                    if link_hops >= _MAX_PACKAGE_LINK_HOPS:
                        raise _too_large()
                    link_hops += 1
                    pending = _expanded_package_link(
                        current_descriptor,
                        component,
                        metadata,
                        tuple(prefix),
                        pending[index + 1 :],
                    )
                    restart = True
                    break
                if index == len(pending) - 1:
                    if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode):
                        raise LearnDeliveryError("Node package dependency tree is unavailable")
                    return _ResolvedPackageLink(Path(*pending), stat.S_ISDIR(metadata.st_mode))
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LearnDeliveryError("Node package dependency tree is unavailable")
                previous_descriptor = owned_descriptor
                owned_descriptor = -1
                owned_descriptor = _advance_package_link_directory(
                    current_descriptor,
                    component,
                    metadata,
                    previous_descriptor,
                    descriptor_slots,
                )
                current_descriptor = owned_descriptor
                prefix.append(component)
        except BaseException:
            primary_error = True
            raise
        finally:
            if owned_descriptor >= 0:
                _close_package_descriptors(
                    (owned_descriptor,),
                    unavailable_message="Node package dependency tree is unavailable",
                    preserve_error=primary_error,
                )
        if not restart:
            raise LearnDeliveryError("Node package dependency tree is unavailable")


def _link_package_entry(
    root_descriptor: int,
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
    relative_path: Path,
    relative: bytes,
    mode: int,
    remaining_metadata_bytes: int,
    snapshot_root: Path | None,
    descriptor_slots: int,
    deadline: float,
) -> _PackageEntryResult:
    """Validate, record, and optionally copy one internal package link."""
    target_text = os.readlink(name, dir_fd=parent_descriptor)
    target = _resolve_package_link(
        root_descriptor,
        tuple(relative_path.parent.parts),
        target_text,
        descriptor_slots=descriptor_slots,
        deadline=deadline,
    )
    target_bytes = target_text.encode("utf-8")
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _package_entry_identity(metadata) != _package_entry_identity(current):
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    record = _bounded_package_record(
        (b"link", relative, f"{mode:o}".encode(), target_bytes),
        remaining_metadata_bytes,
    )
    if snapshot_root is not None:
        _snapshot_package_link(
            snapshot_root,
            relative_path,
            target.relative_path,
            target_is_directory=target.is_directory,
            deadline=deadline,
        )
    return record, 0, None


def _package_tree_entry_record(
    root_descriptor: int,
    parent_descriptor: int,
    relative_parent: Path,
    child: os.DirEntry[str],
    remaining_bytes: int,
    remaining_metadata_bytes: int,
    snapshot_root: Path | None,
    expected_identities: dict[str, os.stat_result],
    captured_payloads: dict[str, bytes],
    descriptor_slots: int,
    deadline: float,
) -> _PackageEntryResult:
    """Return one entry record, its file size, and any directory to visit."""
    relative_path, relative = _bounded_relative_path(relative_parent, child.name)
    try:
        _check_package_deadline(deadline)
        metadata = os.stat(child.name, dir_fd=parent_descriptor, follow_symlinks=False)
        _check_package_deadline(deadline)
        expected = expected_identities.get(relative_path.as_posix())
        if expected is not None and (
            _package_entry_identity(expected) != _package_entry_identity(metadata)
        ):
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            return _regular_package_entry(
                parent_descriptor,
                child.name,
                metadata,
                relative_path,
                relative,
                mode,
                remaining_bytes,
                remaining_metadata_bytes,
                snapshot_root,
                expected_identities,
                captured_payloads,
                deadline,
            )
        if stat.S_ISDIR(metadata.st_mode):
            return _directory_package_entry(
                parent_descriptor,
                child.name,
                metadata,
                relative_path,
                relative,
                mode,
                remaining_metadata_bytes,
                snapshot_root,
            )
        if stat.S_ISLNK(metadata.st_mode):
            return _link_package_entry(
                root_descriptor,
                parent_descriptor,
                child.name,
                metadata,
                relative_path,
                relative,
                mode,
                remaining_metadata_bytes,
                snapshot_root,
                descriptor_slots,
                deadline,
            )
        raise LearnDeliveryError("Node package dependency tree contains a special entry")
    except LearnDeliveryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None


def _bounded_package_children(
    directory_descriptor: int,
    entry_budget: _PackageEntryBudget,
    deadline: float | None = None,
) -> list[os.DirEntry[str]]:
    """Return sorted children reserved against one traversal-wide limit."""
    effective_deadline = _package_deadline(deadline)
    _check_package_deadline(effective_deadline)
    children: list[os.DirEntry[str]] = []
    reserved = 0
    complete = False
    try:
        entries = os.scandir(directory_descriptor)
        _check_package_deadline(effective_deadline)
        with entries:
            for child in entries:
                _check_package_deadline(effective_deadline)
                entry_budget.reserve_pending()
                try:
                    children.append(child)
                except BaseException:
                    entry_budget.release_pending(1)
                    raise
                reserved += 1
        children.sort(key=lambda entry: entry.name)
        complete = True
        return children
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    finally:
        if not complete:
            entry_budget.release_pending(reserved)


def _open_package_root(root: Path) -> _BoundPackageRoot:
    """Open each canonical root component once without following links."""
    if not root.is_absolute() or root.name != _PACKAGE_ROOT_NAME:
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    names = tuple(root.parts[1:])
    if len(names) + 1 > _MAX_PACKAGE_ACTIVE_DESCRIPTORS:
        raise _too_large()
    descriptors: list[int] = []
    identities: list[os.stat_result] = []
    primary_error = False
    try:
        descriptor = os.open(root.anchor, _PACKAGE_DIRECTORY_FLAGS)
        _transfer_package_descriptor(
            descriptors,
            descriptor,
            unavailable_message="Node package dependency tree is unavailable",
        )
        identities.append(os.fstat(descriptor))
        for name in names:
            parent = descriptors[-1]
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise LearnDeliveryError("Node package dependency tree is unavailable")
            descriptor = _open_package_directory(parent, name, named)
            _transfer_package_descriptor(
                descriptors,
                descriptor,
                unavailable_message="Node package dependency tree is unavailable",
            )
            identities.append(named)
        binding = _BoundPackageRoot(descriptors, names, tuple(identities))
        binding.verify()
        return binding
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    except BaseException:
        primary_error = True
        raise
    finally:
        if primary_error:
            _close_package_descriptors(
                tuple(reversed(descriptors)),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=True,
            )


def _finish_package_directory_frame(
    frame: _PackageDirectoryFrame, parent_descriptor: int | None
) -> None:
    """Validate and close one completed package directory frame."""
    primary_error = False
    try:
        opened = os.fstat(frame.descriptor)
        identities = [_package_entry_identity(frame.initial), _package_entry_identity(opened)]
        if frame.name is not None:
            if parent_descriptor is None:
                raise LearnDeliveryError("Node package dependency tree is unavailable")
            named = os.stat(frame.name, dir_fd=parent_descriptor, follow_symlinks=False)
            identities.append(_package_entry_identity(named))
        if len(set(identities)) != 1:
            raise LearnDeliveryError("Node package dependency tree is unavailable")
    except LearnDeliveryError:
        primary_error = True
        raise
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    finally:
        if frame.close_descriptor:
            _close_package_descriptors(
                (frame.descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=primary_error,
            )


def _package_tree_records(
    root: Path,
    *,
    root_binding: _BoundPackageRoot | None = None,
    snapshot_root: Path | None = None,
    expected_identities: dict[str, os.stat_result] | None = None,
    captured_payloads: dict[str, bytes] | None = None,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    """Return deterministic records for every entry in one npm root."""
    if not _POSIX_PACKAGE_DESCRIPTOR_WALK:
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    effective_deadline = _package_deadline(deadline)
    _check_package_deadline(effective_deadline)
    binding = root_binding
    owns_binding = root_binding is None
    frames: list[_PackageDirectoryFrame] = []
    required_identities = expected_identities or {}
    payloads = captured_payloads if captured_payloads is not None else {}
    primary_error = False
    try:
        if binding is None:
            binding = _open_package_root(root)
        opened_root = os.fstat(binding.descriptor)
        entry_budget = _PackageEntryBudget(_MAX_PACKAGE_ENTRIES)
        entry_budget.consume_unreserved()
        root_record = _bounded_package_record(
            (b"directory", b".", f"{stat.S_IMODE(opened_root.st_mode):o}".encode()),
            _MAX_PACKAGE_METADATA_BYTES,
        )
        records: list[bytes] = [root_record]
        frames.append(
            _PackageDirectoryFrame(
                binding.descriptor,
                Path(),
                [],
                opened_root,
                close_descriptor=False,
            )
        )
        seen_paths: set[str] = set()
        total_bytes = 0
        total_metadata_bytes = len(root_record) + 1
        binding.verify()
        frames[0].children = _bounded_package_children(
            binding.descriptor, entry_budget, effective_deadline
        )
        while frames:
            frame = frames[-1]
            if frame.index >= len(frame.children):
                parent_descriptor = frames[-2].descriptor if len(frames) > 1 else None
                completed = frames.pop()
                _finish_package_directory_frame(completed, parent_descriptor)
                continue
            child = frame.children[frame.index]
            frame.index += 1
            entry_budget.consume_pending()
            active_descriptors = len(binding.descriptors) + len(frames) - 1
            if active_descriptors + 1 > _MAX_PACKAGE_ACTIVE_DESCRIPTORS:
                raise _too_large()
            record, size, child_directory = _package_tree_entry_record(
                binding.descriptor,
                frame.descriptor,
                frame.relative_parent,
                child,
                _MAX_PACKAGE_BYTES - total_bytes,
                _MAX_PACKAGE_METADATA_BYTES - total_metadata_bytes,
                snapshot_root,
                required_identities,
                payloads,
                _MAX_PACKAGE_ACTIVE_DESCRIPTORS - active_descriptors,
                effective_deadline,
            )
            records.append(record)
            total_bytes += size
            total_metadata_bytes += len(record) + 1
            seen_paths.add((frame.relative_parent / child.name).as_posix())
            if child_directory is not None:
                descriptor, relative_path, name, initial = child_directory
                transferred = False
                try:
                    children = _bounded_package_children(
                        descriptor,
                        entry_budget,
                        effective_deadline,
                    )
                    frames.append(
                        _PackageDirectoryFrame(descriptor, relative_path, children, initial, name)
                    )
                    transferred = True
                finally:
                    if not transferred:
                        _close_package_descriptors(
                            (descriptor,),
                            unavailable_message=("Node package dependency tree is unavailable"),
                            preserve_error=True,
                        )
        if not required_identities.keys() <= seen_paths:
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        binding.verify()
        return tuple(sorted(records))
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    except BaseException:
        primary_error = True
        raise
    finally:
        _close_package_descriptors(
            tuple(frame.descriptor for frame in frames if frame.close_descriptor),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=primary_error,
        )
        if owns_binding and binding is not None:
            binding.close(preserve_error=primary_error)


def _package_records_digest(records: tuple[bytes, ...]) -> str:
    """Hash deterministic records from one admitted package tree."""
    digest = hashlib.sha256()
    for record in records:
        digest.update(record)
        digest.update(b"\n")
    return digest.hexdigest()


def _package_tree_digest(root: Path, *, deadline: float | None = None) -> str:
    """Hash every entry and mode in one admitted package tree."""
    effective_deadline = _package_deadline(deadline)
    digest = _package_records_digest(_package_tree_records(root, deadline=effective_deadline))
    _check_package_deadline(effective_deadline)
    return digest


def node_package_tree(cli: Path) -> NodePackageTree:
    """Resolve and bind the canonical npm tree for one Markdown lint CLI."""
    if not _POSIX_PACKAGE_DESCRIPTOR_WALK:
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    cli = cli.resolve()
    if not cli.is_file() or cli.is_symlink():
        raise LearnDeliveryError("Node package CLI is not a regular file")
    package = cli.parent
    root = package.parent
    if package.name != _CLI_PACKAGE_NAME or root.name != _PACKAGE_ROOT_NAME:
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    if not root.is_dir() or len(_cli_root_candidates(cli)) != 1:
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    deadline = time.monotonic() + _PACKAGE_WALK_TIMEOUT_S
    cli_relative = cli.relative_to(root).as_posix()
    binding = _open_package_root(root)
    primary_error = False
    snapshot_parent: Path | None = None
    try:
        manifest_payload, expected_identities = _bind_package_inputs(
            binding, cli_relative, deadline
        )
        try:
            snapshot_parent = Path(tempfile.mkdtemp(prefix="hephaestus-node-package-")).resolve()
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        snapshot_root = snapshot_parent / _PACKAGE_ROOT_NAME
        try:
            snapshot_root.mkdir(mode=0o700)
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        captured_payloads: dict[str, bytes] = {}
        records = _package_tree_records(
            root,
            root_binding=binding,
            snapshot_root=snapshot_root,
            expected_identities=expected_identities,
            captured_payloads=captured_payloads,
            deadline=deadline,
        )
        if captured_payloads.get(_PACKAGE_MANIFEST_RELATIVE) != manifest_payload:
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        snapshot_cli_relative = _manifest_bin_target(
            captured_payloads[_PACKAGE_MANIFEST_RELATIVE], cli_relative
        )
        _freeze_package_snapshot(snapshot_root, deadline)
        snapshot_cli = snapshot_root / snapshot_cli_relative
        if not snapshot_cli.is_file() or snapshot_cli.is_symlink():
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        binding.verify()
        binding.close(preserve_error=False)
        result = NodePackageTree(
            root=root,
            digest=_package_records_digest(records),
            snapshot_root=snapshot_root,
            snapshot_cli=snapshot_cli,
            _snapshot_parent=snapshot_parent,
            _snapshot_digest=_package_tree_digest(snapshot_root, deadline=deadline),
        )
        _check_package_deadline(deadline)
        return result
    except BaseException:
        primary_error = True
        binding.close(preserve_error=True)
        if snapshot_parent is not None:
            with suppress(LearnDeliveryError):
                _remove_package_snapshot(snapshot_parent)
        raise
    finally:
        binding.close(preserve_error=primary_error)
