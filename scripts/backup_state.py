#!/usr/bin/env python3
"""Backup, restore, and verify Hephaestus tier-3 operational state.

Disaster-recovery tooling for the local-only ("tier-3") automation state
directory ``build/.issue_implementer/`` — arming records, CI-fix markers, and
per-stage logs. Tier-1 state (issues, labels, PRs, branches, tags) is durable
on GitHub and re-derived; tier-2 state (``.venv``, worktrees, caches) is
recreated with ``uv sync``. See ``docs/adr/0013-backup-and-disaster-recovery-policy.md``
and ``docs/runbooks/backup-restore.md``.

This tool is deliberately stdlib-only and imports no ``hephaestus`` module: it
must run under a bare ``python3`` in a broken environment (no ``uv sync``, no
editable install), because that is precisely when a restore is needed.
Credentials and secrets are never archived.

Verification and restore fail closed: they reject malformed manifests,
unauthorized or non-regular archive members, unsafe paths, and size or digest
mismatches before any state payload is written.

Usage:
    # Archive tier-3 state to ~/.hephaestus-backups/
    python3 scripts/backup_state.py backup

    # Read-only integrity drill against an archive (exit 0 pass, 1 fail)
    python3 scripts/backup_state.py verify <archive.tar.gz>

    # Restore an archive into the repo (refuses non-empty target without --force)
    python3 scripts/backup_state.py restore <archive.tar.gz> --force

Exit codes:
    0  success (or verify: all members intact)
    1  verify failure (invalid structure, metadata, size, or digest)
    2  usage error, or a restore refused because the target was non-empty
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

# Tier-3, local-only state. Tiers 1-2 are re-derived, not archived (ADR-0013).
INVENTORY: tuple[str, ...] = ("build/.issue_implementer",)

# Manifest member name inside the archive; maps each member to its digest+size.
MANIFEST_NAME = "manifest.json"

_ARCHIVE_PREFIX = "hephaestus-state-"


class RestoreError(Exception):
    """Raised when a restore cannot be performed safely (fail closed)."""


class BackupError(Exception):
    """Raised when inventory cannot be archived as safe regular files."""


def _sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _read_regular_inventory_file(path: Path) -> bytes:
    """Read one unchanged regular file without following a symbolic link."""
    try:
        expected = path.lstat()
    except OSError as exc:
        raise BackupError(f"cannot inspect inventory path {path}: {exc}") from exc
    if not stat.S_ISREG(expected.st_mode):
        raise BackupError(f"inventory path is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"cannot safely open inventory file {path}: {exc}") from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (expected.st_dev, expected.st_ino):
            raise BackupError(f"inventory file changed while being opened: {path}")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _inventory_members(repo_root: Path) -> list[tuple[str, bytes]]:
    """Return safe regular inventory members and their bytes, sorted by name."""
    members: list[tuple[str, bytes]] = []
    for prefix in INVENTORY:
        base = repo_root / prefix
        current = repo_root
        for part in PurePosixPath(prefix).parts:
            current /= part
            if current.is_symlink():
                raise BackupError(f"inventory path is a symbolic link: {current}")

        if not base.exists():
            continue
        if not base.is_dir():
            raise BackupError(f"inventory root is not a directory: {base}")

        for path in base.rglob("*"):
            if path.is_symlink():
                raise BackupError(f"inventory path is a symbolic link: {path}")
            if path.is_dir():
                continue
            rel = path.relative_to(repo_root).as_posix()
            members.append((rel, _read_regular_inventory_file(path)))
    return sorted(members)


def cmd_backup(repo_root: Path, output_dir: Path, timestamp: str) -> Path:
    """Archive INVENTORY paths to ``<output_dir>/hephaestus-state-<timestamp>.tar.gz``.

    The archive stores each file under its repo-relative POSIX path plus a
    ``manifest.json`` mapping every member to its SHA-256 digest and byte size.
    Symbolic links are rejected before the archive is created; hard-linked
    files are materialized as independent regular members.
    A repo with no tier-3 state produces a valid archive with an empty member
    map (an empty backup is a legitimate state, not an error).

    Args:
        repo_root: Repository root the inventory paths are relative to.
        output_dir: Directory to write the archive into (created if absent).
        timestamp: Timestamp component of the archive filename (UTC, caller-supplied
            so library behavior is deterministic under test).

    Returns:
        The path to the written archive.

    Raises:
        BackupError: If an inventory path is a symlink, is not a regular file,
            or changes while being opened.

    """
    inventory_members = _inventory_members(repo_root)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{_ARCHIVE_PREFIX}{timestamp}.tar.gz"

    members: dict[str, dict[str, object]] = {}
    with tarfile.open(archive_path, "w:gz") as tar:
        for rel, data in inventory_members:
            members[rel] = {"sha256": _sha256_bytes(data), "size": len(data)}
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        manifest = json.dumps({"members": members}, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    return archive_path


def _canonical_member_name(name: str) -> str:
    """Return one safe, canonical POSIX archive-member name.

    Raises:
        RestoreError: If ``name`` is empty, unsafe, or normalizes to the
            current directory.

    """
    if not name or "\x00" in name or "\\" in name:
        raise RestoreError(f"unsafe archive member path: {name!r}")

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RestoreError(f"unsafe archive member path: {name!r}")

    normalized = path.as_posix()
    if normalized == ".":
        raise RestoreError(f"unsafe archive member path: {name!r}")
    return normalized


def _inventory_prefix_for(normalized: str) -> str:
    """Return the inventory prefix that strictly owns ``normalized``."""
    path = PurePosixPath(normalized)
    for prefix in INVENTORY:
        inventory = PurePosixPath(prefix)
        if path != inventory and path.is_relative_to(inventory):
            return prefix
    raise RestoreError(f"archive member is outside inventory: {normalized!r}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError(f"duplicate manifest key: {key!r}")
        result[key] = value
    return result


def _read_manifest(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> dict[str, dict[str, object]]:
    """Read and validate a manifest from its validated regular member.

    Raises:
        RestoreError: If the manifest cannot be read, decoded, parsed, or
            does not contain valid member metadata.

    """
    try:
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RestoreError(f"archive has no readable {MANIFEST_NAME}")
        document = json.loads(
            extracted.read().decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"invalid {MANIFEST_NAME}: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"members"}:
        raise RestoreError(f"{MANIFEST_NAME} must contain exactly a members object")
    raw_members = document["members"]
    if not isinstance(raw_members, dict):
        raise RestoreError(f"{MANIFEST_NAME} members must be an object")

    members: dict[str, dict[str, object]] = {}
    for name, metadata in raw_members.items():
        if not isinstance(metadata, dict) or set(metadata) != {"sha256", "size"}:
            raise RestoreError(f"invalid manifest metadata for {name!r}")

        digest = metadata["sha256"]
        size = metadata["size"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise RestoreError(f"invalid SHA-256 for {name!r}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise RestoreError(f"invalid SHA-256 for {name!r}") from exc
        if type(size) is not int or size < 0:
            raise RestoreError(f"invalid size for {name!r}")

        members[name] = metadata
    return members


def _validated_restore_members(
    tar: tarfile.TarFile,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, tuple[tarfile.TarInfo, str]],
]:
    """Validate and bind the manifest and authorized regular data members.

    The complete tar index is checked before any state payload is read. Only
    one regular ``manifest.json`` and regular files strictly beneath an
    ``INVENTORY`` prefix are accepted, and the manifest must name exactly the
    same canonical data members.

    Raises:
        RestoreError: If the archive contains unsafe, duplicate, unknown, or
            non-regular members, or an invalid/non-matching manifest.

    """
    manifest_member: tarfile.TarInfo | None = None
    archive_members: dict[str, tuple[tarfile.TarInfo, str]] = {}
    seen: set[str] = set()

    for member in tar.getmembers():
        normalized = _canonical_member_name(member.name)
        if normalized in seen:
            raise RestoreError(f"duplicate archive member: {normalized!r}")
        seen.add(normalized)

        if not member.isreg():
            raise RestoreError(f"archive member is not a regular file: {member.name!r}")

        if normalized == MANIFEST_NAME:
            manifest_member = member
            continue

        prefix = _inventory_prefix_for(normalized)
        archive_members[normalized] = (member, prefix)

    if manifest_member is None:
        raise RestoreError(f"archive is missing {MANIFEST_NAME}")

    raw_manifest = _read_manifest(tar, manifest_member)
    manifest: dict[str, dict[str, object]] = {}
    for raw_name, metadata in raw_manifest.items():
        normalized = _canonical_member_name(raw_name)
        _inventory_prefix_for(normalized)
        if normalized in manifest:
            raise RestoreError(f"duplicate normalized manifest member: {normalized!r}")
        manifest[normalized] = metadata

    if set(manifest) != set(archive_members):
        raise RestoreError("archive members do not exactly match the manifest")
    return manifest, archive_members


def _is_within(root: Path, target: Path) -> bool:
    """Return True if ``target`` resolves to a path inside ``root`` (or equals it)."""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def cmd_verify(archive: Path) -> int:
    """Verify archive structure, authorization, sizes, and digests read-only.

    Invalid archive members, malformed manifests, and size or digest
    mismatches produce a ``FAIL`` result and return ``1``. Never mutates the
    repo or the archive.

    Returns:
        0 if every member matches its recorded digest, 1 otherwise.

    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest, archive_members = _validated_restore_members(tar)
            ok = True
            for name, metadata in sorted(manifest.items()):
                member, _ = archive_members[name]
                extracted = tar.extractfile(member)
                data = extracted.read() if extracted is not None else None
                size = cast(int, metadata["size"])
                digest = cast(str, metadata["sha256"])
                if (
                    data is None
                    or member.size != size
                    or len(data) != size
                    or _sha256_bytes(data) != digest
                ):
                    print(f"FAIL {name} (content mismatch)")
                    ok = False
                else:
                    print(f"PASS {name}")
            return 0 if ok else 1
    except (RestoreError, OSError, tarfile.TarError) as exc:
        print(f"FAIL archive ({exc})")
        return 1


def cmd_restore(repo_root: Path, archive: Path, *, force: bool = False) -> None:
    """Restore a fully validated archive into ``repo_root``.

    Fail-closed guarantees:
    - Invalid structure, unauthorized members, malformed manifests, and
      destination escapes are rejected before state payloads are read.
    - Every member is verified against its declared size and digest before
      anything is written; a single mismatch aborts with nothing written.
    - If any INVENTORY target directory is already non-empty, the restore is
      refused unless ``force`` is set, so a restore never silently clobbers state.

    Raises:
        RestoreError: On invalid structure, unauthorized members, malformed
            metadata, size or digest mismatch, destination escape, or a
            non-empty target without ``force``. The repository is left
            untouched in every validation failure.

    """
    staged: dict[Path, bytes] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest, archive_members = _validated_restore_members(tar)

            # Refuse to overwrite populated tier-3 targets unless forced.
            if not force:
                for prefix in INVENTORY:
                    base = repo_root / prefix
                    if base.exists() and any(base.rglob("*")):
                        raise RestoreError(
                            f"target {base} is not empty; pass force=True to overwrite"
                        )

            repo_resolved = repo_root.resolve()
            destinations: dict[str, tuple[Path, tarfile.TarInfo]] = {}
            seen_destinations: set[Path] = set()
            for name in manifest:
                member, prefix = archive_members[name]
                inventory_root = repo_resolved / prefix
                dest = (repo_resolved / name).resolve()
                if not _is_within(repo_resolved, dest) or not _is_within(inventory_root, dest):
                    raise RestoreError(f"refusing member outside inventory: {name!r}")
                if dest in seen_destinations:
                    raise RestoreError(f"multiple members resolve to destination: {name!r}")
                seen_destinations.add(dest)
                metadata = manifest[name]
                if member.size != cast(int, metadata["size"]):
                    raise RestoreError(f"size mismatch for {name!r}; refusing restore")
                destinations[name] = (dest, member)

            for name, metadata in manifest.items():
                dest, member = destinations[name]
                extracted = tar.extractfile(member)
                data = extracted.read() if extracted is not None else None
                if data is None:
                    raise RestoreError(f"member is unreadable: {name!r}")
                size = cast(int, metadata["size"])
                digest = cast(str, metadata["sha256"])
                if len(data) != size:
                    raise RestoreError(f"size mismatch for {name!r}; refusing restore")
                if _sha256_bytes(data) != digest:
                    raise RestoreError(f"digest mismatch for {name!r}; refusing restore")
                staged[dest] = data

    except RestoreError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"archive processing failed: {exc}") from exc

    # All members verified — commit writes only now (fail closed above).
    for dest, data in staged.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def _default_output_dir() -> Path:
    """Default backup destination: ``~/.hephaestus-backups`` (outside the repo)."""
    return Path.home() / ".hephaestus-backups"


def _default_repo_root() -> Path:
    """Return the repo root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the three subcommands."""
    parser = argparse.ArgumentParser(
        prog="backup_state.py",
        description="Backup, restore, and verify Hephaestus tier-3 operational state.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Archive tier-3 state to an output directory.")
    p_backup.add_argument(
        "--repo-root", type=Path, default=None, help="Repository root (default: autodetect)."
    )
    p_backup.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: ~/.hephaestus-backups).",
    )
    p_backup.add_argument(
        "--timestamp",
        default=None,
        help="Archive timestamp component (default: current UTC time).",
    )

    p_restore = sub.add_parser("restore", help="Restore an archive into the repository.")
    p_restore.add_argument("archive", type=Path, help="Archive produced by 'backup'.")
    p_restore.add_argument(
        "--repo-root", type=Path, default=None, help="Repository root (default: autodetect)."
    )
    p_restore.add_argument("--force", action="store_true", help="Overwrite a non-empty target.")

    p_verify = sub.add_parser("verify", help="Read-only integrity drill on an archive.")
    p_verify.add_argument("archive", type=Path, help="Archive produced by 'backup'.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "backup":
        repo_root = args.repo_root or _default_repo_root()
        output_dir = args.output or _default_output_dir()
        timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            archive = cmd_backup(repo_root, output_dir, timestamp)
        except BackupError as exc:
            print(f"Backup refused: {exc}", file=sys.stderr)
            return 2
        print(f"Wrote backup: {archive}")
        return 0

    if args.command == "verify":
        return cmd_verify(args.archive)

    if args.command == "restore":
        repo_root = args.repo_root or _default_repo_root()
        try:
            cmd_restore(repo_root, args.archive, force=args.force)
        except RestoreError as exc:
            print(f"Restore refused: {exc}", file=sys.stderr)
            return 2
        print(f"Restored {args.archive} into {repo_root}")
        return 0

    parser.error(f"unknown command: {args.command}")  # pragma: no cover - argparse guards this
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
