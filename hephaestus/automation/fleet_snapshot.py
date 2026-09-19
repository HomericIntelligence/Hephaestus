"""Export, verify and restore bounded Fleet source snapshots."""

from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_snapshot_files import directory, private_parent, publish, remaining
from hephaestus.automation.fleet_snapshot_policy import (
    MANIFEST_SCHEMA,
    MAX_MANIFEST_BYTES,
    MAX_SCAN_ENTRIES,
    SnapshotError as SnapshotError,
    SnapshotPolicy as SnapshotPolicy,
    admit_member_path,
    canonical,
    digest,
    integer,
    sha,
    valid_path,
    validate_manifest,
)
from hephaestus.automation.git_config_safety import unsafe_local_git_config_key
from hephaestus.automation.git_runtime import current_operation_shutdown
from hephaestus.automation.worktree_snapshot import (
    isolated_checkout_git_env,
    path_content_identity,
    run_bounded_git_output,
    secure_dir_fd_supported,
    trusted_git_executable,
)


def _deadline(timeout: float) -> float:
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise SnapshotError("invalid snapshot deadline")
    deadline = time.monotonic() + timeout
    remaining(deadline)
    if not secure_dir_fd_supported():
        raise SnapshotError("secure snapshot filesystem operations are unavailable")
    return deadline


def _root(path: Path) -> Path:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise SnapshotError("snapshot root is not canonical")
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise SnapshotError("snapshot root is not a directory")
    return path


def _output(path: Path, source: Path) -> None:
    _root(path.parent)
    private_parent(path.parent.lstat())
    if (
        path.name in {"", ".", ".."}
        or path.is_relative_to(source)
        or path.exists()
        or path.is_symlink()
    ):
        raise SnapshotError("snapshot destination must be new and outside its source")


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _git(source: Path, arguments: tuple[str, ...], deadline: float) -> str:
    executable = trusted_git_executable()
    if executable is None:
        raise SnapshotError("trusted Git is unavailable")
    environment = isolated_checkout_git_env()
    # Inspect repository configuration explicitly. Global and system
    # configuration remain disabled by the controlled child environment.
    environment.pop("GIT_CONFIG", None)
    return run_bounded_git_output(
        (
            executable,
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.excludesFile={os.devnull}",
            f"--work-tree={source}",
            *arguments,
        ),
        cwd=source,
        timeout=remaining(deadline),
        max_bytes=MAX_MANIFEST_BYTES,
        retain_text=True,
        env=environment,
        shutdown=current_operation_shutdown(),
    ).text


def _admit_git_configuration(source: Path, deadline: float) -> None:
    """Check local and linked-worktree settings before source inventory."""
    configuration = _git(
        source, ("config", "--local", "--no-includes", "--null", "--list"), deadline
    )
    if unsafe_local_git_config_key(configuration) is not None:
        raise SnapshotError("source has unsafe local Git configuration")
    worktree_config = _git(
        source,
        (
            "config",
            "--local",
            "--no-includes",
            "--type=bool",
            "--default=false",
            "--get",
            "extensions.worktreeConfig",
        ),
        deadline,
    ).strip()
    if worktree_config == "true":
        configuration = _git(
            source, ("config", "--worktree", "--no-includes", "--null", "--list"), deadline
        )
        if unsafe_local_git_config_key(configuration) is not None:
            raise SnapshotError("source has unsafe local Git configuration")


def _records(text: str) -> list[str]:
    if text and (not text.endswith("\0") or "\0\0" in text):
        raise SnapshotError("invalid Git path records")
    return text[:-1].split("\0") if text else []


def _tracked_paths(index: str, policy: SnapshotPolicy) -> set[str]:
    tracked: set[str] = set()
    for record in _records(index):
        header, separator, path = record.partition("\t")
        fields = header.split(" ")
        if not separator or len(fields) != 3 or fields[0] not in {"100644", "100755"}:
            raise SnapshotError("source contains unsupported Git entries")
        if fields[2] != "0":
            raise SnapshotError("source index is unmerged")
        if not valid_path(path) or policy.excludes(path):
            raise SnapshotError("tracked source contains a private or unsupported path")
        tracked.add(path)
    return tracked


def _ignored(source: Path, names: list[str], deadline: float) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        try:
            _git(source, ("check-ignore", "--no-index", "--quiet", "--", name), deadline)
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
        else:
            ignored.add(name)
    return ignored


def _scan_paths(
    source: Path, tracked: set[str], policy: SnapshotPolicy, deadline: float
) -> set[str]:
    """Walk real entries, including specials Git's untracked listing omits."""
    ancestors = {
        "/".join(name.split("/")[:index])
        for name in tracked
        for index in range(1, len(name.split("/")))
    }
    known = tracked | ancestors
    selected: set[str] = set()
    scanned = 0

    def visit(descriptor: int, prefix: str) -> None:
        nonlocal scanned
        before = _identity(os.fstat(descriptor))
        names: list[str] = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                remaining(deadline)
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    raise SnapshotError("snapshot filesystem inventory limit exceeded")
                relative = prefix + entry.name
                if not policy.excludes(relative):
                    names.append(relative)
        ignored = _ignored(source, [name for name in names if name not in known], deadline)
        for name in sorted(set(names) - ignored):
            remaining(deadline)
            if not valid_path(name):
                raise SnapshotError("untracked source contains an unsupported path")
            leaf = name.split("/")[-1]
            metadata = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    leaf,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                try:
                    visit(child, name + "/")
                    if _identity(os.fstat(child)) != _identity(
                        os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
                    ):
                        raise SnapshotError("source directory changed during inventory")
                finally:
                    os.close(child)
            else:
                selected.add(name)
        if _identity(os.fstat(descriptor)) != before:
            raise SnapshotError("source directory changed during inventory")

    with directory(source, deadline) as descriptor:
        visit(descriptor, "")
    return selected | tracked


def _present_paths(
    source: Path, selected: set[str], tracked: set[str], policy: SnapshotPolicy, deadline: float
) -> tuple[str, ...]:
    present: list[str] = []
    total = 0
    names: dict[str, tuple[str, bool]] = {}
    for name in sorted(selected):
        remaining(deadline)
        location = source
        try:
            for part in name.split("/")[:-1]:
                location /= part
                if not stat.S_ISDIR(location.lstat().st_mode):
                    raise SnapshotError("source path has an unsupported ancestor")
            metadata = (source / name).lstat()
        except FileNotFoundError:
            if name not in tracked:
                raise SnapshotError("untracked source changed during capture") from None
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & ~0o777
        ):
            raise SnapshotError("source contains an unsupported file type or mode")
        admit_member_path(name, names)
        total += metadata.st_size
        present.append(name)
        if total > policy.max_bytes or len(present) > policy.max_members:
            raise SnapshotError("snapshot member or content limit exceeded")
    if not present or total == 0:
        raise SnapshotError("empty source snapshot")
    return tuple(present)


def _inventory(
    source: Path, policy: SnapshotPolicy, deadline: float
) -> tuple[str, str, tuple[str, ...]]:
    admin = source / ".git"
    if admin.is_symlink() or not (admin.is_file() or admin.is_dir()):
        raise SnapshotError("source is not a Git worktree root")
    _admit_git_configuration(source, deadline)
    head = _git(source, ("rev-parse", "--verify", "HEAD^{commit}"), deadline).strip()
    if not sha(head, 40):
        raise SnapshotError("source base commit is invalid")
    index = _git(source, ("ls-files", "--stage", "-z"), deadline)
    tracked = _tracked_paths(index, policy)
    selected = _scan_paths(source, tracked, policy, deadline)
    present = _present_paths(source, selected, tracked, policy, deadline)
    return head, digest(index.encode("utf-8", "surrogateescape")), present


def _read_regular(root: Path, name: str, limit: int, deadline: float) -> tuple[bytes, int]:
    """Read a regular leaf through held directory descriptors without links."""
    with directory(root, deadline) as descriptor:
        return _read_regular_at(root, descriptor, name, limit, deadline)


def _read_regular_at(
    root: Path, descriptor: int, name: str, limit: int, deadline: float
) -> tuple[bytes, int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = [os.dup(descriptor)]
    root_id = _identity(os.fstat(descriptors[0]))
    bindings: list[tuple[int, str, tuple[int, ...]]] = []
    try:
        for part in name.split("/")[:-1]:
            parent = descriptors[-1]
            descriptor = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(descriptor)
            bindings.append((parent, part, _identity(os.fstat(descriptor))))
        parent = descriptors[-1]
        leaf = name.split("/")[-1]
        descriptor = os.open(leaf, flags | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise SnapshotError("invalid snapshot artifact file")
        parts: list[bytes] = []
        size = 0
        while True:
            remaining(deadline)
            block = os.read(descriptor, min(65536, limit - size + 1))
            if not block:
                break
            size += len(block)
            if size > limit:
                raise SnapshotError("snapshot content limit exceeded")
            parts.append(block)
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(before) != _identity(
            os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        ):
            raise SnapshotError("snapshot content changed during read")
        for parent, part, expected in bindings:
            if _identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) != expected:
                raise SnapshotError("snapshot directory changed during read")
        if _identity(root.lstat()) != root_id:
            raise SnapshotError("snapshot root changed during read")
        return b"".join(parts), stat.S_IMODE(before.st_mode)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _tar(entries: list[dict[str, Any]], contents: list[bytes], deadline: float) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry, data in zip(entries, contents, strict=True):
            remaining(deadline)
            member = tarfile.TarInfo(entry["path"])
            member.mode = entry["mode"]
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def export_snapshot(
    source: Path,
    artifact: Path,
    *,
    reference: str,
    policy: SnapshotPolicy,
    timeout: float = 30,
) -> dict[str, str | int]:
    """Capture working source; return metadata for a separate admission service."""
    try:
        deadline = _deadline(timeout)
        source = _root(source)
        _output(artifact, source)
        if (
            not isinstance(reference, str)
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", reference) is None
        ):
            raise SnapshotError("invalid private snapshot reference")
        before = _inventory(source, policy, deadline)
        paths = "\0".join(before[2]) + "\0"
        identity = path_content_identity(
            source, paths, include_file_content=False, timeout=remaining(deadline)
        )
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=artifact.parent) as temporary:
            stage = Path(temporary)
            path_content_identity(
                source,
                paths,
                copy_root=stage,
                remaining_content_bytes=[policy.max_bytes],
                timeout=remaining(deadline),
            )
            if (
                _inventory(source, policy, deadline) != before
                or path_content_identity(
                    source, paths, include_file_content=False, timeout=remaining(deadline)
                )
                != identity
            ):
                raise SnapshotError("source changed during snapshot capture")
            actual = tuple(
                sorted(
                    path.relative_to(stage).as_posix()
                    for path in stage.rglob("*")
                    if not path.is_dir() or path.is_symlink()
                )
            )
            if actual != before[2]:
                raise SnapshotError("captured source membership changed")
            entries: list[dict[str, Any]] = []
            contents: list[bytes] = []
            total = 0
            for name in actual:
                data, mode = _read_regular(stage, name, policy.max_bytes - total, deadline)
                total += len(data)
                entries.append(
                    {"path": name, "mode": mode, "size": len(data), "sha256": digest(data)}
                )
                contents.append(data)
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "baseCommit": before[0],
                "policyDigest": policy.digest,
                "files": entries,
            }
            validate_manifest(manifest, policy)
            encoded = canonical(manifest)
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise SnapshotError("snapshot manifest limit exceeded")
            archive = _tar(entries, contents, deadline)
            remaining(deadline)
            publish(
                artifact,
                {"source.tar": (archive, 0o600), "manifest.json": (encoded, 0o600)},
                deadline,
            )
        return {
            "reference": reference,
            "manifestDigest": digest(encoded),
            "baseCommit": before[0],
            "members": len(entries),
            "bytes": total,
            "policyDigest": policy.digest,
        }
    except SnapshotError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SnapshotError("source snapshot capture failed") from exc


def _decode(
    encoded: bytes,
    raw: bytes,
    commitment: Mapping[str, object],
    policy: SnapshotPolicy,
    deadline: float,
) -> tuple[dict[str, Any], list[bytes]]:
    manifest = validate_manifest(json.loads(encoded), policy)
    if canonical(manifest) != encoded:
        raise SnapshotError("snapshot manifest is not canonical")
    if (
        set(commitment)
        != {"reference", "manifestDigest", "baseCommit", "members", "bytes", "policyDigest"}
        or not isinstance(commitment["reference"], str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", commitment["reference"]) is None
        or commitment["manifestDigest"] != digest(encoded)
        or commitment["baseCommit"] != manifest["baseCommit"]
        or commitment["policyDigest"] != policy.digest
        or not integer(commitment["members"], 1, policy.max_members)
        or commitment["members"] != len(manifest["files"])
        or not integer(commitment["bytes"], 1, policy.max_bytes)
        or commitment["bytes"] != sum(entry["size"] for entry in manifest["files"])
    ):
        raise SnapshotError("snapshot commitment does not match its manifest")
    contents: list[bytes] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for entry in manifest["files"]:
            remaining(deadline)
            member = archive.next()
            if (
                member is None
                or not member.isreg()
                or member.name != entry["path"]
                or member.mode != entry["mode"]
                or member.size != entry["size"]
                or member.pax_headers
                or member.linkname
            ):
                raise SnapshotError("snapshot archive member does not match its manifest")
            stream = archive.extractfile(member)
            if stream is None:
                raise SnapshotError("snapshot archive member is unreadable")
            with stream:
                data = stream.read(entry["size"] + 1)
            if len(data) != entry["size"] or digest(data) != entry["sha256"]:
                raise SnapshotError("snapshot archive content digest mismatch")
            contents.append(data)
        if archive.next() is not None:
            raise SnapshotError("snapshot archive contains extra members")
    if _tar(manifest["files"], contents, deadline) != raw:
        raise SnapshotError("snapshot archive is not canonical")
    return manifest, contents


def _artifact_identity(artifact: Path, deadline: float) -> str:
    pending = {"manifest.json", "source.tar"}
    with directory(artifact, deadline) as descriptor:
        remaining(deadline)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                remaining(deadline)
                if entry.name not in pending:
                    raise SnapshotError("snapshot artifact has unexpected members")
                pending.remove(entry.name)
    if pending:
        raise SnapshotError("snapshot artifact has unexpected members")
    return path_content_identity(
        artifact,
        "manifest.json\0source.tar\0",
        include_file_content=False,
        timeout=remaining(deadline),
    )


def _read_snapshot(
    artifact: Path,
    commitment: Mapping[str, object],
    policy: SnapshotPolicy,
    deadline: float,
) -> tuple[dict[str, Any], list[bytes]]:
    artifact_identity = _artifact_identity(artifact, deadline)
    encoded, _ = _read_regular(artifact, "manifest.json", MAX_MANIFEST_BYTES, deadline)
    archive_limit = policy.max_bytes + policy.max_members * 1024 + 10240
    raw, _ = _read_regular(artifact, "source.tar", archive_limit, deadline)
    manifest, contents = _decode(encoded, raw, commitment, policy, deadline)
    if _artifact_identity(artifact, deadline) != artifact_identity:
        raise SnapshotError("snapshot artifact changed between reads")
    return manifest, contents


def verify_snapshot(
    artifact: Path,
    *,
    commitment: Mapping[str, object],
    policy: SnapshotPolicy,
    timeout: float = 30,
) -> None:
    """Check actual snapshot bytes without creating a destination."""
    try:
        deadline = _deadline(timeout)
        _read_snapshot(_root(artifact), commitment, policy, deadline)
        remaining(deadline)
    except SnapshotError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        raise SnapshotError("source snapshot verification failed") from exc


def restore_snapshot(
    artifact: Path,
    destination: Path,
    *,
    commitment: Mapping[str, object],
    policy: SnapshotPolicy,
    timeout: float = 30,
) -> Path:
    """Check actual source bytes and modes before returning a new private tree."""
    try:
        deadline = _deadline(timeout)
        artifact = _root(artifact)
        _output(destination, artifact)
        manifest, contents = _read_snapshot(artifact, commitment, policy, deadline)
        publish(
            destination,
            {
                entry["path"]: (data, entry["mode"])
                for entry, data in zip(manifest["files"], contents, strict=True)
            },
            deadline,
        )
        return destination
    except SnapshotError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        raise SnapshotError("source snapshot restore failed") from exc
