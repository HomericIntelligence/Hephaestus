"""Real source and artifact behavior for the finite Fleet snapshot profile."""

from __future__ import annotations

import errno
import hashlib
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import fleet_snapshot
from hephaestus.automation.fleet_snapshot import (
    SnapshotError,
    SnapshotPolicy,
    export_snapshot,
    restore_snapshot,
    verify_snapshot,
)
from hephaestus.automation.git_config_safety import unsafe_local_git_config_key
from hephaestus.automation.git_runtime import operation_deadline
from hephaestus.config.child_environments import build_git_child_env

pytestmark = [
    pytest.mark.requires_posix,
    pytest.mark.skipif(os.name != "posix", reason="Snapshot capture requires POSIX descriptors."),
]


def git(root: Path, *arguments: str) -> str:
    """Use real Git only in the test's private repository."""
    env = build_git_child_env()
    env.update(
        GIT_AUTHOR_NAME="Snapshot fixture",
        GIT_AUTHOR_EMAIL="snapshot@example.invalid",
        GIT_COMMITTER_NAME="Snapshot fixture",
        GIT_COMMITTER_EMAIL="snapshot@example.invalid",
    )
    return subprocess.check_output(
        ["git", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=root,
        env=env,
        text=True,
        stderr=subprocess.PIPE,
        timeout=10,
    ).strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Keep tracked, dirty, deleted, staged and ignored source in a real index."""
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "--template=")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "recipe.txt").write_text("committed\n")
    (root / "remove.txt").write_text("deleted\n")
    (root / "mode.sh").write_text("printf snapshot\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    (root / "recipe.txt").write_text("staged\n")
    git(root, "add", "recipe.txt")
    (root / "recipe.txt").write_text("working\n")
    (root / "remove.txt").unlink()
    (root / "mode.sh").chmod(0o751)
    (root / "new.txt").write_bytes(b"untracked\x00content\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "private").write_text("must not transfer")
    return root


def capture(source: Path, tmp_path: Path) -> tuple[Path, dict[str, Any], SnapshotPolicy]:
    """Use the public exporter before exercising a receiver failure."""
    policy = SnapshotPolicy(max_members=20, max_bytes=8192)
    artifact = tmp_path / "artifact"
    commitment = export_snapshot(source, artifact, reference="snapshot-1", policy=policy)
    return artifact, commitment, policy


def tree_state(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Record fixture membership, modes and bytes without using access times."""
    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat().st_mode),
            None if path.is_dir() else path.read_bytes(),
        )
        for path in (root, *root.rglob("*"))
    }


def receive_snapshot(
    operation: str,
    artifact: Path,
    destination: Path,
    commitment: dict[str, Any],
    policy: SnapshotPolicy,
    *,
    timeout: float = 30,
) -> None:
    """Use either public receiver with the same actual artifact and commitment."""
    if operation == "verify":
        verify_snapshot(artifact, commitment=commitment, policy=policy, timeout=timeout)
    else:
        restore_snapshot(
            artifact, destination, commitment=commitment, policy=policy, timeout=timeout
        )


def test_verify_checks_actual_snapshot_without_creating_files(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification leaves source and artifact bytes, modes and membership unchanged."""
    artifact, commitment, policy = capture(source, tmp_path)
    before = tree_state(tmp_path)
    real_os_open = os.open
    real_io_open = io.open

    def read_only_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR)
        return real_os_open(path, flags, *args, **kwargs)

    def read_only_stream(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        assert not set(mode) & set("wax+")
        return real_io_open(path, mode, *args, **kwargs)

    def unexpected_directory(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("snapshot verification created a directory")

    assert real_os_open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", read_only_open)
        patch.setattr(
            os, "supports_dir_fd", os.supports_dir_fd | {read_only_open, unexpected_directory}
        )
        patch.setattr(io, "open", read_only_stream)
        patch.setattr("builtins.open", read_only_stream)
        patch.setattr(os, "mkdir", unexpected_directory)
        verify_snapshot(artifact, commitment=commitment, policy=policy)
    assert tree_state(tmp_path) == before


def test_dirty_source_round_trip_is_complete_deterministic_and_private(
    source: Path, tmp_path: Path
) -> None:
    """The restored source uses working bytes, real modes and the exact file set."""
    artifact, commitment, policy = capture(source, tmp_path)
    second = tmp_path / "second"
    repeated = export_snapshot(source, second, reference="snapshot-1", policy=policy)
    assert repeated == commitment
    assert (artifact / "source.tar").read_bytes() == (second / "source.tar").read_bytes()
    manifest_bytes = (artifact / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert (
        manifest_bytes
        == (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
    )
    assert commitment["manifestDigest"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert commitment["baseCommit"] == git(source, "rev-parse", "HEAD")
    assert commitment["members"] == 4
    assert commitment["bytes"] == sum(entry["size"] for entry in manifest["files"])
    # Retain real producer outputs outside the artifact's exact two-file boundary.
    (tmp_path / "commitment.json").write_text(
        json.dumps(commitment, sort_keys=True, separators=(",", ":")) + "\n"
    )
    (tmp_path / "policy.json").write_text(
        json.dumps(policy.document(), sort_keys=True, separators=(",", ":")) + "\n"
    )
    destination = tmp_path / "restored"
    assert (
        restore_snapshot(artifact, destination, commitment=commitment, policy=policy) == destination
    )
    assert sorted(path.name for path in destination.iterdir()) == [
        ".gitignore",
        "mode.sh",
        "new.txt",
        "recipe.txt",
    ]
    assert (destination / "recipe.txt").read_text() == "working\n"
    assert (destination / "new.txt").read_bytes() == b"untracked\x00content\n"
    assert stat.S_IMODE((destination / "mode.sh").stat().st_mode) == 0o751
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_assume_unchanged_and_staged_delete_present_bytes_are_captured(
    source: Path, tmp_path: Path
) -> None:
    """Git status hints cannot hide current bytes from the full inventory."""
    git(source, "update-index", "--assume-unchanged", "mode.sh")
    (source / "mode.sh").write_text("new mode content\n")
    git(source, "rm", "--cached", "--force", "recipe.txt")
    artifact, commitment, policy = capture(source, tmp_path)
    destination = tmp_path / "restored"
    restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
    assert (destination / "mode.sh").read_text() == "new mode content\n"
    assert (destination / "recipe.txt").read_text() == "working\n"


def test_excluded_untracked_runtime_and_credentials_are_not_read(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private roots are removed from selection before content capture."""
    for relative in [".env", ".fleet-runtime/socket", ".codex/auth.json", "private/key"]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not source")
    policy = SnapshotPolicy(20, 8192, private_paths=("private",))
    read = os.read

    def observe(descriptor: int, count: int) -> bytes:
        data = read(descriptor, count)
        assert b"not source" not in data
        return data

    monkeypatch.setattr(os, "read", observe)
    artifact = tmp_path / "artifact"
    commitment = export_snapshot(source, artifact, reference="snapshot-1", policy=policy)
    restore_snapshot(artifact, tmp_path / "restored", commitment=commitment, policy=policy)
    assert commitment["members"] == 4


@pytest.mark.parametrize("relative", [".env", ".fleet-runtime/state", ".codex/auth.json"])
def test_tracked_private_source_is_rejected_before_export(
    source: Path, tmp_path: Path, relative: str
) -> None:
    """Tracked exclusions fail instead of silently changing the source tree."""
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private")
    git(source, "add", "--force", relative)
    with pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert not (tmp_path / "artifact" / "manifest.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "ancestor", "hardlink", "fifo", "gitlink"])
def test_unsupported_source_cannot_produce_an_artifact(
    source: Path, tmp_path: Path, kind: str
) -> None:
    """Real unsupported filesystem and Git entries cannot disappear from selection."""
    outside = tmp_path / "outside"
    outside.write_text("outside secret")
    if kind == "symlink":
        (source / "link").symlink_to(outside)
    elif kind == "ancestor":
        (source / "directory").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, source / "alias")
    elif kind == "fifo":
        os.mkfifo(source / "pipe")
        assert stat.S_ISFIFO((source / "pipe").lstat().st_mode)
    elif kind == "gitlink":
        git(
            source,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{git(source, 'rev-parse', 'HEAD')},nested",
        )
    with pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert outside.read_text() == "outside secret"
    assert not (tmp_path / "artifact" / "manifest.json").exists()


def test_special_bits_are_rejected_at_the_metadata_boundary(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control the metadata boundary because this host strips requested setuid bits."""
    original = Path.lstat
    observed = False

    def metadata(path: Path) -> os.stat_result:
        nonlocal observed
        result = original(path)
        if path == source / "mode.sh":
            observed = True
            fields = list(result)
            fields[0] = stat.S_IFREG | 0o4755
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert observed
    assert not (tmp_path / "artifact" / "manifest.json").exists()


@pytest.mark.parametrize("members,byte_limit", [(1, 8192), (20, 1), (True, 8192), (20, 1.0)])
def test_source_bounds_reject_without_a_published_manifest(
    source: Path, tmp_path: Path, members: Any, byte_limit: Any
) -> None:
    """The declared resource limits reject invalid or excessive source."""
    with pytest.raises((SnapshotError, ValueError)):
        policy = SnapshotPolicy(members, byte_limit)
        export_snapshot(source, tmp_path / "artifact", reference="snapshot-1", policy=policy)
    assert not (tmp_path / "artifact" / "manifest.json").exists()


@pytest.mark.parametrize(
    "field", ["reference", "manifestDigest", "baseCommit", "members", "bytes", "policyDigest"]
)
@pytest.mark.parametrize("operation", ["restore", "verify"])
def test_changed_commitment_is_not_content_verification(
    source: Path, tmp_path: Path, field: str, operation: str
) -> None:
    """A modified admission commitment does not verify an artifact."""
    artifact, commitment, policy = capture(source, tmp_path)
    commitment[field] = "../foreign" if field == "reference" else 0
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "corruption",
    ["content", "mode", "extra", "duplicate", "traversal", "link", "missing", "malformed"],
)
@pytest.mark.parametrize("operation", ["restore", "verify"])
def test_receiver_rejects_corrupt_actual_archive(
    source: Path, tmp_path: Path, corruption: str, operation: str
) -> None:
    """Actual archive modifications must fail before destination publication."""
    artifact, commitment, policy = capture(source, tmp_path)
    archive = artifact / "source.tar"
    with tarfile.open(archive) as reader:
        members = []
        for item in reader.getmembers():
            stream = reader.extractfile(item)
            assert stream is not None
            members.append((item, stream.read()))
    if corruption == "content":
        item, data = members[0]
        members[0] = (item, b"X" * len(data))
    elif corruption == "mode":
        members[0][0].mode ^= 0o100
    elif corruption in {"extra", "traversal", "link"}:
        item = tarfile.TarInfo("../escape" if corruption == "traversal" else "extra")
        item.size = 1
        if corruption == "link":
            item.type = tarfile.SYMTYPE
            item.linkname = "../outside"
            item.size = 0
        members.append((item, b"x" if item.size else b""))
    elif corruption == "duplicate":
        members.append(members[0])
    elif corruption == "missing":
        members.pop()
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as writer:
        for item, data in members:
            writer.addfile(item, io.BytesIO(data))
    if corruption == "malformed":
        archive.write_bytes(archive.read_bytes()[:10])
    before = tree_state(tmp_path)
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "restored").exists()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("operation", ["restore", "verify"])
@pytest.mark.parametrize(
    "corruption",
    ["utf8", "json", "whitespace", "duplicate", "schema", "shape", "boolean", "path", "digest"],
)
def test_receiver_rejects_corrupt_manifest_bytes(
    source: Path, tmp_path: Path, operation: str, corruption: str
) -> None:
    """A matching manifest hash cannot hide invalid metadata or changed file digests."""
    artifact, commitment, policy = capture(source, tmp_path)
    path = artifact / "manifest.json"
    original = path.read_bytes()
    manifest = json.loads(original)
    if corruption == "utf8":
        data = b"\xff"
    elif corruption == "json":
        data = b"{"
    elif corruption == "whitespace":
        data = original + b" "
    elif corruption == "duplicate":
        data = original.replace(b'"schema":', b'"schema":"invalid","schema":', 1)
        assert data != original
    else:
        if corruption == "schema":
            manifest["schema"] = "invalid"
        elif corruption == "shape":
            del manifest["baseCommit"]
        elif corruption == "boolean":
            manifest["files"][0]["size"] = True
        elif corruption == "path":
            manifest["files"][0]["path"] = "../escape"
        elif corruption == "digest":
            manifest["files"][0]["sha256"] = "0" * 64
        data = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
    path.write_bytes(data)
    commitment["manifestDigest"] = hashlib.sha256(data).hexdigest()
    before = tree_state(tmp_path)
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("operation", ["restore", "verify"])
@pytest.mark.parametrize(
    "field",
    ["manifestDigest", "baseCommit", "members", "bytes", "policyDigest", "extra", "missing"],
)
def test_receiver_rejects_well_formed_but_different_commitment(
    source: Path, tmp_path: Path, operation: str, field: str
) -> None:
    """The retained commitment must match the actual artifact, including its fields."""
    artifact, commitment, policy = capture(source, tmp_path)
    if field == "extra":
        commitment["extra"] = "value"
    elif field == "missing":
        del commitment["reference"]
    elif field in {"members", "bytes"}:
        commitment[field] += 1
    else:
        commitment[field] = "0" * (40 if field == "baseCommit" else 64)
    before = tree_state(tmp_path)
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert tree_state(tmp_path) == before


def test_verification_does_not_infer_the_opaque_reference_registration(
    source: Path, tmp_path: Path
) -> None:
    """Registration owns the reference; the manifest binds the source bytes."""
    artifact, commitment, policy = capture(source, tmp_path)
    commitment["reference"] = "another-registered-reference"
    before = tree_state(tmp_path)
    verify_snapshot(artifact, commitment=commitment, policy=policy)
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("operation", ["restore", "verify"])
@pytest.mark.parametrize("name", ["manifest.json", "source.tar"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_receiver_rejects_linked_artifact_files(
    source: Path, tmp_path: Path, operation: str, name: str, kind: str
) -> None:
    """Matching content through a link cannot supply a regular owned artifact."""
    artifact, commitment, policy = capture(source, tmp_path)
    path = artifact / name
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    if kind == "symlink":
        path.symlink_to(outside)
    else:
        os.link(outside, path)
    before = tree_state(tmp_path)
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert tree_state(tmp_path) == before


def test_receiver_does_not_overwrite_existing_destination(source: Path, tmp_path: Path) -> None:
    """An existing destination remains owned by its original caller."""
    artifact, commitment, policy = capture(source, tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "sentinel").write_text("keep")
    with pytest.raises(SnapshotError):
        restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
    assert (destination / "sentinel").read_text() == "keep"


def test_changed_file_during_capture_is_not_a_snapshot(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real source mutation during content reading invalidates capture."""
    read = os.read
    changed = False

    def mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = read(descriptor, count)
        if data == b"working\n" and not changed:
            changed = True
            (source / "recipe.txt").write_text("later bytes\n")
        return data

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert changed
    assert not (tmp_path / "artifact" / "manifest.json").exists()


def test_expired_capture_does_not_start_git(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired operation fails before dispatching any Git subprocess."""
    dispatched = False

    def unexpected_git(*args: Any, **kwargs: Any) -> Any:
        nonlocal dispatched
        dispatched = True
        raise AssertionError("expired capture dispatched Git")

    monkeypatch.setattr(fleet_snapshot, "run_bounded_git_output", unexpected_git)
    with pytest.raises((SnapshotError, ValueError)):
        export_snapshot(
            source,
            tmp_path / "artifact",
            reference="snapshot-1",
            policy=SnapshotPolicy(20, 8192),
            timeout=0,
        )
    assert not (tmp_path / "artifact").exists()
    assert not dispatched


@pytest.mark.parametrize("timeout", [0, -1, True, 301, float("inf"), float("nan"), "30"])
def test_invalid_verification_timeout_cannot_read_artifact_bytes(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: Any
) -> None:
    """An invalid time budget fails before the first artifact read."""
    artifact, commitment, policy = capture(source, tmp_path)
    before = tree_state(tmp_path)

    def unexpected_read(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("invalid verification budget read artifact bytes")

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", unexpected_read)
        with pytest.raises(SnapshotError):
            verify_snapshot(artifact, commitment=commitment, policy=policy, timeout=timeout)
    assert tree_state(tmp_path) == before


def test_verification_keeps_one_deadline_during_actual_reads(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry during an archive read prevents a successful verification result."""
    artifact, commitment, policy = capture(source, tmp_path)
    before = tree_state(tmp_path)
    original = os.read
    expired = False

    def read(descriptor: int, count: int) -> bytes:
        nonlocal expired
        data = original(descriptor, count)
        if b"ustar\x00" in data:
            expired = True
        return data

    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: 20.0 if expired else 10.0)
        patch.setattr(os, "read", read)
        with pytest.raises(SnapshotError):
            verify_snapshot(artifact, commitment=commitment, policy=policy, timeout=5)
    assert expired
    assert tree_state(tmp_path) == before


def test_member_limit_counts_only_present_files(source: Path, tmp_path: Path) -> None:
    """A deleted base path does not consume the quota for four actual files."""
    policy = SnapshotPolicy(4, 8192)
    artifact = tmp_path / "artifact"
    commitment = export_snapshot(source, artifact, reference="snapshot-1", policy=policy)
    assert commitment["members"] == 4
    destination = tmp_path / "restored"
    restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
    assert sorted(path.name for path in destination.iterdir()) == [
        ".gitignore",
        "mode.sh",
        "new.txt",
        "recipe.txt",
    ]


def test_eligible_non_ascii_source_is_explicitly_rejected(source: Path, tmp_path: Path) -> None:
    """An eligible unsupported name fails instead of vanishing from the source."""
    (source / "caf\u00e9.py").write_text("selected source")
    with pytest.raises(SnapshotError, match="unsupported path"):
        capture(source, tmp_path)
    assert not (tmp_path / "artifact").exists()


@pytest.mark.parametrize("operation", ["restore", "verify"])
def test_artifact_change_between_reads_is_rejected(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The two actual artifact reads must belong to one stable capture."""
    artifact, commitment, policy = capture(source, tmp_path)
    original = os.read
    changed = False

    def mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = original(descriptor, count)
        if b"ustar\x00" in data and not changed:
            changed = True
            manifest = artifact / "manifest.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")
        return data

    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(SnapshotError):
        receive_snapshot(operation, artifact, tmp_path / "restored", commitment, policy)
    assert changed
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("operation", ["export", "restore"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_publication_keeps_ownership_when_destination_is_replaced(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    replacement: str,
) -> None:
    """A real root replacement cannot redirect writes or delete the foreign tree."""
    artifact, commitment, policy = capture(source, tmp_path)
    target = tmp_path / "new-output"
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "sentinel").write_text("keep outside")
    moved = tmp_path / "owned-moved"
    real_io_open = io.open
    real_os_open = os.open
    changed = False

    def replace() -> None:
        nonlocal changed
        if changed or not target.exists():
            return
        changed = True
        target.rename(moved)
        if replacement == "symlink":
            target.symlink_to(outside, target_is_directory=True)
        else:
            target.mkdir()
            (target / "foreign-sentinel").write_text("keep foreign")
            raise OSError("controlled write failure after replacement")

    def io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "x" in mode:
            replace()
        return real_io_open(file, mode, *args, **kwargs)

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            replace()
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(io, "open", io_open)
    assert real_os_open in os.supports_dir_fd
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os_open})
    with pytest.raises(SnapshotError):
        if operation == "export":
            export_snapshot(source, target, reference="snapshot-1", policy=policy)
        else:
            restore_snapshot(artifact, target, commitment=commitment, policy=policy)
    assert changed
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert (outside / "sentinel").read_text() == "keep outside"
    if replacement == "directory":
        assert (target / "foreign-sentinel").read_text() == "keep foreign"


def test_restore_keeps_ownership_when_an_ancestor_is_replaced(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced nested directory cannot redirect an actual restored file write."""
    (source / "nested").mkdir()
    (source / "nested" / "data.txt").write_text("nested content")
    artifact, commitment, policy = capture(source, tmp_path)
    target = tmp_path / "restored"
    nested = target / "nested"
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "sentinel").write_text("keep outside")
    real_io_open = io.open
    real_os_open = os.open
    changed = False

    def replace(file: Any) -> None:
        nonlocal changed
        if not changed and Path(file).name == "data.txt" and nested.exists():
            changed = True
            nested.rename(target / "owned-moved")
            nested.symlink_to(outside, target_is_directory=True)

    def io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if "x" in mode:
            replace(file)
        return real_io_open(file, mode, *args, **kwargs)

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            replace(path)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(io, "open", io_open)
    assert real_os_open in os.supports_dir_fd
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os_open})
    with pytest.raises(SnapshotError):
        restore_snapshot(artifact, target, commitment=commitment, policy=policy)
    assert changed
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert (outside / "sentinel").read_text() == "keep outside"


@pytest.mark.parametrize("operation", ["export", "restore", "nested"])
@pytest.mark.parametrize(
    "cleanup_fails", [False, True], ids=["cleanup-completes", "cleanup-refused"]
)
def test_directory_open_failure_cleans_lease_owned_creation(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cleanup_fails: bool,
) -> None:
    """Setup failure removes owned directories or reports the cleanup refusal."""
    if operation == "nested":
        (source / "nested").mkdir()
        (source / "nested" / "data.txt").write_text("nested source")
    artifact, commitment, policy = capture(source, tmp_path)
    assert commitment["members"] == (5 if operation == "nested" else 4)
    unrelated = tmp_path / "caller-data"
    unrelated.mkdir(mode=0o700)
    (unrelated / "sentinel").write_bytes(b"keep caller data\n")
    source_before = tree_state(source)
    artifact_before = tree_state(artifact)
    unrelated_before = tree_state(unrelated)
    target = tmp_path / "new-output"
    created = target / "nested" if operation == "nested" else target
    real_open = os.open
    real_rmdir = os.rmdir
    created_identity: tuple[int, int] | None = None
    open_failed = False
    cleanup_refused = False
    open_failure = OSError(errno.EIO, "controlled directory-open failure after real mkdir")
    cleanup_failure = PermissionError(errno.EACCES, "controlled directory cleanup refusal")

    def open_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal open_failed, created_identity
        if (
            flags & os.O_DIRECTORY
            and Path(path).name == created.name
            and not open_failed
            and created.exists()
        ):
            metadata = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
            expected = created.stat()
            if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
                created_identity = (metadata.st_dev, metadata.st_ino)
                open_failed = True
                raise open_failure
        return real_open(path, flags, *args, **kwargs)

    def remove_directory(path: Any, *, dir_fd: int | None = None) -> None:
        nonlocal cleanup_refused
        if cleanup_fails and created_identity is not None:
            metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) == created_identity:
                cleanup_refused = True
                raise cleanup_failure
        real_rmdir(path, dir_fd=dir_fd)

    assert real_open in os.supports_dir_fd
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_directory)
        patch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_directory})
        patch.setattr(os, "rmdir", remove_directory)
        with pytest.raises(SnapshotError) as caught:
            if operation == "export":
                export_snapshot(source, target, reference="snapshot-1", policy=policy)
            else:
                restore_snapshot(artifact, target, commitment=commitment, policy=policy)

    assert open_failed, "the fixture did not reach the created-directory open"
    assert tree_state(source) == source_before
    assert tree_state(artifact) == artifact_before
    assert tree_state(unrelated) == unrelated_before
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert str(open_failure) in diagnostic
    if cleanup_fails:
        assert cleanup_refused, "the fixture did not reach created-directory cleanup"
        assert created.is_dir()
        metadata = created.stat()
        assert (metadata.st_dev, metadata.st_ino) == created_identity
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert list(created.iterdir()) == []
        assert str(cleanup_failure) in diagnostic, "the caller cannot see cleanup uncertainty"
    else:
        assert not cleanup_refused
        assert not target.exists(), "failed directory open left an owned partial publication"


@pytest.mark.parametrize("operation", ["export", "restore"])
@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_public_parent_cannot_receive_a_private_snapshot(
    source: Path, tmp_path: Path, operation: str, mode: int
) -> None:
    """Actual public parent modes cannot supply the supervisor's private lease."""
    artifact, commitment, policy = capture(source, tmp_path)
    parent = tmp_path / "public-parent"
    parent.mkdir()
    parent.chmod(mode)
    assert stat.S_IMODE(parent.stat().st_mode) == mode
    target = parent / "new-output"
    with pytest.raises(SnapshotError, match="private"):
        if operation == "export":
            export_snapshot(source, target, reference="snapshot-1", policy=policy)
        else:
            restore_snapshot(artifact, target, commitment=commitment, policy=policy)
    assert not target.exists()


@pytest.mark.parametrize("operation", ["export", "restore"])
def test_foreign_parent_cannot_supply_the_output_lease(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Control only owner metadata because the fixture cannot chown to another user."""
    artifact, commitment, policy = capture(source, tmp_path)
    parent = tmp_path / "foreign-parent"
    parent.mkdir(mode=0o700)
    expected = parent.stat()
    original_lstat = Path.lstat
    original_fstat = os.fstat
    observed = False

    def foreign(metadata: os.stat_result) -> os.stat_result:
        nonlocal observed
        if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
            observed = True
            fields = list(metadata)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return metadata

    def lstat(path: Path) -> os.stat_result:
        return foreign(original_lstat(path))

    def fstat(descriptor: int) -> os.stat_result:
        return foreign(original_fstat(descriptor))

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(os, "fstat", fstat)
    target = parent / "new-output"
    with pytest.raises(SnapshotError, match="private"):
        if operation == "export":
            export_snapshot(source, target, reference="snapshot-1", policy=policy)
        else:
            restore_snapshot(artifact, target, commitment=commitment, policy=policy)
    assert observed
    assert not target.exists()


def test_dirty_source_export_verify_restore_preserves_exact_inventory(tmp_path: Path) -> None:
    """Restore the working bytes and modes of every eligible source file."""
    private = tmp_path.resolve() / "snapshot-case"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    source = private / "source"
    source.mkdir(mode=0o700)
    git(source, "init", "--template=")
    committed = {
        ".gitignore": b"ignored/\n",
        "recipe.txt": b"committed\n",
        "remove.txt": b"deleted\n",
        "mode.sh": b"printf snapshot\n",
        "empty.txt": b"",
    }
    for name, content in committed.items():
        path = source / name
        path.write_bytes(content)
        path.chmod(0o644)
    git(source, "add", ".")
    git(source, "commit", "-m", "fixture")
    base_commit = git(source, "rev-parse", "HEAD")
    (source / "recipe.txt").write_bytes(b"staged\n")
    git(source, "add", "recipe.txt")
    (source / "recipe.txt").write_bytes(b"working\n")
    (source / "remove.txt").unlink()
    (source / "mode.sh").chmod(0o751)
    (source / "new.bin").write_bytes(b"untracked\x00\xffcontent\n")
    (source / "new.bin").chmod(0o640)
    for name in ("ignored", "private"):
        (source / name).mkdir(mode=0o700)
        (source / name / "excluded.txt").write_bytes(b"excluded fixture bytes\n")

    expected = {
        ".gitignore": (b"ignored/\n", 0o644),
        "empty.txt": (b"", 0o644),
        "mode.sh": (b"printf snapshot\n", 0o751),
        "new.bin": (b"untracked\x00\xffcontent\n", 0o640),
        "recipe.txt": (b"working\n", 0o644),
    }
    # These checks distinguish fixture failure from missing snapshot behavior.
    assert git(source, "show", "HEAD:recipe.txt") == "committed"
    assert git(source, "show", ":recipe.txt") == "staged"
    assert (source / "recipe.txt").read_bytes() == b"working\n"
    assert not (source / "remove.txt").exists()
    assert git(source, "ls-files", "new.bin") == ""
    for name, (content, mode) in expected.items():
        assert (source / name).read_bytes() == content
        assert stat.S_IMODE((source / name).stat().st_mode) == mode

    # Keep the missing capability inside this collected behavior test.
    try:
        snapshot = importlib.import_module("hephaestus.automation.fleet_snapshot")
    except ModuleNotFoundError as error:
        if error.name != "hephaestus.automation.fleet_snapshot":
            raise
        pytest.fail(
            "Fleet source snapshot API is missing: export_snapshot, verify_snapshot, "
            "and restore_snapshot cannot preserve this dirty source.",
            pytrace=False,
        )

    policy = snapshot.SnapshotPolicy(max_members=20, max_bytes=8192, private_paths=("private",))
    artifact = private / "artifact"
    destination = private / "restored"
    commitment = snapshot.export_snapshot(
        source, artifact, reference="snapshot-round-trip", policy=policy
    )
    assert set(commitment) == {
        "reference",
        "manifestDigest",
        "baseCommit",
        "members",
        "bytes",
        "policyDigest",
    }
    assert commitment["reference"] == "snapshot-round-trip"
    assert commitment["baseCommit"] == base_commit
    assert commitment["members"] == len(expected)
    assert commitment["bytes"] == sum(len(content) for content, _ in expected.values())
    assert {path.name for path in artifact.iterdir()} == {"manifest.json", "source.tar"}
    artifact_before = {path.name: path.read_bytes() for path in artifact.iterdir()}
    manifest_bytes = artifact_before["manifest.json"]
    assert commitment["manifestDigest"] == hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    expected_entries = [
        {
            "path": name,
            "mode": mode,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, (content, mode) in sorted(expected.items())
    ]
    assert manifest == {
        "schema": "hi/hephaestus/source-snapshot/v1",
        "baseCommit": base_commit,
        "policyDigest": commitment["policyDigest"],
        "files": expected_entries,
    }

    names_before = {path.name for path in private.iterdir()}
    assert snapshot.verify_snapshot(artifact, commitment=commitment, policy=policy) is None
    assert {path.name for path in private.iterdir()} == names_before
    assert not destination.exists()
    assert {path.name: path.read_bytes() for path in artifact.iterdir()} == artifact_before
    assert (
        snapshot.restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
        == destination
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert {path.relative_to(destination).as_posix() for path in destination.rglob("*")} == set(
        expected
    )
    restored = {}
    for path in destination.iterdir():
        metadata = path.lstat()
        assert stat.S_ISREG(metadata.st_mode)
        restored[path.name] = (path.read_bytes(), stat.S_IMODE(metadata.st_mode))
    assert restored == expected


@pytest.mark.parametrize("operation", ["export", "restore"])
@pytest.mark.parametrize(
    "cleanup_fails", [False, True], ids=["cleanup-completes", "cleanup-refused"]
)
def test_failed_publication_preserves_caller_data_and_reports_cleanup_failure(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cleanup_fails: bool,
) -> None:
    """A partial write reports cleanup failure and preserves unowned files."""
    artifact, commitment, policy = capture(source, tmp_path)
    target = tmp_path / "new-output"
    leaf = target / ("source.tar" if operation == "export" else ".gitignore")
    caller_file = tmp_path / "caller-data.txt"
    caller_file.write_bytes(b"retain caller data\n")
    caller_before = tree_state(caller_file.parent)["caller-data.txt"]
    source_before = tree_state(source)
    artifact_before = tree_state(artifact)
    foreign = target / "foreign-data.txt"
    foreign_bytes = b"retain foreign data\n"
    foreign_identity: tuple[int, int] | None = None
    partial_identity: tuple[int, int] | None = None
    partial_bytes = b""
    publication_failure = OSError(errno.EIO, "controlled publication write failure")
    cleanup_failure = PermissionError(errno.EACCES, "controlled cleanup refusal")
    real_write = os.write
    real_unlink = os.unlink
    write_failed = False
    cleanup_refused = False

    def write_partial(descriptor: int, data: bytes) -> int:
        nonlocal write_failed, partial_identity, partial_bytes, foreign_identity
        metadata = os.fstat(descriptor)
        if not write_failed and leaf.exists():
            expected = leaf.stat()
            if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
                assert data, "the write failure needs a nonempty publication write"
                assert real_write(descriptor, data[:1]) == 1
                partial_identity = (metadata.st_dev, metadata.st_ino)
                partial_bytes = data[:1]
                write_failed = True
                if cleanup_fails:
                    foreign.write_bytes(foreign_bytes)
                    foreign_metadata = foreign.stat()
                    foreign_identity = (foreign_metadata.st_dev, foreign_metadata.st_ino)
                raise publication_failure
        return real_write(descriptor, data)

    def unlink_owned(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal cleanup_refused
        if cleanup_fails and partial_identity is not None:
            metadata = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) == partial_identity:
                cleanup_refused = True
                raise cleanup_failure
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write_partial)
        patch.setattr(os, "unlink", unlink_owned)
        with pytest.raises(SnapshotError) as caught:
            if operation == "export":
                export_snapshot(source, target, reference="snapshot-1", policy=policy)
            else:
                restore_snapshot(artifact, target, commitment=commitment, policy=policy)

    assert write_failed, "the fixture did not reach the publication write"
    assert tree_state(source) == source_before
    assert tree_state(artifact) == artifact_before
    assert tree_state(caller_file.parent)["caller-data.txt"] == caller_before
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert str(publication_failure) in diagnostic
    if cleanup_fails:
        assert cleanup_refused, "the fixture did not reach owned-file cleanup"
        assert sorted(path.name for path in target.iterdir()) == sorted([leaf.name, foreign.name])
        assert leaf.read_bytes() == partial_bytes
        assert foreign.read_bytes() == foreign_bytes
        metadata = foreign.stat()
        assert (metadata.st_dev, metadata.st_ino) == foreign_identity
        assert str(cleanup_failure) in diagnostic, "the caller cannot see cleanup uncertainty"
    else:
        assert not cleanup_refused
        assert not target.exists(), "successful cleanup left an owned partial publication"


def configuration_source(source: Path, tmp_path: Path, scope: str) -> tuple[Path, str]:
    """Select real checkout, shared linked, or private linked configuration."""
    if scope == "local":
        return source, "--local"
    linked = tmp_path / "linked"
    git(source, "worktree", "add", "--detach", str(linked), "HEAD")
    if scope == "linked-worktree":
        git(source, "config", "--local", "extensions.worktreeConfig", "true")
        return linked, "--worktree"
    return linked, "--local"


@pytest.mark.parametrize("scope", ["local", "linked-local", "linked-worktree"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("credential.helper", "store", id="credential-helper"),
        pytest.param("include.path", "snapshot-unused-include", id="include-path"),
    ],
)
def test_export_refuses_unsafe_git_configuration_before_source_inventory(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, key: str, value: str
) -> None:
    """An unsafe key refuses capture without enumerating or changing source."""
    root, config_scope = configuration_source(source, tmp_path, scope)
    git(root, "config", config_scope, key, value)
    config = git(root, "config", config_scope, "--no-includes", "--null", "--list")
    assert unsafe_local_git_config_key(config) == key
    before = tree_state(tmp_path)
    metadata = root.stat()
    root_identity = (metadata.st_dev, metadata.st_ino)
    real_scandir = os.scandir

    def guarded_scandir(path: Any = ".") -> Any:
        inspected = os.fstat(path) if isinstance(path, int) else os.stat(path)
        if (inspected.st_dev, inspected.st_ino) == root_identity:
            pytest.fail("Export inventoried source before refusing unsafe Git configuration.")
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", guarded_scandir)
        with pytest.raises(SnapshotError):
            capture(root, tmp_path)

    assert not (tmp_path / "artifact").exists()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("scope", ["local", "linked-local", "linked-worktree"])
def test_export_accepts_safe_git_configuration(source: Path, tmp_path: Path, scope: str) -> None:
    """Safe configuration in each supported scope permits a real round trip."""
    root, config_scope = configuration_source(source, tmp_path, scope)
    git(root, "config", config_scope, "snapshot.fixture", "admitted")
    config = git(root, "config", config_scope, "--no-includes", "--null", "--list")
    assert unsafe_local_git_config_key(config) is None
    expected = {
        ".gitignore": b"ignored/\n",
        "mode.sh": b"printf snapshot\n",
        "recipe.txt": b"working\n" if scope == "local" else b"committed\n",
    }
    if scope == "local":
        expected["new.txt"] = b"untracked\x00content\n"
    else:
        expected["remove.txt"] = b"deleted\n"
    source_before = tree_state(root)
    artifact, commitment, policy = capture(root, tmp_path)
    restored = tmp_path / "restored"
    assert restore_snapshot(artifact, restored, commitment=commitment, policy=policy) == restored
    assert {path.name: path.read_bytes() for path in restored.iterdir()} == expected
    assert commitment["members"] == len(expected)
    assert commitment["bytes"] == sum(map(len, expected.values()))
    assert tree_state(root) == source_before


def test_export_refuses_oversized_local_git_configuration(source: Path, tmp_path: Path) -> None:
    """Configuration output above the declared 4 MiB bound cannot be admitted."""
    config_path = source / ".git" / "config"
    with config_path.open("ab") as stream:
        stream.write(b"\n[snapshot]\n\tfixture = " + b"x" * (4 * 1024 * 1024 + 1) + b"\n")
    before = tree_state(tmp_path)
    with pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert not (tmp_path / "artifact").exists()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("operation", ["export", "verify", "restore"])
@pytest.mark.parametrize("context_state", ["expired", "cancelled", "active"])
def test_snapshot_respects_outer_context_before_filesystem_work(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    context_state: str,
) -> None:
    """An exhausted outer context cannot begin source or artifact filesystem work."""
    artifact, commitment, policy = capture(source, tmp_path)
    destination = tmp_path / "restored"
    new_artifact = tmp_path / "new-artifact"
    before = tree_state(tmp_path)
    shutdown = threading.Event()
    if context_state == "cancelled":
        shutdown.set()
    filesystem_calls: list[str] = []
    real_open, real_lstat = os.open, os.lstat

    def observe_open(*args: Any, **kwargs: Any) -> int:
        filesystem_calls.append("open")
        return real_open(*args, **kwargs)

    def observe_lstat(*args: Any, **kwargs: Any) -> os.stat_result:
        filesystem_calls.append("lstat")
        return real_lstat(*args, **kwargs)

    def invoke() -> None:
        if operation == "export":
            exported = export_snapshot(
                source, new_artifact, reference="snapshot-new", policy=policy, timeout=30
            )
            assert exported["members"] == commitment["members"]
            assert exported["bytes"] == commitment["bytes"]
        else:
            receive_snapshot(operation, artifact, destination, commitment, policy, timeout=30)

    with monkeypatch.context() as patch:
        if context_state != "active":
            patch.setattr(time, "monotonic", lambda: 100.0)
        deadline = 99.0 if context_state == "expired" else time.monotonic() + 5.0
        patch.setattr(os, "open", observe_open)
        patch.setattr(os, "lstat", observe_lstat)
        # Keep real descriptor support while observing the same filesystem operations.
        patch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {observe_open})
        with operation_deadline(deadline, shutdown=shutdown):
            if context_state == "active":
                invoke()
            else:
                with pytest.raises(SnapshotError):
                    invoke()

    if context_state != "active":
        assert filesystem_calls == [], "The exhausted context performed filesystem work."
        assert tree_state(tmp_path) == before
        assert not destination.exists()
        assert not new_artifact.exists()
    else:
        assert filesystem_calls, "The valid control did not reach the observed filesystem."
        if operation == "verify":
            assert tree_state(tmp_path) == before
        elif operation == "restore":
            assert (destination / "recipe.txt").read_bytes() == b"working\n"
        else:
            assert {path.name for path in new_artifact.iterdir()} == {"manifest.json", "source.tar"}


@pytest.mark.parametrize(
    "remove_from_index", [False, True], ids=["still-tracked", "staged-index-removal"]
)
def test_current_index_controls_whether_ignore_rules_apply(
    source: Path, tmp_path: Path, remove_from_index: bool
) -> None:
    """Ignore rules apply after index removal, while the file remains on disk."""
    ignore_bytes = b"ignored/\nrecipe.txt\n"
    (source / ".gitignore").write_bytes(ignore_bytes)
    if remove_from_index:
        git(source, "rm", "--cached", "--force", "--", "recipe.txt")
    assert git(source, "ls-files", "--", "recipe.txt") == (
        "" if remove_from_index else "recipe.txt"
    )
    assert git(source, "check-ignore", "--no-index", "--", "recipe.txt") == "recipe.txt"
    assert (source / "recipe.txt").read_bytes() == b"working\n"
    source_before = tree_state(source)
    expected = {
        ".gitignore": ignore_bytes,
        "mode.sh": b"printf snapshot\n",
        "new.txt": b"untracked\x00content\n",
    }
    if not remove_from_index:
        expected["recipe.txt"] = b"working\n"

    artifact, commitment, policy = capture(source, tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_bytes())
    assert {entry["path"] for entry in manifest["files"]} == set(expected)
    destination = tmp_path / "restored"
    assert (
        restore_snapshot(artifact, destination, commitment=commitment, policy=policy) == destination
    )
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == expected
    assert commitment["members"] == len(expected)
    assert commitment["bytes"] == sum(map(len, expected.values()))
    assert tree_state(source) == source_before


def test_snapshot_cancels_git_child_after_output_pipes_close(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited cancellation stops the owned child after its output ends."""
    closed = tmp_path / "pipes-closed"
    executable = tmp_path / "capture-child"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os\nimport time\nfrom pathlib import Path\n"
        f"Path({str(closed)!r}).write_text('closed')\n"
        "os.close(1)\nos.close(2)\n"
        "time.sleep(5)\n"
    )
    executable.chmod(0o700)
    shutdown = threading.Event()
    requested = False
    original_wait = subprocess.Popen.wait

    def request_then_wait(process: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal requested
        if process.args[0] == str(executable) and not requested:
            assert closed.read_text() == "closed"
            requested = True
            shutdown.set()
        return original_wait(process, *args, **kwargs)

    monkeypatch.setattr(fleet_snapshot, "trusted_git_executable", lambda: str(executable))
    monkeypatch.setattr(subprocess.Popen, "wait", request_then_wait)
    started = time.monotonic()
    with operation_deadline(started + 10, shutdown=shutdown), pytest.raises(SnapshotError):
        capture(source, tmp_path)
    assert requested, "The fixture did not reach the child wait after pipe closure."
    assert time.monotonic() - started < 2, "Cancellation waited for the child's normal exit."
    assert not (tmp_path / "artifact").exists()


@pytest.mark.parametrize("mutation", ["head", "index"])
def test_git_identity_change_during_source_read_refuses_export(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """A real HEAD or index change cannot yield a completed source commitment."""
    artifact, commitment, policy = capture(source, tmp_path)
    verify_snapshot(artifact, commitment=commitment, policy=policy)
    head_before = git(source, "rev-parse", "HEAD")
    index_before = git(source, "ls-files", "--stage")
    read = os.read
    changed = False

    def mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = read(descriptor, count)
        if data == b"working\n" and not changed:
            changed = True
            if mutation == "head":
                git(source, "commit", "--allow-empty", "-m", "capture fixture change")
            else:
                git(source, "add", "recipe.txt")
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", mutate)
        with pytest.raises(SnapshotError):
            export_snapshot(
                source, tmp_path / "changed-artifact", reference="changed", policy=policy
            )
    assert changed, "The fixture did not change Git state during the actual source read."
    if mutation == "head":
        assert git(source, "rev-parse", "HEAD") != head_before
    else:
        assert git(source, "ls-files", "--stage") != index_before
        assert git(source, "rev-parse", "HEAD") == head_before
    assert (source / "recipe.txt").read_bytes() == b"working\n"
    assert not (tmp_path / "changed-artifact" / "manifest.json").exists()


def test_export_binds_independent_policy_bytes_and_policy_changes(
    source: Path, tmp_path: Path
) -> None:
    """The commitment binds the complete policy and canonical private-path order."""
    # This is the version-one wire contract, independent of the product serializer.
    fixed_policy = {
        "schema": "hi/hephaestus/source-snapshot-policy/v1",
        "selection": "git-current-tracked-and-unignored-v1",
        "paths": "portable-ascii-v1",
        "fileModes": "posix-permissions-no-special-bits",
        "directories": "implicit-0700",
        "outputParent": "current-user-0700-exclusive-supervisor-lease",
        "symlinks": "reject",
        "submodules": "reject",
        "hardlinks": "reject",
        "excludedNames": sorted(
            [
                ".aws",
                ".azure",
                ".claude.json",
                ".codex",
                ".config",
                ".credentials.json",
                ".fleet-runtime",
                ".git",
                ".git-credentials",
                ".gnupg",
                ".netrc",
                ".npmrc",
                ".pypirc",
                ".ssh",
                "auth.json",
                "credentials.json",
                "fleet-runtime",
                "id_dsa",
                "id_ecdsa",
                "id_ed25519",
                "id_rsa",
            ]
        ),
        "excludedPatterns": [".env", ".env.*", "*.key", "*.pem"],
        "maxPathBytes": 240,
        "maxScanEntries": 40_000,
        "maxManifestBytes": 4 * 1024 * 1024,
        "maxGitOutputBytes": 4 * 1024 * 1024,
    }
    selections = [
        (20, 8192, ("private-z", "private-a")),
        (21, 8192, ("private-z", "private-a")),
        (20, 8193, ("private-z", "private-a")),
        (20, 8192, ("private-c",)),
        (20, 8192, ("private-a", "private-z")),
    ]
    actual_digests = []
    member_sets = []
    for index, (members, byte_limit, private_paths) in enumerate(selections):
        expected_policy = {
            **fixed_policy,
            "maxMembers": members,
            "maxBytes": byte_limit,
            "privatePaths": sorted(private_paths),
        }
        encoded = (
            json.dumps(expected_policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        expected_digest = hashlib.sha256(encoded).hexdigest()
        policy = SnapshotPolicy(members, byte_limit, private_paths=private_paths)
        artifact = tmp_path / f"policy-{index}"
        commitment = export_snapshot(source, artifact, reference="policy-check", policy=policy)
        manifest = json.loads((artifact / "manifest.json").read_bytes())
        assert commitment["policyDigest"] == expected_digest
        assert manifest["policyDigest"] == expected_digest
        verify_snapshot(artifact, commitment=commitment, policy=policy)
        actual_digests.append(commitment["policyDigest"])
        member_sets.append(manifest["files"])
    assert len(set(actual_digests[:4])) == 4
    assert actual_digests[4] == actual_digests[0]
    assert all(members == member_sets[0] for members in member_sets)


@pytest.mark.parametrize(
    "bound,offset",
    [("members", -1), ("members", 1), ("bytes", -1), ("bytes", 0), ("bytes", 1)],
)
def test_exact_source_limits_use_actual_members_and_bytes(
    source: Path, tmp_path: Path, bound: str, offset: int
) -> None:
    """One missing unit refuses capture; exact and spare capacity preserve the source."""
    expected = {
        ".gitignore": b"ignored/\n",
        "recipe.txt": b"working\n",
        "mode.sh": b"printf snapshot\n",
        "new.txt": b"untracked\x00content\n",
    }
    actual_bytes = sum(len(data) for data in expected.values())
    members = len(expected) + offset if bound == "members" else 20
    byte_limit = actual_bytes + offset if bound == "bytes" else 8192
    policy = SnapshotPolicy(members, byte_limit)
    artifact = tmp_path / "boundary-artifact"
    if offset < 0:
        with pytest.raises(SnapshotError):
            export_snapshot(source, artifact, reference="boundary", policy=policy)
        assert not (artifact / "manifest.json").exists()
    else:
        commitment = export_snapshot(source, artifact, reference="boundary", policy=policy)
        assert commitment["members"] == len(expected)
        assert commitment["bytes"] == actual_bytes
        verify_snapshot(artifact, commitment=commitment, policy=policy)
        destination = tmp_path / "restored"
        restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
        assert {path.name: path.read_bytes() for path in destination.iterdir()} == expected


@pytest.mark.parametrize("scan_limit", [5, 6, 7])
def test_filesystem_scan_limit_counts_entries_before_selection(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_limit: int
) -> None:
    """The scan admits its exact entry bound and refuses one fewer entry."""
    from hephaestus.automation import fleet_snapshot_policy

    # The real fixture has six root entries. Its ignored directory is not traversed.
    assert {path.name for path in source.iterdir()} == {
        ".git",
        ".gitignore",
        "ignored",
        "mode.sh",
        "new.txt",
        "recipe.txt",
    }
    monkeypatch.setattr(fleet_snapshot, "MAX_SCAN_ENTRIES", scan_limit)
    monkeypatch.setattr(fleet_snapshot_policy, "MAX_SCAN_ENTRIES", scan_limit)
    policy = SnapshotPolicy(20, 8192)
    artifact = tmp_path / "scan-artifact"
    if scan_limit < 6:
        with pytest.raises(SnapshotError):
            export_snapshot(source, artifact, reference="scan-boundary", policy=policy)
        assert not (artifact / "manifest.json").exists()
    else:
        commitment = export_snapshot(source, artifact, reference="scan-boundary", policy=policy)
        assert commitment["members"] == 4
        verify_snapshot(artifact, commitment=commitment, policy=policy)


def test_export_refuses_case_colliding_effective_source_names(source: Path, tmp_path: Path) -> None:
    """Distinct Git names that differ only by case cannot define portable source."""
    artifact, commitment, policy = capture(source, tmp_path)
    verify_snapshot(artifact, commitment=commitment, policy=policy)
    alias = source / "Recipe.txt"
    if not alias.exists():
        alias.write_bytes(b"second ordinary source\n")
    # Git's index retains both names even when this filesystem folds their case.
    blob = git(source, "rev-parse", ":recipe.txt")
    git(source, "update-index", "--add", "--cacheinfo", f"100644,{blob},Recipe.txt")
    tracked = set(git(source, "ls-files").splitlines())
    assert {"Recipe.txt", "recipe.txt"} <= tracked
    assert alias.is_file() and (source / "recipe.txt").is_file()
    with pytest.raises(SnapshotError):
        export_snapshot(
            source, tmp_path / "colliding-artifact", reference="collision", policy=policy
        )
    assert not (tmp_path / "colliding-artifact" / "manifest.json").exists()


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("collides", [False, True])
def test_receivers_reject_case_collisions_with_consistent_artifact_bytes(
    source: Path, tmp_path: Path, operation: str, collides: bool
) -> None:
    """Matching hashes cannot admit colliding names; an ordinary rename stays valid."""
    artifact, commitment, policy = capture(source, tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_bytes())
    contents = {}
    with tarfile.open(artifact / "source.tar") as reader:
        for entry in reader.getmembers():
            stream = reader.extractfile(entry)
            assert stream is not None
            contents[entry.name] = stream.read()
    renamed = "Recipe.txt" if collides else "new-file.txt"
    for entry in manifest["files"]:
        if entry["path"] == "new.txt":
            entry["path"] = renamed
    contents[renamed] = contents.pop("new.txt")
    manifest["files"].sort(key=lambda entry: entry["path"])
    with tarfile.open(artifact / "source.tar", "w", format=tarfile.USTAR_FORMAT) as writer:
        for entry in manifest["files"]:
            member = tarfile.TarInfo(entry["path"])
            member.size = entry["size"]
            member.mode = entry["mode"]
            writer.addfile(member, io.BytesIO(contents[entry["path"]]))
    encoded = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    (artifact / "manifest.json").write_bytes(encoded)
    commitment["manifestDigest"] = hashlib.sha256(encoded).hexdigest()
    destination = tmp_path / "restored"
    if collides:
        with pytest.raises(SnapshotError):
            receive_snapshot(operation, artifact, destination, commitment, policy)
        assert not destination.exists()
    else:
        receive_snapshot(operation, artifact, destination, commitment, policy)
        if operation == "restore":
            assert {path.name: path.read_bytes() for path in destination.iterdir()} == contents


@pytest.mark.parametrize("collides", [False, True], ids=["shared-directory", "case-collision"])
def test_export_rejects_implicit_directory_case_collisions(
    source: Path, tmp_path: Path, collides: bool
) -> None:
    """Conflicting directory spellings cannot produce a completed artifact."""
    second = "folder/two.txt" if collides else "Folder/two.txt"
    contents = {"Folder/one.txt": b"first ordinary file\n", second: b"second ordinary file\n"}
    with (source / ".gitignore").open("a") as stream:
        # On case-folding hosts, ignore the discovered Folder/two.txt alias so
        # a full-path collision cannot mask the index's directory-spelling gap.
        stream.write("/Folder/two.txt\n")
    for name, data in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        blob = git(source, "hash-object", "-w", "--", name)
        git(source, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    indexed = set(git(source, "ls-files", "-z").split("\0"))
    assert set(contents) <= indexed
    for name, data in contents.items():
        assert (source / name).is_file()
        assert (source / name).read_bytes() == data
    source_before = tree_state(source)

    if collides:
        before = tree_state(tmp_path)
        with pytest.raises(SnapshotError):
            capture(source, tmp_path)
        assert tree_state(tmp_path) == before
        assert not (tmp_path / "artifact").exists()
    else:
        artifact, commitment, policy = capture(source, tmp_path)
        verify_snapshot(artifact, commitment=commitment, policy=policy)
        destination = tmp_path / "restored"
        restore_snapshot(artifact, destination, commitment=commitment, policy=policy)
        assert commitment["members"] == 6
        assert sorted(path.name for path in (destination / "Folder").iterdir()) == [
            "one.txt",
            "two.txt",
        ]
        for name, data in contents.items():
            assert (destination / name).read_bytes() == data
        assert tree_state(source) == source_before


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize(
    "case",
    [
        "shared-directory",
        "directory-case-collision",
        "file-directory-collision",
        "file-before-directory",
    ],
)
def test_receivers_reject_implicit_directory_case_collisions(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    case: str,
) -> None:
    """Consistent bytes cannot authorize conflicting file or directory spellings."""
    artifact, commitment, policy = capture(source, tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_bytes())
    second = {
        "shared-directory": "Folder/two.txt",
        "directory-case-collision": "folder/two.txt",
        "file-directory-collision": "folder",
        "file-before-directory": "folder/two.txt",
    }[case]
    first = "Folder" if case == "file-before-directory" else "Folder/one.txt"
    contents = {first: b"first ordinary file\n", second: b"second ordinary file\n"}
    manifest["files"] = [
        {"path": name, "mode": 0o644, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(contents.items())
    ]
    with tarfile.open(artifact / "source.tar", "w", format=tarfile.USTAR_FORMAT) as writer:
        for entry in manifest["files"]:
            member = tarfile.TarInfo(entry["path"])
            member.size = entry["size"]
            member.mode = entry["mode"]
            writer.addfile(member, io.BytesIO(contents[entry["path"]]))
    encoded = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    (artifact / "manifest.json").write_bytes(encoded)
    commitment["manifestDigest"] = hashlib.sha256(encoded).hexdigest()
    commitment["members"] = len(contents)
    commitment["bytes"] = sum(map(len, contents.values()))
    destination = tmp_path / "restored"
    before = tree_state(tmp_path)

    if case != "shared-directory":
        real_mkdir = os.mkdir

        def refuse_destination(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(os.fsdecode(path)).name == destination.name:
                pytest.fail(
                    "Restore created its destination before rejecting colliding directories."
                )
            real_mkdir(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "mkdir", refuse_destination)
            patch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {refuse_destination})
            with pytest.raises(SnapshotError):
                receive_snapshot(operation, artifact, destination, commitment, policy)
        assert not destination.exists()
        assert tree_state(tmp_path) == before
    else:
        receive_snapshot(operation, artifact, destination, commitment, policy)
        if operation == "verify":
            assert not destination.exists()
            assert tree_state(tmp_path) == before
        else:
            assert {
                path.relative_to(destination).as_posix(): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            } == contents
            for name in contents:
                assert stat.S_IMODE((destination / name).stat().st_mode) == 0o644


class ObservedArtifactScan:
    """Expose real directory entries through a bounded OS iterator fixture."""

    def __init__(self, entries: list[os.DirEntry[str]], case: str) -> None:
        self.entries = entries
        self.case = case
        self.offset = 0
        self.closed = False
        self.expired = False
        self.names: list[str] = []

    def __iter__(self) -> ObservedArtifactScan:
        """Return the observed iterator."""
        return self

    def __next__(self) -> os.DirEntry[str]:
        """Yield one real entry within the fixture's inspection bound."""
        if self.offset == len(self.entries):
            raise StopIteration
        limit = 2 if self.case == "inherited-deadline" else 3
        if self.offset >= limit:
            pytest.fail("Artifact scan continued after its refusal or deadline boundary.")
        entry = self.entries[self.offset]
        self.offset += 1
        self.names.append(entry.name)
        if self.case == "inherited-deadline" and self.offset == 1:
            self.expired = True
        return entry

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> ObservedArtifactScan:
        """Return this iterator as a scan context."""
        return self

    def __exit__(self, *details: object) -> None:
        """Record closure when the scan context exits."""
        self.close()


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("case", ["valid", "extra-entry", "inherited-deadline"])
def test_artifact_membership_scan_stops_at_refusal_or_deadline(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    case: str,
) -> None:
    """Artifact membership uses a bounded scan and the inherited time budget."""
    artifact, commitment, policy = capture(source, tmp_path)
    if case != "valid":
        (artifact / "extra-a").write_bytes(b"")
        (artifact / "extra-b").write_bytes(b"")
    before = tree_state(tmp_path)
    metadata = artifact.stat()
    artifact_identity = (metadata.st_dev, metadata.st_ino)
    real_scandir = os.scandir
    with real_scandir(artifact) as entries:
        actual_entries = {entry.name: entry for entry in entries}
    names = ["manifest.json", "source.tar"]
    if case != "valid":
        names.extend(["extra-a", "extra-b"])
    assert set(actual_entries) == set(names)
    ordered = [actual_entries[name] for name in names]
    scans: list[ObservedArtifactScan] = []

    def observe_scandir(path: Any = ".") -> Any:
        inspected = os.fstat(path) if isinstance(path, int) else os.stat(path)
        if (inspected.st_dev, inspected.st_ino) != artifact_identity:
            return real_scandir(path)
        scan = ObservedArtifactScan(ordered, case)
        scans.append(scan)
        return scan

    destination = tmp_path / "restored"
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", observe_scandir)
        if real_scandir in os.supports_fd:
            patch.setattr(os, "supports_fd", os.supports_fd | {observe_scandir})
        patch.setattr(
            time, "monotonic", lambda: 20.0 if any(scan.expired for scan in scans) else 10.0
        )
        with operation_deadline(15.0):
            if case == "valid":
                receive_snapshot(operation, artifact, destination, commitment, policy, timeout=30)
            else:
                with pytest.raises(SnapshotError) as caught:
                    receive_snapshot(
                        operation, artifact, destination, commitment, policy, timeout=30
                    )

    assert scans, "the fixture did not reach artifact membership enumeration"
    assert all(scan.closed for scan in scans)
    if case == "valid":
        assert all(scan.names == ["manifest.json", "source.tar"] for scan in scans)
        if operation == "verify":
            assert not destination.exists()
            assert tree_state(tmp_path) == before
        else:
            assert (destination / "recipe.txt").read_bytes() == b"working\n"
            assert (destination / "new.txt").read_bytes() == b"untracked\x00content\n"
    else:
        assert not destination.exists()
        assert tree_state(tmp_path) == before
        if case == "extra-entry":
            assert any(scan.names == ["manifest.json", "source.tar", "extra-a"] for scan in scans)
        else:
            assert any(scan.expired for scan in scans)
            assert all(1 <= len(scan.names) <= 2 for scan in scans)
            diagnostic = "".join(traceback.format_exception(caught.value))
            assert "operation deadline" in diagnostic


def _snapshot_duplicate_metadata(
    descriptor: int, fstat: Callable[[int], os.stat_result]
) -> os.stat_result | None:
    """Return real handle metadata, or None only when the handle is closed."""
    try:
        return fstat(descriptor)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return None


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("metadata_fails", [False, True], ids=["control", "metadata-fails"])
def test_receiver_closes_duplicate_when_initial_metadata_read_fails(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    metadata_fails: bool,
) -> None:
    """A receiver closes its duplicate after a failed initial metadata read."""
    artifact, commitment, policy = capture(source, tmp_path)
    destination = tmp_path / "restored"
    caller = tmp_path / "caller-data"
    caller.mkdir(mode=0o700)
    (caller / "sentinel").write_bytes(b"retain caller data\n")
    source_before = tree_state(source)
    artifact_before = tree_state(artifact)
    caller_before = tree_state(caller)
    before = tree_state(tmp_path)
    metadata = artifact.stat()
    artifact_identity = (metadata.st_dev, metadata.st_ino)
    real_dup = os.dup
    real_fstat = os.fstat
    real_close = os.close
    owned_duplicate: int | None = None
    initial_probe_seen = False
    failure_count = 0
    failure = OSError(errno.EIO, "controlled initial duplicate metadata failure")

    def duplicate(descriptor: int) -> int:
        nonlocal owned_duplicate
        result = real_dup(descriptor)
        metadata = real_fstat(descriptor)
        if owned_duplicate is None and (metadata.st_dev, metadata.st_ino) == artifact_identity:
            owned_duplicate = result
        return result

    def probe(descriptor: int) -> os.stat_result:
        nonlocal initial_probe_seen, failure_count
        if descriptor == owned_duplicate and not initial_probe_seen:
            initial_probe_seen = True
            if metadata_fails:
                failure_count += 1
                raise failure
        return real_fstat(descriptor)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "dup", duplicate)
            patch.setattr(os, "fstat", probe)
            if metadata_fails:
                with pytest.raises(SnapshotError) as caught:
                    receive_snapshot(operation, artifact, destination, commitment, policy)
                assert str(failure) in "".join(traceback.format_exception(caught.value))
            else:
                receive_snapshot(operation, artifact, destination, commitment, policy)

        assert owned_duplicate is not None, "the receiver did not duplicate the artifact handle"
        assert initial_probe_seen, "the receiver did not inspect the duplicated handle"
        assert failure_count == int(metadata_fails)
        closed = _snapshot_duplicate_metadata(owned_duplicate, real_fstat) is None

        assert tree_state(source) == source_before
        assert tree_state(artifact) == artifact_before
        assert tree_state(caller) == caller_before
        if metadata_fails or operation == "verify":
            assert not destination.exists()
            assert tree_state(tmp_path) == before
        else:
            assert (destination / "recipe.txt").read_bytes() == b"working\n"
            assert sorted(path.name for path in destination.iterdir()) == [
                ".gitignore",
                "mode.sh",
                "new.txt",
                "recipe.txt",
            ]
        assert closed, "the receiver left its duplicated artifact descriptor open"
    finally:
        # Release a fixture-owned leak only after the product assertion fails.
        if owned_duplicate is not None:
            remaining_metadata = _snapshot_duplicate_metadata(owned_duplicate, real_fstat)
            if remaining_metadata is not None:
                assert (remaining_metadata.st_dev, remaining_metadata.st_ino) == artifact_identity
                real_close(owned_duplicate)


class _PublicationMetadataProbe:
    """Inject one metadata failure after a real output file is open."""

    def __init__(self, leaf: Path, case: str) -> None:
        """Keep real OS calls and the identity of the selected fixture file."""
        self.leaf = leaf
        self.case = case
        self.real_fstat = os.fstat
        self.real_close = os.close
        self.real_rmdir = os.rmdir
        self.descriptor: int | None = None
        self.identity: tuple[int, int] | None = None
        self.foreign_identity: tuple[int, int] | None = None
        self.moved = leaf.parent.parent / "retained-open-file"
        self.foreign_bytes = b"retain replacement data\n"
        self.failure = OSError(errno.EIO, "controlled initial output metadata failure")
        self.cleanup_failure = PermissionError(errno.EACCES, "controlled cleanup refusal")
        self.metadata_failed = False
        self.cleanup_refused = False

    def fstat(self, descriptor: int) -> os.stat_result:
        """Fail the first metadata probe of the real newly created output file."""
        metadata = self.real_fstat(descriptor)
        if self.descriptor is None and stat.S_ISREG(metadata.st_mode) and self.leaf.exists():
            selected = self.leaf.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            if identity == (selected.st_dev, selected.st_ino):
                self.descriptor = descriptor
                self.identity = identity
                if self.case == "foreign-replacement":
                    self.leaf.rename(self.moved)
                    self.leaf.write_bytes(self.foreign_bytes)
                    foreign = self.leaf.lstat()
                    self.foreign_identity = (foreign.st_dev, foreign.st_ino)
                if self.case != "control":
                    self.metadata_failed = True
                    raise self.failure
        return metadata

    def rmdir(self, path: Any, *, dir_fd: int | None = None) -> None:
        """Refuse cleanup only for this test's newly created output directory."""
        if self.case == "cleanup-refused" and self.metadata_failed:
            selected = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            output = self.leaf.parent.lstat()
            if (selected.st_dev, selected.st_ino) == (output.st_dev, output.st_ino):
                self.cleanup_refused = True
                raise self.cleanup_failure
        self.real_rmdir(path, dir_fd=dir_fd)

    def close_fixture_leak(self) -> None:
        """Close only an inode-confirmed fixture leak after the product assertions."""
        if self.descriptor is not None:
            metadata = _snapshot_duplicate_metadata(self.descriptor, self.real_fstat)
            if metadata is not None:
                assert (metadata.st_dev, metadata.st_ino) == self.identity
                self.real_close(self.descriptor)


@pytest.mark.parametrize("operation", ["export", "restore"])
@pytest.mark.parametrize(
    "case", ["control", "metadata-fails", "cleanup-refused", "foreign-replacement"]
)
def test_publication_closes_file_when_initial_metadata_read_fails(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    case: str,
) -> None:
    """Close the output handle and report any output whose cleanup is uncertain."""
    artifact, commitment, policy = capture(source, tmp_path)
    target = tmp_path / "new-output"
    leaf = target / ("source.tar" if operation == "export" else ".gitignore")
    caller = tmp_path / "caller-data"
    caller.mkdir(mode=0o700)
    (caller / "sentinel").write_bytes(b"retain caller data\n")
    source_before = tree_state(source)
    artifact_before = tree_state(artifact)
    caller_before = tree_state(caller)
    probe = _PublicationMetadataProbe(leaf, case)
    result: dict[str, Any] | Path | None = None
    diagnostic = ""

    def publish_output() -> dict[str, Any] | Path:
        if operation == "export":
            return export_snapshot(source, target, reference="snapshot-1", policy=policy)
        return restore_snapshot(artifact, target, commitment=commitment, policy=policy)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", probe.fstat)
            patch.setattr(os, "rmdir", probe.rmdir)
            if case == "control":
                result = publish_output()
            else:
                with pytest.raises(SnapshotError) as caught:
                    publish_output()
                assert caught.value.__cause__ is probe.failure
                diagnostic = "".join(traceback.format_exception(caught.value))

        assert probe.descriptor is not None, "the fixture did not reach the new output handle"
        assert probe.metadata_failed == (case != "control")
        closed = _snapshot_duplicate_metadata(probe.descriptor, probe.real_fstat) is None
        assert tree_state(source) == source_before
        assert tree_state(artifact) == artifact_before
        assert tree_state(caller) == caller_before

        if case == "control":
            if operation == "export":
                assert result == commitment
                verify_snapshot(target, commitment=commitment, policy=policy)
            else:
                assert result == target
                assert (target / "recipe.txt").read_bytes() == b"working\n"
                assert (target / "new.txt").read_bytes() == b"untracked\x00content\n"
        else:
            assert result is None
            assert str(probe.failure) in diagnostic
            assert any("cleanup" in note for note in getattr(probe.failure, "__notes__", []))
            assert sorted(path.name for path in target.iterdir()) == [leaf.name]
            assert stat.S_IMODE(target.stat().st_mode) == 0o700
            retained = probe.moved if case == "foreign-replacement" else leaf
            metadata = retained.lstat()
            assert (metadata.st_dev, metadata.st_ino) == probe.identity
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert retained.read_bytes() == b""
        if case == "cleanup-refused":
            assert probe.cleanup_refused, "the fixture did not reach output-directory cleanup"
            assert str(probe.cleanup_failure) in diagnostic
        if case == "foreign-replacement":
            assert leaf.read_bytes() == probe.foreign_bytes
            metadata = leaf.lstat()
            assert (metadata.st_dev, metadata.st_ino) == probe.foreign_identity
        assert closed, "publication left its new regular-file descriptor open"
    finally:
        probe.close_fixture_leak()
