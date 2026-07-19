#!/usr/bin/env python3
"""Backup, restore, and verify Hephaestus tier-3 operational state.

Disaster-recovery tooling for the local-only ("tier-3") automation state
directory ``build/.issue_implementer/`` — arming records, CI-fix markers, and
per-stage logs. Tier-1 state (issues, labels, PRs, branches, tags) is durable
on GitHub and re-derived; tier-2 state (``.venv``, worktrees, caches) is
recreated with ``uv sync``. See ``docs/adr/0012-backup-and-disaster-recovery-policy.md``
and ``docs/runbooks/backup-restore.md``.

This tool is deliberately stdlib-only and imports no ``hephaestus`` module: it
must run under a bare ``python3`` in a broken environment (no ``uv sync``, no
editable install), because that is precisely when a restore is needed.
Credentials and secrets are never archived.

Usage:
    # Archive tier-3 state to ~/.hephaestus-backups/
    python3 scripts/backup_state.py backup

    # Read-only integrity drill against an archive (exit 0 pass, 1 fail)
    python3 scripts/backup_state.py verify <archive.tar.gz>

    # Restore an archive into the repo (refuses non-empty target without --force)
    python3 scripts/backup_state.py restore <archive.tar.gz> --force

Exit codes:
    0  success (or verify: all members intact)
    1  verify failure (a member's digest did not match the manifest)
    2  usage error, or a restore refused because the target was non-empty
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Tier-3, local-only state. Tiers 1-2 are re-derived, not archived (ADR-0012).
INVENTORY: tuple[str, ...] = ("build/.issue_implementer",)

# Manifest member name inside the archive; maps each member to its digest+size.
MANIFEST_NAME = "manifest.json"

_ARCHIVE_PREFIX = "hephaestus-state-"


class RestoreError(Exception):
    """Raised when a restore cannot be performed safely (fail closed)."""


def _sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _iter_inventory_files(repo_root: Path) -> list[Path]:
    """Return every regular file under any INVENTORY prefix, sorted by path."""
    files: list[Path] = []
    for prefix in INVENTORY:
        base = repo_root / prefix
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*") if p.is_file())
    return sorted(files)


def cmd_backup(repo_root: Path, output_dir: Path, timestamp: str) -> Path:
    """Archive INVENTORY paths to ``<output_dir>/hephaestus-state-<timestamp>.tar.gz``.

    The archive stores each file under its repo-relative POSIX path plus a
    ``manifest.json`` mapping every member to its SHA-256 digest and byte size.
    A repo with no tier-3 state produces a valid archive with an empty member
    map (an empty backup is a legitimate state, not an error).

    Args:
        repo_root: Repository root the inventory paths are relative to.
        output_dir: Directory to write the archive into (created if absent).
        timestamp: Timestamp component of the archive filename (UTC, caller-supplied
            so library behavior is deterministic under test).

    Returns:
        The path to the written archive.

    """
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{_ARCHIVE_PREFIX}{timestamp}.tar.gz"

    members: dict[str, dict[str, object]] = {}
    with tarfile.open(archive_path, "w:gz") as tar:
        for file_path in _iter_inventory_files(repo_root):
            rel = file_path.relative_to(repo_root).as_posix()
            data = file_path.read_bytes()
            members[rel] = {"sha256": _sha256_bytes(data), "size": len(data)}
            tar.add(file_path, arcname=rel)

        manifest = json.dumps({"members": members}, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        import io

        tar.addfile(info, io.BytesIO(manifest))

    return archive_path


def _read_manifest(tar: tarfile.TarFile) -> dict[str, dict[str, object]]:
    """Return the ``members`` mapping from an archive's manifest.

    Raises:
        RestoreError: The archive has no readable ``manifest.json``.

    """
    try:
        member = tar.extractfile(MANIFEST_NAME)
    except KeyError:
        member = None
    if member is None:
        raise RestoreError(f"archive is missing {MANIFEST_NAME}")
    manifest = json.loads(member.read().decode("utf-8"))
    result: dict[str, dict[str, object]] = manifest.get("members", {})
    return result


def _is_within(root: Path, target: Path) -> bool:
    """Return True if ``target`` resolves to a path inside ``root`` (or equals it)."""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def cmd_verify(archive: Path) -> int:
    """Read-only integrity drill: recompute every member digest against the manifest.

    Extracts the archive to a throwaway temp dir, compares each member's SHA-256
    to the manifest, and prints per-member ``PASS``/``FAIL``. Never mutates the
    repo or the archive.

    Returns:
        0 if every member matches its recorded digest, 1 otherwise.

    """
    ok = True
    with tarfile.open(archive, "r:gz") as tar:
        manifest = _read_manifest(tar)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            for name, meta in sorted(manifest.items()):
                extracted = tar.extractfile(name)
                data = extracted.read() if extracted is not None else None
                if data is None:
                    print(f"FAIL {name} (missing from archive)")
                    ok = False
                    continue
                actual = _sha256_bytes(data)
                if actual == meta.get("sha256"):
                    print(f"PASS {name}")
                else:
                    print(f"FAIL {name} (digest mismatch)")
                    ok = False
            _ = tmp_root  # temp dir reserved for future streamed extraction
    return 0 if ok else 1


def cmd_restore(repo_root: Path, archive: Path, *, force: bool = False) -> None:
    """Restore ``archive`` into ``repo_root`` after verifying every manifest digest.

    Fail-closed guarantees:
    - Every member is verified against the manifest digest *before* anything is
      written; a single mismatch aborts with nothing written.
    - A member whose resolved path escapes ``repo_root`` (tar path traversal) is
      rejected before any write.
    - If any INVENTORY target directory is already non-empty, the restore is
      refused unless ``force`` is set, so a restore never silently clobbers state.

    Raises:
        RestoreError: On digest mismatch, path traversal, or a non-empty target
            without ``force``. The repository is left untouched in every case.

    """
    with tarfile.open(archive, "r:gz") as tar:
        manifest = _read_manifest(tar)

        # Refuse to overwrite populated tier-3 targets unless forced.
        if not force:
            for prefix in INVENTORY:
                base = repo_root / prefix
                if base.exists() and any(base.rglob("*")):
                    raise RestoreError(f"target {base} is not empty; pass force=True to overwrite")

        repo_resolved = repo_root.resolve()
        staged: dict[Path, bytes] = {}
        for name, meta in manifest.items():
            dest = (repo_root / name).resolve()
            if not _is_within(repo_resolved, dest):
                raise RestoreError(f"refusing member outside repo root: {name!r}")
            extracted = tar.extractfile(name)
            data = extracted.read() if extracted is not None else None
            if data is None:
                raise RestoreError(f"member missing from archive: {name!r}")
            if _sha256_bytes(data) != meta.get("sha256"):
                raise RestoreError(f"digest mismatch for {name!r}; refusing restore")
            staged[dest] = data

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
        timestamp = args.timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = cmd_backup(repo_root, output_dir, timestamp)
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
