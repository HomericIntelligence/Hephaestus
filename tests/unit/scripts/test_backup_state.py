"""Tests for the stdlib backup/restore/verify DR tool (``scripts/backup_state.py``).

These tests are the *tested restore* mandated by ADR-0012: they execute a real
backup → destroy → restore round-trip against a temporary repo root, plus
fail-closed tamper and path-traversal guards. Nothing here touches live state.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import tarfile
from collections.abc import Sequence
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
_STATE_MEMBER = "build/.issue_implementer/state.json"
_STATE_BYTES = b"state payload"


def _regular_member(name: str, data: bytes) -> tarfile.TarInfo:
    """Return a regular tar member whose size matches ``data``."""
    member = tarfile.TarInfo(name)
    member.size = len(data)
    return member


def _manifest_for(entries: dict[str, bytes]) -> bytes:
    """Build valid manifest bytes for a mapping of archive names to payloads."""
    return json.dumps(
        {
            "members": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for name, data in entries.items()
            }
        }
    ).encode("utf-8")


def _write_test_archive(
    path: Path,
    entries: Sequence[tuple[tarfile.TarInfo, bytes | None]],
    manifest: bytes,
    *,
    include_manifest: bool = True,
) -> Path:
    """Write an archive with caller-controlled members and manifest bytes."""
    with tarfile.open(path, "w:gz") as tar:
        if include_manifest:
            manifest_info = tarfile.TarInfo(backup_state.MANIFEST_NAME)
            manifest_info.size = len(manifest)
            tar.addfile(manifest_info, io.BytesIO(manifest))
        for member, data in entries:
            stream = io.BytesIO(data) if data is not None else None
            tar.addfile(member, stream)
    return path


def _archive_with_regular_members(path: Path, entries: dict[str, bytes]) -> Path:
    """Write a valid archive containing exactly the supplied regular members."""
    members = [(_regular_member(name, data), data) for name, data in entries.items()]
    return _write_test_archive(path, members, _manifest_for(entries))


def _write_invalid_archive(path: Path, case: str) -> Path:
    """Write one explicitly malformed archive-index case for restore tests."""
    data = _STATE_BYTES

    if case in {"absolute", "parent_traversal", "backslash", "unauthorized"}:
        names = {
            "absolute": "/tmp/pwned",
            "parent_traversal": "../pwned",
            "backslash": "build\\.issue_implementer\\state.json",
            "unauthorized": "pyproject.toml",
        }
        name = names[case]
        return _archive_with_regular_members(path, {name: data})

    if case == "undeclared":
        member = _regular_member(_STATE_MEMBER, data)
        return _write_test_archive(path, [(member, data)], b'{"members": {}}')

    if case == "missing":
        manifest = _manifest_for({_STATE_MEMBER: data})
        return _write_test_archive(path, [], manifest)

    if case == "duplicate":
        entries = [
            (_regular_member(_STATE_MEMBER, data), data),
            (_regular_member(_STATE_MEMBER, data), data),
        ]
        return _write_test_archive(path, entries, _manifest_for({_STATE_MEMBER: data}))

    if case == "normalized_duplicate":
        equivalent = "build/.issue_implementer/./state.json"
        entries = [
            (_regular_member(_STATE_MEMBER, data), data),
            (_regular_member(equivalent, data), data),
        ]
        return _write_test_archive(path, entries, _manifest_for({_STATE_MEMBER: data}))

    if case == "duplicate_manifest":
        duplicate = _regular_member(backup_state.MANIFEST_NAME, b"{}")
        return _write_test_archive(
            path,
            [(duplicate, b"{}")],
            _manifest_for({}),
        )

    if case == "nonregular_manifest":
        manifest_link = tarfile.TarInfo(backup_state.MANIFEST_NAME)
        manifest_link.type = tarfile.SYMTYPE
        manifest_link.linkname = _STATE_MEMBER
        return _write_test_archive(
            path,
            [(manifest_link, None)],
            b'{"members": {}}',
            include_manifest=False,
        )

    if case in {"directory", "symbolic_link", "hard_link", "device"}:
        member = tarfile.TarInfo(_STATE_MEMBER)
        if case == "directory":
            member.type = tarfile.DIRTYPE
        elif case == "symbolic_link":
            member.type = tarfile.SYMTYPE
            member.linkname = "outside"
        elif case == "hard_link":
            member.type = tarfile.LNKTYPE
            member.linkname = "outside"
        else:
            member.type = tarfile.CHRTYPE
            member.devmajor = 1
            member.devminor = 3
        return _write_test_archive(
            path,
            [(member, None)],
            _manifest_for({_STATE_MEMBER: b""}),
        )

    raise AssertionError(f"unknown invalid archive case: {case}")


MALFORMED_MANIFESTS: list[bytes] = [
    b"\xff",
    b"{not-json",
    b"[]",
    b"{}",
    b'{"members": []}',
    b'{"members": {}, "extra": true}',
    b'{"members": {"build/.issue_implementer/state.json": []}}',
    b'{"members": {"build/.issue_implementer/state.json": {}}}',
    b'{"members": {"build/.issue_implementer/state.json": {"sha256": "' + b"0" * 64 + b'"}}}',
    b'{"members": {"build/.issue_implementer/state.json": {"size": 1}}}',
    b'{"members": {"build/.issue_implementer/state.json": {"sha256": "short", "size": 1}}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"z" * 64 + b'", "size": 1}}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "size": true}}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "size": -1}}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "size": 1.5}}}',
    b'{"members": {"pyproject.toml": {"sha256": "' + b"0" * 64 + b'", "size": 1}}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "size": 1}, '
    b'"build/.issue_implementer/./state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "size": 1}}}',
    b'{"members": {}, "members": {}}',
    b'{"members": {"build/.issue_implementer/state.json": '
    b'{"sha256": "' + b"0" * 64 + b'", "sha256": "' + b"1" * 64 + b'", '
    b'"size": 1}}}',
]


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


@pytest.mark.parametrize("name", ["", ".", "\x00", "build/\x00/state"])
def test_canonical_member_name_rejects_empty_or_nul(name: str) -> None:
    """Empty, current-directory, and NUL-containing names fail closed."""
    with pytest.raises(backup_state.RestoreError):
        backup_state._canonical_member_name(name)


def test_restore_rejects_in_repo_member_outside_inventory_without_writing(
    tmp_path: Path,
) -> None:
    """A digest-valid archive cannot overwrite an unauthorized repo file."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    protected = repo_root / "pyproject.toml"
    protected.write_bytes(b"SAFE")

    archive = _archive_with_regular_members(
        tmp_path / "evil.tar.gz",
        {"pyproject.toml": b"PWNED"},
    )

    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(repo_root, archive, force=True)

    assert protected.read_bytes() == b"SAFE"
    assert not (repo_root / "build" / ".issue_implementer").exists()


def test_restore_rejects_inventory_symlink_redirect_without_writing(tmp_path: Path) -> None:
    """An authorized-looking member cannot follow an inventory symlink outside it."""
    repo_root = tmp_path / "repo"
    inventory = repo_root / "build" / ".issue_implementer"
    inventory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (inventory / "redirect").symlink_to(outside, target_is_directory=True)

    payload = b"PWNED"
    name = "build/.issue_implementer/redirect/state.json"
    archive = _archive_with_regular_members(tmp_path / "redirect.tar.gz", {name: payload})

    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(repo_root, archive, force=True)

    assert not (outside / "state.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        "absolute",
        "parent_traversal",
        "backslash",
        "unauthorized",
        "undeclared",
        "missing",
        "duplicate",
        "normalized_duplicate",
        "duplicate_manifest",
        "nonregular_manifest",
        "directory",
        "symbolic_link",
        "hard_link",
        "device",
    ],
)
def test_restore_rejects_invalid_archive_members(tmp_path: Path, case: str) -> None:
    """Unsafe, unknown, duplicate, and non-regular members fail closed."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    archive = _write_invalid_archive(tmp_path / f"{case}.tar.gz", case)

    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(repo_root, archive, force=True)

    assert not (repo_root / "build" / ".issue_implementer").exists()


@pytest.mark.parametrize("manifest", MALFORMED_MANIFESTS)
def test_restore_rejects_malformed_manifest_as_restore_error(
    tmp_path: Path,
    manifest: bytes,
) -> None:
    """Malformed manifests use the fail-closed RestoreError boundary."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    archive = _write_test_archive(tmp_path / "bad.tar.gz", [], manifest)

    with pytest.raises(backup_state.RestoreError):
        backup_state.cmd_restore(repo_root, archive, force=True)

    assert not (repo_root / "build").exists()


def test_verify_rejects_structurally_invalid_archive(tmp_path: Path) -> None:
    """Verify reports an unauthorized archive member as a structural failure."""
    archive = _write_invalid_archive(tmp_path / "bad.tar.gz", "unauthorized")
    assert backup_state.cmd_verify(archive) == 1


def test_verify_rejects_manifest_size_mismatch(tmp_path: Path) -> None:
    """Verify returns one when a valid member's declared size is wrong."""
    payload = b"payload"
    name = _STATE_MEMBER
    manifest = json.dumps(
        {
            "members": {
                name: {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload) + 1,
                }
            }
        }
    ).encode("utf-8")
    archive = _write_test_archive(
        tmp_path / "wrong-size.tar.gz",
        [(_regular_member(name, payload), payload)],
        manifest,
    )
    assert backup_state.cmd_verify(archive) == 1


def test_verify_rejects_malformed_manifest(tmp_path: Path) -> None:
    """Verify reports structural failure instead of leaking a parsing exception."""
    archive = _write_test_archive(tmp_path / "bad.tar.gz", [], b"{not-json")
    assert backup_state.cmd_verify(archive) == 1


def test_cli_restore_malformed_manifest_returns_two(tmp_path: Path) -> None:
    """The CLI keeps its usage/refusal exit code for malformed restores."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    archive = _write_test_archive(tmp_path / "bad.tar.gz", [], b"{not-json")
    assert backup_state.main(["restore", str(archive), "--repo-root", str(repo_root)]) == 2


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
