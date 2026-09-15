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

from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError

NodeInspector = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]
_MAX_FILES = 128
_TIMEOUT_S = 10.0
_MAX_PACKAGE_ENTRIES = 100_000
_MAX_PACKAGE_BYTES = 512 * 1024 * 1024
_MAX_PACKAGE_MANIFEST_BYTES = 1024 * 1024
_PACKAGE_WALK_TIMEOUT_S = 10.0
_PACKAGE_TIMEOUT_MESSAGE = "Node package dependency tree timed out"
_SNAPSHOT_SCANDIR = os.scandir
_SNAPSHOT_CHMOD = os.chmod
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


def _manifest_bin_target(package: Path, cli: Path, deadline: float) -> Path:
    """Return the manifest bin target or reject an invalid CLI package."""
    payload = _read_package_manifest(package, deadline)
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
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


def _package_deadline(deadline: float | None = None) -> float:
    """Return the supplied deadline or make one package-walk deadline."""
    return deadline if deadline is not None else time.monotonic() + _PACKAGE_WALK_TIMEOUT_S


def _check_package_deadline(deadline: float) -> None:
    """Stop a package walk that exceeded its elapsed-time limit."""
    if time.monotonic() > deadline:
        raise LearnDeliveryError(_PACKAGE_TIMEOUT_MESSAGE)


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


def _remove_package_snapshot(snapshot_parent: Path) -> None:
    """Remove one private snapshot after restoring owner directory access."""
    if not snapshot_parent.exists():
        return

    def remove_tree(path: Path) -> None:
        _SNAPSHOT_CHMOD(path, 0o700)
        with _SNAPSHOT_SCANDIR(path) as entries:
            children = tuple(entries)
        for child in children:
            child_path = Path(child.path)
            if child.is_dir(follow_symlinks=False):
                remove_tree(child_path)
            else:
                _SNAPSHOT_UNLINK(child_path)
        _SNAPSHOT_RMDIR(path)

    try:
        remove_tree(snapshot_parent)
    except OSError:
        raise LearnDeliveryError("Node package snapshot cleanup failed") from None


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
    source_root: Path,
    snapshot_root: Path,
    relative_path: Path,
    target: Path,
    deadline: float,
) -> None:
    """Create one internal snapshot link from its validated source target."""
    _check_package_deadline(deadline)
    snapshot_path = snapshot_root / relative_path
    snapshot_target = snapshot_root / target.relative_to(source_root)
    target_text = os.path.relpath(snapshot_target, start=snapshot_path.parent)
    try:
        snapshot_path.symlink_to(target_text, target_is_directory=target.is_dir())
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


def _read_package_manifest(package: Path, deadline: float) -> bytes:
    """Read one stable package manifest through a no-follow descriptor."""
    if not _POSIX_PACKAGE_DESCRIPTOR_WALK:
        raise LearnDeliveryError("Node package manifest is unavailable")
    try:
        package_descriptor, _opened = _open_package_root(package)
    except LearnDeliveryError:
        raise LearnDeliveryError("Node package manifest is unavailable") from None
    primary_error = False
    try:
        metadata = os.stat("package.json", dir_fd=package_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise LearnDeliveryError("Node package manifest is unavailable")
        return _read_bounded_regular_file(
            package_descriptor,
            "package.json",
            metadata,
            _MAX_PACKAGE_MANIFEST_BYTES,
            deadline=deadline,
            unavailable_message="Node package manifest is unavailable",
            too_large_message="Node package manifest is too large",
        )
    except LearnDeliveryError:
        primary_error = True
        raise
    except OSError:
        primary_error = True
        raise LearnDeliveryError("Node package manifest is unavailable") from None
    except BaseException:
        primary_error = True
        raise
    finally:
        _close_package_descriptors(
            (package_descriptor,),
            unavailable_message="Node package manifest is unavailable",
            preserve_error=primary_error,
        )


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


def _package_tree_entry_record(
    root: Path,
    parent_descriptor: int,
    relative_parent: Path,
    child: os.DirEntry[str],
    remaining_bytes: int,
    snapshot_root: Path | None,
    deadline: float,
) -> tuple[bytes, int, tuple[int, Path, str, os.stat_result] | None]:
    """Return one entry record, its file size, and any directory to visit."""
    relative_path = relative_parent / child.name
    path = root / relative_path
    try:
        _check_package_deadline(deadline)
        metadata = os.stat(child.name, dir_fd=parent_descriptor, follow_symlinks=False)
        _check_package_deadline(deadline)
        relative = relative_path.as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISREG(metadata.st_mode):
            payload = _read_package_regular_file(
                parent_descriptor, child.name, metadata, remaining_bytes, deadline
            )
            if snapshot_root is not None:
                _write_snapshot_file(snapshot_root / relative_path, payload, mode, deadline)
            content = hashlib.sha256(payload).hexdigest()
            return f"file\0{relative}\0{mode:o}\0{content}".encode(), len(payload), None
        if stat.S_ISDIR(metadata.st_mode):
            descriptor = _open_package_directory(parent_descriptor, child.name, metadata)
            if snapshot_root is not None:
                try:
                    (snapshot_root / relative_path).mkdir(mode=0o700)
                except OSError:
                    _close_package_descriptors(
                        (descriptor,),
                        unavailable_message="Node package dependency tree is unavailable",
                        preserve_error=True,
                    )
                    raise LearnDeliveryError(
                        "Node package dependency tree is unavailable"
                    ) from None
            return (
                f"directory\0{relative}\0{mode:o}".encode(),
                0,
                (descriptor, relative_path, child.name, metadata),
            )
        if stat.S_ISLNK(metadata.st_mode):
            target_text = os.readlink(child.name, dir_fd=parent_descriptor)
            target = (path.parent / target_text).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise LearnDeliveryError("Node package dependency link escapes its root")
            current = os.stat(child.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _package_entry_identity(metadata) != _package_entry_identity(current):
                raise LearnDeliveryError("Node package dependency tree is unavailable")
            if snapshot_root is not None:
                _snapshot_package_link(root, snapshot_root, relative_path, target, deadline)
            return f"link\0{relative}\0{mode:o}\0{target_text}".encode(), 0, None
        raise LearnDeliveryError("Node package dependency tree contains a special entry")
    except LearnDeliveryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None


def _bounded_package_children(
    directory_descriptor: int, remaining_entries: int, deadline: float | None = None
) -> list[os.DirEntry[str]]:
    """Return sorted children without reading past the remaining entry limit."""
    effective_deadline = _package_deadline(deadline)
    _check_package_deadline(effective_deadline)
    if remaining_entries < 0:
        raise LearnDeliveryError("Node package dependency tree is too large")
    try:
        entries = os.scandir(directory_descriptor)
        _check_package_deadline(effective_deadline)
        with entries:
            children: list[os.DirEntry[str]] = []
            for child in entries:
                _check_package_deadline(effective_deadline)
                if len(children) >= remaining_entries:
                    raise LearnDeliveryError("Node package dependency tree is too large")
                children.append(child)
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    children.sort(key=lambda entry: entry.name)
    return children


def _open_package_root(root: Path) -> tuple[int, os.stat_result]:
    """Open and bind one package root without following its final name."""
    descriptor = -1
    try:
        named = root.lstat()
        descriptor = os.open(root, _PACKAGE_DIRECTORY_FLAGS)
        opened = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            _close_package_descriptors(
                (descriptor,),
                unavailable_message="Node package dependency tree is unavailable",
                preserve_error=True,
            )
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if not stat.S_ISDIR(named.st_mode) or (
        _package_entry_identity(named) != _package_entry_identity(opened)
    ):
        _close_package_descriptors(
            (descriptor,),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=True,
        )
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    return descriptor, opened


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
        _close_package_descriptors(
            (frame.descriptor,),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=primary_error,
        )


def _package_tree_records(
    root: Path,
    *,
    snapshot_root: Path | None = None,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    """Return deterministic records for every entry in one npm root."""
    if not _POSIX_PACKAGE_DESCRIPTOR_WALK:
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    effective_deadline = _package_deadline(deadline)
    _check_package_deadline(effective_deadline)
    root_descriptor, opened_root = _open_package_root(root)
    records: list[bytes] = [f"directory\0.\0{stat.S_IMODE(opened_root.st_mode):o}".encode()]
    frames = [_PackageDirectoryFrame(root_descriptor, Path(), [], opened_root)]
    total_bytes = 0
    primary_error = False
    try:
        frames[0].children = _bounded_package_children(
            root_descriptor, _MAX_PACKAGE_ENTRIES - len(records), effective_deadline
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
            record, size, child_directory = _package_tree_entry_record(
                root,
                frame.descriptor,
                frame.relative_parent,
                child,
                _MAX_PACKAGE_BYTES - total_bytes,
                snapshot_root,
                effective_deadline,
            )
            records.append(record)
            total_bytes += size
            if child_directory is not None:
                descriptor, relative_path, name, initial = child_directory
                try:
                    children = _bounded_package_children(
                        descriptor,
                        _MAX_PACKAGE_ENTRIES - len(records),
                        effective_deadline,
                    )
                except BaseException:
                    _close_package_descriptors(
                        (descriptor,),
                        unavailable_message="Node package dependency tree is unavailable",
                        preserve_error=True,
                    )
                    raise
                frames.append(
                    _PackageDirectoryFrame(descriptor, relative_path, children, initial, name)
                )
    except BaseException:
        primary_error = True
        raise
    finally:
        _close_package_descriptors(
            tuple(frame.descriptor for frame in frames),
            unavailable_message="Node package dependency tree is unavailable",
            preserve_error=primary_error,
        )
    try:
        current_root = root.lstat()
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    if _package_entry_identity(opened_root) != _package_entry_identity(current_root):
        raise LearnDeliveryError("Node package dependency tree is unavailable")
    return tuple(sorted(records))


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
    if package.name != "markdownlint-cli2" or root.name != "node_modules":
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    if not root.is_dir() or len(_cli_root_candidates(cli)) != 1:
        raise LearnDeliveryError("Node package CLI has an ambiguous root")
    deadline = time.monotonic() + _PACKAGE_WALK_TIMEOUT_S
    _manifest_bin_target(package, cli, deadline)
    try:
        snapshot_parent = Path(tempfile.mkdtemp(prefix="hephaestus-node-package-")).resolve()
    except OSError:
        raise LearnDeliveryError("Node package dependency tree is unavailable") from None
    snapshot_root = snapshot_parent / "node_modules"
    try:
        try:
            snapshot_root.mkdir(mode=0o700)
        except OSError:
            raise LearnDeliveryError("Node package dependency tree is unavailable") from None
        records = _package_tree_records(
            root,
            snapshot_root=snapshot_root,
            deadline=deadline,
        )
        _freeze_package_snapshot(snapshot_root, deadline)
        snapshot_cli = snapshot_root / cli.relative_to(root)
        if not snapshot_cli.is_file() or snapshot_cli.is_symlink():
            raise LearnDeliveryError("Node package dependency tree is unavailable")
        return NodePackageTree(
            root=root,
            digest=_package_records_digest(records),
            snapshot_root=snapshot_root,
            snapshot_cli=snapshot_cli,
            _snapshot_parent=snapshot_parent,
            _snapshot_digest=_package_tree_digest(snapshot_root, deadline=deadline),
        )
    except BaseException:
        with suppress(LearnDeliveryError):
            _remove_package_snapshot(snapshot_parent)
        raise
