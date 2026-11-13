"""Reject invalid intake path authority without creating worker state."""

import json
from pathlib import Path

import pytest

from hephaestus.automation.repo_intake import RepoIntakeError
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from tests.unit.automation.test_repo_intake import _make_repository, _manager, _run_git


def _durable_root(intake: object) -> Path:
    """Return the durable state root that outlives every launch."""
    return intake.state_root.parent.parent  # type: ignore[attr-defined]


@pytest.mark.precommit
@pytest.mark.parametrize(
    "damage",
    ["missing", "json", "size", "state", "identity", "ownership", "common", "symlink"],
)
def test_invalid_intake_registration_never_selects_a_local_worker_base(
    tmp_path: Path, damage: str
) -> None:
    """Invalid registration authority must not fall back to the intake checkout."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    registration_path = _durable_root(intake) / "intake-registration.json"
    payload = json.loads(registration_path.read_text())
    if damage == "missing":
        registration_path.unlink()
    elif damage == "json":
        registration_path.write_text("invalid JSON")
    elif damage == "size":
        registration_path.write_bytes(b"x" * 65537)
    elif damage == "symlink":
        saved = _durable_root(intake) / "retained-registration.json"
        registration_path.rename(saved)
        registration_path.symlink_to(saved)
    else:
        field, value = {
            "state": ("state_root", str(tmp_path / "another-state")),
            "identity": ("repository_identity", "another-identity"),
            "ownership": ("ownership_key", "another-owner"),
            "common": ("common_dir", str(tmp_path / "another-common")),
        }[damage]
        payload[field] = value
        registration_path.write_text(json.dumps(payload))
    before = registration_path.read_bytes() if registration_path.exists() else None

    with pytest.raises(RepoIntakeError):
        SourceWorkspaceManager(intake.path, repository="repo")

    assert not (intake.path / "build").exists()
    assert _run_git(intake.path, "status", "--porcelain").stdout == ""
    assert (registration_path.read_bytes() if registration_path.exists() else None) == before


@pytest.mark.precommit
def test_intake_worker_base_rejects_a_redirected_build_directory(tmp_path: Path) -> None:
    """A redirected state directory must not receive worker files."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (_durable_root(intake) / "build").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepoIntakeError, match="base"):
        SourceWorkspaceManager(intake.path, repository="repo")

    assert list(outside.iterdir()) == []
    assert not (intake.path / "build").exists()


@pytest.mark.precommit
def test_intake_worker_base_survives_launch_cleanup(tmp_path: Path) -> None:
    """The durable registration still answers after its launch intake is gone."""
    from hephaestus.automation.repo_intake import intake_worker_base
    from hephaestus.git.runtime import git_metadata_lock_path

    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote, "cleanup-launch")
    intake = manager.prepare()
    common_dir = git_metadata_lock_path(caller).parent.resolve(strict=True)
    assert intake_worker_base(intake.path, common_dir, repository="acme/repo") is not None

    manager.cleanup()

    assert not intake.path.exists()
    base = intake_worker_base(intake.path, common_dir, repository="acme/repo")
    assert base == _durable_root(intake) / "build" / ".worktrees"


@pytest.mark.precommit
def test_intake_worker_base_rejects_a_foreign_active_launch_root(tmp_path: Path) -> None:
    """A root from an earlier launch cannot satisfy the worker-base equality."""
    from hephaestus.automation.repo_intake import intake_worker_base
    from hephaestus.git.runtime import git_metadata_lock_path

    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote, "first-launch").prepare()
    second = _manager(caller, remote, "second-launch").prepare()
    common_dir = git_metadata_lock_path(caller).parent.resolve(strict=True)

    assert intake_worker_base(second.path, common_dir, repository="acme/repo") is not None
    # The first launch's root is not the active launch root, so the trust
    # boundary answers quietly instead of admitting a stale checkout.
    assert intake_worker_base(first.path, common_dir, repository="acme/repo") is None
