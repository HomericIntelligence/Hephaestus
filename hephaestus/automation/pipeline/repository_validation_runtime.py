"""Admit the bounded manifest of an optional sealed Comet runtime.

A valid manifest does not prove that its files exist or that execution is safe.
The runtime adapter must verify those conditions before each local check.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from hephaestus.automation.pipeline_github_review_validation import (
    comet_local_check_ids,
    comet_profile_digest,
    comet_validation_checks,
)

from .repository_validation import (
    RepositoryValidationExecution,
    validate_repository_validation_execution,
)

_CAPABILITY_PATH = Path("build/hephaestus-review-validation/comet")

MANIFEST_BYTES_MAX = 16_777_216
RUNTIME_FILES_MAX = 50_000
RUNTIME_FILE_BYTES_MAX = 1_073_741_824
RUNTIME_TOTAL_BYTES_MAX = 8_589_934_592
RUNTIME_PATH_BYTES_MAX = 4096
_REQUIRED_EXECUTABLES = frozenset(
    f"bin/{name}" for name in ("uv", "python", "ruff", "ty", "mkdocs")
)
_HEADER = {
    "schema": "hephaestus-comet-review-runtime-v1",
    "profile": "comet",
    "repository": "llm360/comet",
    "python_version": "3.12",
    "uv_version": "0.12.7",
}
_MANIFEST_FIELDS = frozenset(
    (*_HEADER, "pyproject_sha256", "uv_lock_sha256", "entries", "tree_sha256")
)
_ENTRY_FIELDS = frozenset(("path", "size", "mode", "sha256"))


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    """Describe one regular file relative to the sealed environment."""

    path: str
    size: int
    mode: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Keep the parsed inventory and its content identities."""

    pyproject_sha256: str
    uv_lock_sha256: str
    entries: tuple[RuntimeFile, ...]
    tree_sha256: str
    manifest_sha256: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "The runtime manifest has a repeated key.")
        result[key] = value
    return result


def _json_constant(value: str) -> NoReturn:
    raise ValueError("The runtime manifest has a nonfinite number.")


def _runtime_path(path: object) -> bool:
    if type(path) is not str or not path or "\0" in path or "\\" in path:
        return False
    if len(path.encode("utf-8")) > RUNTIME_PATH_BYTES_MAX:
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if path.endswith(".pth"):
        return False
    for part in parts:
        for module in ("sitecustomize", "usercustomize"):
            if part == module or (
                part.startswith(module + ".")
                and part.endswith((".py", ".pyc", ".pyo", ".so", ".pyd"))
            ):
                return False
    return True


def _runtime_entries(value: object) -> tuple[RuntimeFile, ...]:
    _require(type(value) is list, "The runtime inventory must be an array.")
    value = cast(list[Any], value)
    _require(0 < len(value) <= RUNTIME_FILES_MAX, "The runtime file count is invalid.")
    entries: list[RuntimeFile] = []
    previous = ""
    total = 0
    executables: set[str] = set()
    for raw in value:
        _require(
            type(raw) is dict and raw.keys() == _ENTRY_FIELDS,
            "The runtime file fields are invalid.",
        )
        path, size, mode, digest = raw["path"], raw["size"], raw["mode"], raw["sha256"]
        _require(
            _runtime_path(path) and path > previous,
            "The runtime file path is invalid or unordered.",
        )
        _require(
            type(size) is int and 0 <= size <= RUNTIME_FILE_BYTES_MAX,
            "The runtime file size is invalid.",
        )
        _require(type(mode) is int and mode in {0o444, 0o555}, "The runtime file mode is invalid.")
        _require(_sha256(digest), "The runtime file digest is invalid.")
        total += size
        _require(total <= RUNTIME_TOTAL_BYTES_MAX, "The runtime size exceeds its limit.")
        if path in _REQUIRED_EXECUTABLES:
            _require(mode == 0o555, "A required runtime program is not executable.")
            executables.add(path)
        entries.append(RuntimeFile(path, size, mode, digest))
        previous = path
    _require(executables == _REQUIRED_EXECUTABLES, "A required runtime program is missing.")
    return tuple(entries)


def parse_runtime_manifest(
    raw: bytes, *, pyproject_sha256: str, uv_lock_sha256: str
) -> RuntimeManifest:
    """Validate canonical UTF-8 JSON against the bound project and lock digests.

    Canonical JSON uses sorted keys, compact separators, and unescaped Unicode.
    This function checks data only. It does not read or execute runtime files.
    """
    _require(
        type(raw) is bytes and len(raw) <= MANIFEST_BYTES_MAX,
        "The runtime manifest exceeds its byte limit.",
    )
    _require(
        _sha256(pyproject_sha256) and _sha256(uv_lock_sha256),
        "The required dependency digests are invalid.",
    )
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_json_constant
        )
        _require(
            type(data) is dict and data.keys() == _MANIFEST_FIELDS,
            "The runtime manifest fields are invalid.",
        )
        _require(
            all(data[key] == value for key, value in _HEADER.items()),
            "The runtime manifest profile is invalid.",
        )
        _require(
            data["pyproject_sha256"] == pyproject_sha256
            and data["uv_lock_sha256"] == uv_lock_sha256,
            "The runtime dependencies do not match the review.",
        )
        entries = _runtime_entries(data["entries"])
        tree_digest = data["tree_sha256"]
        _require(
            _sha256(tree_digest)
            and tree_digest == hashlib.sha256(_canonical(data["entries"])).hexdigest(),
            "The runtime inventory digest is invalid.",
        )
        _require(raw == _canonical(data), "The runtime manifest is not canonical JSON.")
    except (UnicodeError, RecursionError) as error:
        raise ValueError("The runtime manifest encoding or structure is invalid.") from error
    return RuntimeManifest(
        pyproject_sha256, uv_lock_sha256, entries, tree_digest, hashlib.sha256(raw).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class AdmittedRuntime:
    """Bind the fixed runtime root to the inventory checked on this call."""

    root: Path
    manifest: RuntimeManifest


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular_file(
    path: Path,
    *,
    mode: int,
    uid: int,
    max_size: int,
    check_deadline: Callable[[], None],
    retain_bytes: bool = False,
    expected_size: int | None = None,
) -> tuple[bytes, str]:
    """Read a bounded regular file without following its final path component."""
    check_deadline()
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode), "The runtime path is not a regular file.")
    _require(
        stat.S_IMODE(before.st_mode) == mode and before.st_uid == uid and before.st_nlink == 1,
        "The runtime file permissions, owner, or link count are invalid.",
    )
    _require(0 <= before.st_size <= max_size, "The runtime file exceeds its byte limit.")
    _require(
        expected_size is None or before.st_size == expected_size,
        "The runtime file size has changed.",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require(
            _file_identity(os.fstat(descriptor)) == _file_identity(before),
            "The runtime file changed before reading.",
        )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        size = 0
        while True:
            check_deadline()
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            _require(size <= before.st_size, "The runtime file grew during reading.")
            digest.update(chunk)
            if retain_bytes:
                chunks.append(chunk)
        _require(size == before.st_size, "The runtime file size changed during reading.")
        if path.suffix.casefold() in {".zip", ".egg"}:
            _check_import_archive(descriptor, size, check_deadline)
        _require(
            _file_identity(os.fstat(descriptor)) == _file_identity(before)
            and _file_identity(path.lstat()) == _file_identity(before),
            "The runtime file changed during reading.",
        )
        return b"".join(chunks), digest.hexdigest()
    finally:
        os.close(descriptor)


def _check_archive_directory(
    descriptor: int,
    end_offset: int,
    size: int,
    count: int,
    check_deadline: Callable[[], None],
) -> None:
    """Bound directory records before the ZIP reader allocates member objects."""
    _require(size <= end_offset, "The import archive directory offset is invalid.")
    directory = os.pread(descriptor, size, end_offset - size)
    _require(len(directory) == size, "The import archive directory is truncated.")
    position = 0
    observed = 0
    while position < size:
        check_deadline()
        _require(
            observed < count
            and position + 46 <= size
            and directory[position : position + 4] == b"PK\x01\x02",
            "The import archive directory record is invalid.",
        )
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", directory, position + 28)
        _require(name_size <= RUNTIME_PATH_BYTES_MAX, "The import archive path exceeds its limit.")
        position += 46 + name_size + extra_size + comment_size
        _require(position <= size, "The import archive directory record is truncated.")
        observed += 1
    _require(observed == count, "The import archive directory count is inconsistent.")


def _check_import_archive(descriptor: int, size: int, check_deadline: Callable[[], None]) -> None:
    """Inspect bounded archive member names before accepting an import archive."""
    check_deadline()
    tail = os.pread(descriptor, min(size, 65_557), max(0, size - 65_557))
    end = tail.rfind(b"PK\x05\x06")
    _require(end >= 0 and end + 22 <= len(tail), "The import archive end record is invalid.")
    _, disk, directory_disk, disk_count, count, directory_size, offset, comment_size = (
        struct.unpack("<4s4H2LH", tail[end : end + 22])
    )
    _require(
        disk == directory_disk == 0
        and disk_count == count
        and count <= RUNTIME_FILES_MAX
        and directory_size <= MANIFEST_BYTES_MAX
        and end + 22 + comment_size == len(tail)
        and offset + directory_size <= size - 22 - comment_size,
        "The import archive inventory exceeds its bounds or spans disks.",
    )
    end_offset = size - len(tail) + end
    _require(
        end_offset < 20 or os.pread(descriptor, 4, end_offset - 20) != b"PK\x06\x07",
        "The import archive uses an unsupported extended end record.",
    )
    _check_archive_directory(descriptor, end_offset, directory_size, count, check_deadline)
    check_deadline()
    try:
        with os.fdopen(os.dup(descriptor), "rb") as stream, zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            _require(len(members) == count, "The import archive member count is inconsistent.")
            paths: set[str] = set()
            total = 0
            for member in members:
                check_deadline()
                path = member.filename.removesuffix("/")
                _require(
                    _runtime_path(path) and path not in paths,
                    "The import archive contains an unsafe or repeated path.",
                )
                _require(
                    0 <= member.file_size <= RUNTIME_FILE_BYTES_MAX and member.flag_bits & 1 == 0,
                    "The import archive member is oversized or encrypted.",
                )
                total += member.file_size
                _require(
                    total <= RUNTIME_TOTAL_BYTES_MAX, "The import archive size exceeds its limit."
                )
                paths.add(path)
    except zipfile.BadZipFile as error:
        raise ValueError("The import archive structure is invalid.") from error
    check_deadline()


def _check_directory(path: Path, uid: int, *, parent: bool = False) -> os.stat_result:
    value = path.lstat()
    _require(
        stat.S_ISDIR(value.st_mode) and value.st_uid == uid,
        "The runtime directory type or owner is invalid.",
    )
    mode = stat.S_IMODE(value.st_mode)
    _require(
        mode == 0o700 if parent else mode & 0o222 == 0,
        "The runtime directory permissions are invalid.",
    )
    return value


def _check_runtime_files(
    environment: Path, manifest: RuntimeManifest, uid: int, check_deadline: Callable[[], None]
) -> None:
    expected = {entry.path: entry for entry in manifest.entries}
    observed: set[str] = set()
    first = _check_directory(environment, uid)
    stack = [(environment, os.scandir(environment), first)]
    try:
        while stack:
            check_deadline()
            directory, iterator, original = stack[-1]
            child = next(iterator, None)
            if child is None:
                iterator.close()
                _require(
                    _file_identity(directory.lstat()) == _file_identity(original),
                    "The runtime directory changed during admission.",
                )
                stack.pop()
                continue
            path = Path(child.path)
            relative = path.relative_to(environment).as_posix()
            _require(_runtime_path(relative), "The runtime path is invalid.")
            value = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode):
                directory_stat = _check_directory(path, uid)
                stack.append((path, os.scandir(path), directory_stat))
                continue
            _require(stat.S_ISREG(value.st_mode), "The runtime contains a nonregular file.")
            _require(
                relative in expected and relative not in observed,
                "The runtime contains an unlisted or repeated file.",
            )
            entry = expected[relative]
            _, digest = _read_regular_file(
                path,
                mode=entry.mode,
                uid=uid,
                max_size=RUNTIME_FILE_BYTES_MAX,
                expected_size=entry.size,
                check_deadline=check_deadline,
            )
            _require(digest == entry.sha256, "The runtime file digest has changed.")
            observed.add(relative)
        _require(observed == expected.keys(), "A listed runtime file is missing.")
    finally:
        for _, iterator, _ in stack:
            iterator.close()


def admit_runtime(
    trusted_root: Path,
    *,
    pyproject_sha256: str,
    uv_lock_sha256: str,
    timeout_s: float = 120.0,
    shutdown: threading.Event | None = None,
) -> AdmittedRuntime:
    """Verify the fixed capability without installation or subprocess execution.

    The caller supplies the host-owned Hephaestus root and bound source digests.
    The worker must still enforce source identity and execution isolation.
    """
    _require(
        type(timeout_s) in {int, float} and math.isfinite(timeout_s) and timeout_s > 0,
        "The runtime admission timeout is invalid.",
    )
    deadline = time.monotonic() + min(timeout_s, 120.0)

    def check_deadline() -> None:
        if shutdown is not None and shutdown.is_set():
            raise InterruptedError("Runtime admission was cancelled.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Runtime admission exceeded its deadline.")

    check_deadline()
    _require(
        _sha256(pyproject_sha256) and _sha256(uv_lock_sha256),
        "The runtime dependency digests are invalid.",
    )
    _require(
        isinstance(trusted_root, Path) and trusted_root.is_absolute(),
        "The trusted runtime root must be absolute.",
    )
    parent = trusted_root / _CAPABILITY_PATH
    root = parent / uv_lock_sha256
    uid = os.geteuid()
    try:
        for ancestor in (*reversed(root.parents), root):
            check_deadline()
            _require(
                stat.S_ISDIR(ancestor.lstat().st_mode),
                "A runtime ancestor is not a regular directory.",
            )
        parent_stat = _check_directory(parent, uid, parent=True)
        root_stat = _check_directory(root, uid)
        raw, manifest_digest = _read_regular_file(
            root / "runtime-manifest.json",
            mode=0o400,
            uid=uid,
            max_size=MANIFEST_BYTES_MAX,
            check_deadline=check_deadline,
            retain_bytes=True,
        )
        manifest = parse_runtime_manifest(
            raw, pyproject_sha256=pyproject_sha256, uv_lock_sha256=uv_lock_sha256
        )
        check_deadline()
        _require(
            manifest.manifest_sha256 == manifest_digest, "The runtime manifest digest changed."
        )
        _check_runtime_files(root / "environment", manifest, uid, check_deadline)
        _require(
            _file_identity(parent.lstat()) == _file_identity(parent_stat)
            and _file_identity(root.lstat()) == _file_identity(root_stat),
            "The runtime capability changed during admission.",
        )
        check_deadline()
        return AdmittedRuntime(root, manifest)
    except (TimeoutError, InterruptedError):
        raise
    except OSError as error:
        raise ValueError("The sealed runtime files are unavailable.") from error


def admit_execution_runtime(
    execution: RepositoryValidationExecution,
    *,
    trusted_root: Path,
    timeout_s: float = 120.0,
    shutdown: threading.Event | None = None,
) -> AdmittedRuntime:
    """Bind execution metadata to the fixed profile and the actual sealed runtime.

    This operation does not start a process. The worker must verify the source
    workspace and enforce isolation before it executes the selected command.
    """
    check = validate_repository_validation_execution(execution)
    plan = execution.plan
    _require(
        plan.profile_digest == comet_profile_digest(plan.profile_id)
        and plan.checks == comet_validation_checks(plan.profile_id, plan.changes),
        "The execution does not match the admitted Comet profile.",
    )
    _require(
        check.check_id in comet_local_check_ids(plan.checks),
        "The selected check is not available for local execution.",
    )
    sources = {path: digest for path, _, digest in check.source_digests}
    _require(
        isinstance(trusted_root, Path)
        and trusted_root.is_absolute()
        and execution.runtime_root == trusted_root / _CAPABILITY_PATH / sources["uv.lock"],
        "The execution runtime is outside the fixed host capability.",
    )
    runtime = admit_runtime(
        trusted_root,
        pyproject_sha256=sources["pyproject.toml"],
        uv_lock_sha256=sources["uv.lock"],
        timeout_s=timeout_s,
        shutdown=shutdown,
    )
    _require(
        runtime.manifest.manifest_sha256 == execution.runtime_manifest_sha256
        and runtime.manifest.tree_sha256 == execution.runtime_tree_sha256,
        "The admitted runtime does not match the execution metadata.",
    )
    return runtime
