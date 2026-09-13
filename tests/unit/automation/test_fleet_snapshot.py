"""Real source and artifact behavior for the finite Fleet snapshot profile."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import fleet_snapshot
from hephaestus.automation.fleet_snapshot import (
    SnapshotError,
    SnapshotPolicy,
    export_snapshot,
    restore_snapshot,
)
from hephaestus.config.child_environments import build_git_child_env


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
def test_changed_commitment_is_not_content_verification(
    source: Path, tmp_path: Path, field: str
) -> None:
    """A modified admission commitment does not verify an artifact."""
    artifact, commitment, policy = capture(source, tmp_path)
    commitment[field] = "../foreign" if field == "reference" else 0
    with pytest.raises(SnapshotError):
        restore_snapshot(artifact, tmp_path / "restored", commitment=commitment, policy=policy)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "corruption", ["content", "mode", "extra", "duplicate", "traversal", "link", "missing"]
)
def test_receiver_rejects_corrupt_actual_archive(
    source: Path, tmp_path: Path, corruption: str
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
    else:
        members.pop()
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as writer:
        for item, data in members:
            writer.addfile(item, io.BytesIO(data))
    with pytest.raises(SnapshotError):
        restore_snapshot(artifact, tmp_path / "restored", commitment=commitment, policy=policy)
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "restored").exists()


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

    monkeypatch.setattr(fleet_snapshot, "_run_bounded_git_output", unexpected_git)
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


def test_artifact_change_between_reads_is_rejected(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        restore_snapshot(artifact, tmp_path / "restored", commitment=commitment, policy=policy)
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
def test_directory_open_failure_cleans_lease_owned_creation(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """An ordinary open failure cleans a directory created under the caller's lease."""
    if operation == "nested":
        (source / "nested").mkdir()
        (source / "nested" / "data.txt").write_text("nested source")
    artifact, commitment, policy = capture(source, tmp_path)
    assert commitment["members"] == (5 if operation == "nested" else 4)
    target = tmp_path / "new-output"
    created = target / "nested" if operation == "nested" else target
    real_open = os.open
    changed = False

    def open_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        if (
            flags & os.O_DIRECTORY
            and Path(path).name == created.name
            and not changed
            and created.exists()
        ):
            changed = True
            raise OSError("controlled directory-open failure after real mkdir")
        return real_open(path, flags, *args, **kwargs)

    assert real_open in os.supports_dir_fd
    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_directory})
    with pytest.raises(SnapshotError):
        if operation == "export":
            export_snapshot(source, target, reference="snapshot-1", policy=policy)
        else:
            restore_snapshot(artifact, target, commitment=commitment, policy=policy)
    assert changed
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
