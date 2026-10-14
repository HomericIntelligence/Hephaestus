"""Reject invalid intake path authority without creating worker state."""

import json
from pathlib import Path

import pytest

from hephaestus.automation.repo_intake import RepoIntakeError
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_repo_intake import _make_repository, _manager, _run_git


@pytest.mark.precommit
@pytest.mark.parametrize(
    "damage",
    ["missing", "mode", "json", "size", "state", "identity", "ownership", "common", "symlink"],
)
def test_invalid_intake_receipt_never_selects_a_local_worker_base(
    tmp_path: Path, damage: str
) -> None:
    """Invalid receipt authority must not fall back to the intake checkout."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    receipt_path = intake.state_root / "receipt.json"
    payload = json.loads(receipt_path.read_text())
    if damage == "missing":
        receipt_path.unlink()
    elif damage == "mode":
        receipt_path.chmod(0o644)
    elif damage == "json":
        receipt_path.write_text("invalid JSON")
    elif damage == "size":
        receipt_path.write_bytes(b"x" * 65537)
    elif damage == "symlink":
        saved = intake.state_root / "retained-receipt.json"
        receipt_path.rename(saved)
        receipt_path.symlink_to(saved)
    else:
        field, value = {
            "state": ("state_root", str(tmp_path / "another-state")),
            "identity": ("repository_identity", "another-identity"),
            "ownership": ("ownership_key", "another-owner"),
            "common": ("common_dir", str(tmp_path / "another-common")),
        }[damage]
        payload[field] = value
        receipt_path.write_text(json.dumps(payload))
    before = receipt_path.read_bytes() if receipt_path.exists() else None

    with pytest.raises(RepoIntakeError):
        SourceWorkspaceManager(intake.path, repository="repo")

    assert not (intake.path / "build").exists()
    assert _run_git(intake.path, "status", "--porcelain").stdout == ""
    assert (receipt_path.read_bytes() if receipt_path.exists() else None) == before


@pytest.mark.precommit
def test_intake_worker_base_rejects_a_redirected_build_directory(tmp_path: Path) -> None:
    """A redirected state directory must not receive worker files."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (intake.state_root / "build").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepoIntakeError, match="base"):
        SourceWorkspaceManager(intake.path, repository="repo")

    assert list(outside.iterdir()) == []
    assert not (intake.path / "build").exists()
