"""Check Fleet storage and platform admission without any host credentials."""

import pytest

pytestmark = pytest.mark.precommit


def test_native_macos_is_not_admitted_with_unverified_process_scratch():
    """Reject the pinned native process policy with shared scratch allowances."""
    from hephaestus.automation.fleet_isolation import require_execution_platform

    with pytest.raises(ValueError, match="native_macos_requires_isolated_linux_worker"):
        require_execution_platform("darwin")


def test_linux_platform_alone_does_not_prove_session_isolation():
    """Require enforced session boundaries before Linux can admit issue work."""
    from hephaestus.automation.fleet_isolation import require_execution_platform

    with pytest.raises(ValueError, match="linux_execution_requires_verified_boundary"):
        require_execution_platform("linux")


@pytest.mark.parametrize("private_name", ["auth", "state"])
def test_private_worker_storage_rejects_shared_scratch(tmp_path, private_name):
    """Keep authority data outside globally shared scratch directories."""
    from hephaestus.automation.fleet_isolation import validate_worker_storage

    shared = tmp_path / "shared-scratch"
    shared.mkdir()
    paths = {name: tmp_path / name for name in ("auth", "state", "workspaces")}
    paths[private_name] = shared / private_name
    with pytest.raises(ValueError, match="private_storage_in_shared_scratch"):
        validate_worker_storage(*paths.values(), scratch_roots=(shared,))


@pytest.mark.parametrize("private_name", ["auth", "state"])
def test_private_storage_cannot_overlap_workspaces(tmp_path, private_name):
    """A writable task root cannot contain authentication or worker records."""
    from hephaestus.automation.fleet_isolation import validate_worker_storage

    root = tmp_path / "workspaces"
    paths = {name: tmp_path / name for name in ("auth", "state", "workspaces")}
    paths[private_name] = root / private_name
    with pytest.raises(ValueError, match="worker_storage_overlap"):
        validate_worker_storage(*paths.values(), scratch_roots=())


def test_private_storage_rejects_a_shared_scratch_symlink(tmp_path):
    """Resolve existing path aliases before checking authority storage placement."""
    from hephaestus.automation.fleet_isolation import validate_worker_storage

    shared = tmp_path / "shared"
    shared.mkdir()
    alias = tmp_path / "private-looking"
    alias.symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError, match="private_storage_in_shared_scratch"):
        validate_worker_storage(
            alias / "auth", tmp_path / "state", tmp_path / "work", scratch_roots=(shared,)
        )


def test_tool_home_rejects_preexisting_symlink(tmp_path):
    """Reject a repository-provided link in place of private session runtime data."""
    from hephaestus.automation.fleet_isolation import session_environment

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".fleet-runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="private_directory_symlink"):
        session_environment(workspace)
