"""Tests for the stdlib backup/restore/verify DR tool (``scripts/backup_state.py``).

These tests are the *tested restore* mandated by ADR-0012: they execute a real
backup → destroy → restore round-trip against a temporary repo root, plus
fail-closed tamper and path-traversal guards. Nothing here touches live state.
"""

from __future__ import annotations

import importlib.util
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "backup_state.py"


def _load_module() -> ModuleType:
    """Import ``scripts/backup_state.py`` by file path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("_backup_state", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, "no spec for backup_state.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_state = _load_module()

FIXED_TIMESTAMP = "20260718T120000Z"


def _seed_state(repo_root: Path) -> Path:
    """Create a fake ``build/.issue_implementer`` state dir with representative files."""
    state_dir = repo_root / "build" / ".issue_implementer"
    state_dir.mkdir(parents=True)
    (state_dir / "drive-green-armed-42.json").write_text(
        json.dumps({"issue": 42, "armed": True}), encoding="utf-8"
    )
    (state_dir / "last-ci-fix-99.json").write_text(
        json.dumps({"pr": 99, "head": "abc123"}), encoding="utf-8"
    )
    logs = state_dir / "logs"
    logs.mkdir()
    (logs / "stage.log").write_text("stage output line 1\nstage output line 2\n", encoding="utf-8")
    return state_dir


def _snapshot(root: Path) -> dict[str, bytes]:
    """Return {relative-posix-path: bytes} for every file under ``root``."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_backup_restore_round_trip(tmp_path: Path) -> None:
    """A backup can be restored byte-for-byte after the state dir is destroyed."""
    repo_root = tmp_path / "repo"
    state_dir = _seed_state(repo_root)
    before = _snapshot(state_dir)
    output_dir = tmp_path / "backups"

    archive = backup_state.cmd_backup(repo_root, output_dir, FIXED_TIMESTAMP)
    assert archive.exists()
    assert archive.name == f"hephaestus-state-{FIXED_TIMESTAMP}.tar.gz"

    # Destroy live state.
    import shutil

    shutil.rmtree(state_dir)
    assert not state_dir.exists()

    backup_state.cmd_restore(repo_root, archive)

    after = _snapshot(state_dir)
    assert after == before


def test_backup_writes_manifest_with_digests(tmp_path: Path) -> None:
    """The archive contains a manifest mapping each member to its SHA-256 and size."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)

    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(backup_state.MANIFEST_NAME)
        assert member is not None
        manifest = json.loads(member.read().decode("utf-8"))

    entries = manifest["members"]
    assert "build/.issue_implementer/drive-green-armed-42.json" in entries
    for meta in entries.values():
        assert len(meta["sha256"]) == 64
        assert meta["size"] >= 0


def test_verify_passes_on_untampered_archive(tmp_path: Path) -> None:
    """``verify`` returns 0 for an intact archive."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)
    assert backup_state.cmd_verify(archive) == 0


def _repack_with_tampered_member(archive: Path, dest: Path, target_suffix: str) -> None:
    """Copy ``archive`` to ``dest``, flipping one byte of the member ending in suffix."""
    with tarfile.open(archive, "r:gz") as src, tarfile.open(dest, "w:gz") as out:
        for member in src.getmembers():
            extracted = src.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if member.name.endswith(target_suffix):
                mutated = bytearray(data)
                mutated[0] ^= 0xFF
                data = bytes(mutated)
                member.size = len(data)
            import io

            out.addfile(member, io.BytesIO(data))


def test_verify_detects_tampering(tmp_path: Path) -> None:
    """A single flipped byte in a member makes ``verify`` return 1."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)

    tampered = tmp_path / "tampered.tar.gz"
    _repack_with_tampered_member(archive, tampered, "drive-green-armed-42.json")
    assert backup_state.cmd_verify(tampered) == 1


def test_restore_fails_closed_on_digest_mismatch(tmp_path: Path) -> None:
    """A tampered archive is rejected and leaves the target untouched (fail closed)."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)

    tampered = tmp_path / "tampered.tar.gz"
    _repack_with_tampered_member(archive, tampered, "last-ci-fix-99.json")

    dest_root = tmp_path / "fresh"
    dest_root.mkdir()
    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(dest_root, tampered)

    # Nothing was written on failure.
    assert not (dest_root / "build").exists()


def test_restore_refuses_nonempty_without_force(tmp_path: Path) -> None:
    """Restore refuses to overwrite a populated target unless ``force`` is set."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)

    # State dir already populated with different content.
    (repo_root / "build" / ".issue_implementer" / "drive-green-armed-42.json").write_text(
        "PRE-EXISTING", encoding="utf-8"
    )
    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(repo_root, archive, force=False)

    # Original content preserved (not overwritten).
    content = (repo_root / "build" / ".issue_implementer" / "drive-green-armed-42.json").read_text(
        encoding="utf-8"
    )
    assert content == "PRE-EXISTING"


def test_restore_force_overwrites(tmp_path: Path) -> None:
    """``force=True`` restores over a populated target."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)

    target = repo_root / "build" / ".issue_implementer" / "drive-green-armed-42.json"
    target.write_text("STALE", encoding="utf-8")

    backup_state.cmd_restore(repo_root, archive, force=True)
    restored = json.loads(target.read_text(encoding="utf-8"))
    assert restored == {"issue": 42, "armed": True}


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    """A hand-built archive with a ``../escape`` member is rejected."""
    archive = tmp_path / "evil.tar.gz"
    payload = b"pwned"
    import hashlib
    import io

    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.dumps(
        {"members": {"../escape.txt": {"sha256": digest, "size": len(payload)}}}
    ).encode("utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(backup_state.MANIFEST_NAME)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = len(payload)
        tar.addfile(evil, io.BytesIO(payload))

    dest_root = tmp_path / "repo"
    dest_root.mkdir()
    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(dest_root, archive, force=True)
    assert not (tmp_path / "escape.txt").exists()


def test_backup_only_archives_inventory_paths(tmp_path: Path) -> None:
    """Only INVENTORY prefixes are archived; secrets/other files are excluded."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    # A secret-looking file outside the inventory must never be captured.
    (repo_root / "build").mkdir(exist_ok=True)
    (repo_root / "build" / "credentials.txt").write_text("SECRET", encoding="utf-8")
    (repo_root / ".env").write_text("TOKEN=secret", encoding="utf-8")

    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()

    non_manifest = [n for n in names if n != backup_state.MANIFEST_NAME]
    assert non_manifest, "archive should contain the seeded inventory members"
    for name in non_manifest:
        assert name.startswith("build/.issue_implementer/"), name
    assert "build/credentials.txt" not in names
    assert ".env" not in names


def test_backup_missing_inventory_produces_empty_archive(tmp_path: Path) -> None:
    """Backing up a repo with no state dir yields a manifest with no members."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(backup_state.MANIFEST_NAME)
        assert member is not None
        manifest = json.loads(member.read().decode("utf-8"))
    assert manifest["members"] == {}


def test_cli_backup_then_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """End-to-end CLI: ``backup`` then ``verify`` via ``main(argv)`` exit 0."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    output_dir = tmp_path / "backups"

    rc = backup_state.main(
        [
            "backup",
            "--repo-root",
            str(repo_root),
            "--output",
            str(output_dir),
            "--timestamp",
            FIXED_TIMESTAMP,
        ]
    )
    assert rc == 0
    archive = output_dir / f"hephaestus-state-{FIXED_TIMESTAMP}.tar.gz"
    assert archive.exists()

    rc = backup_state.main(["verify", str(archive)])
    assert rc == 0


def test_cli_verify_failure_returns_one(tmp_path: Path) -> None:
    """``verify`` on a tampered archive exits 1 through ``main``."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)
    tampered = tmp_path / "tampered.tar.gz"
    _repack_with_tampered_member(archive, tampered, "stage.log")
    assert backup_state.main(["verify", str(tampered)]) == 1


def test_cli_restore_refused_returns_two(tmp_path: Path) -> None:
    """A refused restore (non-empty target, no --force) exits 2 through ``main``."""
    repo_root = tmp_path / "repo"
    _seed_state(repo_root)
    archive = backup_state.cmd_backup(repo_root, tmp_path / "backups", FIXED_TIMESTAMP)
    # Populate target so restore refuses without --force.
    (repo_root / "build" / ".issue_implementer" / "extra.json").write_text("x", encoding="utf-8")
    rc = backup_state.main(["restore", str(archive), "--repo-root", str(repo_root)])
    assert rc == 2


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` prints usage and exits 0 (guards the scripts smoke test)."""
    with pytest.raises(SystemExit) as exc:
        backup_state.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "backup" in out and "restore" in out and "verify" in out
