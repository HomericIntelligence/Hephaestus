"""Keep current and future tool mounts outside all declared private authorities."""

from dataclasses import replace

import pytest

from tests.unit.automation.test_fleet_containment import Engine, Kernel, specification

pytestmark = pytest.mark.precommit


@pytest.mark.parametrize("placement", ["inside", "parent", "equal"])
def test_future_workspace_cannot_overlap_registered_authority(tmp_path, placement):
    """A new admission cannot expose a previously registered private root."""
    from hephaestus.automation.fleet_containment import ContainedExecSupervisor

    private = tmp_path / "authority"
    private.mkdir(mode=0o700)
    workspace = private / "child" if placement == "inside" else private
    if placement == "parent":
        workspace = tmp_path
    workspace.mkdir(exist_ok=True)
    engine = Engine()
    owner = ContainedExecSupervisor(
        tmp_path / "state", engine, Kernel(engine), protected_roots=(private,)
    )
    try:
        with pytest.raises(ValueError, match="supervisor_storage_overlap"):
            owner.create(replace(specification(tmp_path), workspace=workspace))
        assert "create" not in engine.calls
    finally:
        owner.close()


def test_existing_lease_cannot_be_reopened_with_overlapping_authority(tmp_path):
    """Restart must check every unresolved workspace against the supplied private roots."""
    from hephaestus.automation.fleet_containment import ContainedExecSupervisor

    engine = Engine()
    spec = specification(tmp_path)
    owner = ContainedExecSupervisor(tmp_path / "state", engine, Kernel(engine))
    owner.create(spec)
    owner.close()
    private = spec.workspace / "new-authority"
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="supervisor_storage_overlap"):
        ContainedExecSupervisor(
            tmp_path / "state", engine, Kernel(engine), protected_roots=(private,)
        )


def test_restart_retains_previously_registered_private_roots(tmp_path):
    """Omitting a private root from later configuration cannot grant a new mount."""
    from hephaestus.automation.fleet_containment import ContainedExecSupervisor

    private = tmp_path / "authority"
    private.mkdir(mode=0o700)
    engine = Engine()
    owner = ContainedExecSupervisor(
        tmp_path / "state", engine, Kernel(engine), protected_roots=(private,)
    )
    owner.close()
    owner = ContainedExecSupervisor(tmp_path / "state", engine, Kernel(engine))
    try:
        with pytest.raises(ValueError, match="supervisor_storage_overlap"):
            owner.create(replace(specification(tmp_path), workspace=private))
        assert "create" not in engine.calls
    finally:
        owner.close()


def test_missing_private_root_does_not_leave_a_journal_writer_locked(tmp_path):
    """An invalid configuration must fail before it takes persistent ownership."""
    from hephaestus.automation.fleet_containment import ContainedExecSupervisor

    engine = Engine()
    with pytest.raises(FileNotFoundError):
        ContainedExecSupervisor(
            tmp_path / "state", engine, Kernel(engine), protected_roots=(tmp_path / "missing",)
        )
    owner = ContainedExecSupervisor(tmp_path / "state", engine, Kernel(engine))
    owner.close()
