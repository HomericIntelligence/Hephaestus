"""Check exclusive tool-environment routing without authorizing execution."""

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.precommit


def lease(tmp_path, number=1):
    """Use synthetic container identities and disjoint workspace roots."""
    from hephaestus.automation.fleet_environments import EnvironmentLease

    workspace = tmp_path / f"workspace-{number}"
    workspace.mkdir(exist_ok=True)
    return EnvironmentLease(
        worker_id="worker-a",
        session_id=f"session-{number}",
        generation=1,
        environment_id=f"session-{number}-g1",
        container_id=str(number) * 64,
        image_digest="sha256:" + "a" * 64,
        workspace=workspace,
        engine_program=Path("/usr/bin/podman"),
    )


def registry(tmp_path, *leases):
    """Create a private configuration directory for a fixed runtime inventory."""
    from hephaestus.automation.fleet_environments import EnvironmentRegistry

    home = tmp_path / "runtime"
    home.mkdir(mode=0o700, exist_ok=True)
    return EnvironmentRegistry(home, leases)


def assignment(item):
    """Return the authoritative worker assignment used for selection."""
    return {
        "workerId": item.worker_id,
        "sessionId": item.session_id,
        "generation": item.generation,
        "workspace": str(item.workspace),
    }


def test_private_configuration_disables_implicit_and_local_environments(tmp_path):
    """Default selection must not attach every registered logical agent."""
    first, second = lease(tmp_path), lease(tmp_path, 2)
    pool = registry(tmp_path, first, second)
    config = pool.write_configuration()
    parsed = tomllib.loads(config.read_text())
    assert parsed["include_local"] is False
    assert parsed["default"] == "none"
    assert not config.stat().st_mode & 0o077
    assert [item["id"] for item in parsed["environments"]] == [
        first.environment_id,
        second.environment_id,
    ]
    assert parsed["environments"][0]["args"] == [
        "start",
        "--attach",
        "--interactive",
        "--sig-proxy=false",
        first.container_id,
    ]
    assert "auth" not in json.dumps(parsed)


@pytest.mark.parametrize("operation", ["thread/start", "turn/start"])
def test_selection_uses_exactly_one_owned_environment(tmp_path, operation):
    """Each start request carries an explicit singleton selection."""
    first, second = lease(tmp_path), lease(tmp_path, 2)
    pool = registry(tmp_path, first, second)
    pool.write_configuration()
    assert pool.parameters(assignment(first), operation) == {
        "environments": [
            {
                "environmentId": first.environment_id,
                "cwd": "/workspace",
                "runtimeWorkspaceRoots": ["/workspace"],
            }
        ]
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("workerId", "other"),
        ("sessionId", "other"),
        ("generation", 2),
        ("workspace", "/some/other/workspace"),
    ],
)
def test_selection_rejects_cross_owner_or_generation(tmp_path, field, value):
    """A configuration entry cannot replace a fenced assignment."""
    item = lease(tmp_path)
    pool = registry(tmp_path, item)
    pool.write_configuration()
    session = {**assignment(item), field: value}
    with pytest.raises(ValueError, match="environment_not_owned"):
        pool.parameters(session, "turn/start")


@pytest.mark.parametrize("field", ["environment_id", "container_id", "workspace"])
def test_duplicate_execution_boundary_cannot_be_registered(tmp_path, field):
    """Distinct logical sessions must not share a container or workspace."""
    first, second = lease(tmp_path), lease(tmp_path, 2)
    second = replace(second, **{field: getattr(first, field)})
    with pytest.raises(ValueError, match="environment_lease_overlap"):
        registry(tmp_path, first, second)


def test_modified_runtime_configuration_blocks_further_selection(tmp_path):
    """An external configuration edit requires reconciliation before execution."""
    item = lease(tmp_path)
    pool = registry(tmp_path, item)
    path = pool.write_configuration()
    path.write_text("include_local = true\n")
    with pytest.raises(ValueError, match="environment_configuration_changed"):
        pool.parameters(assignment(item), "turn/start")
    with pytest.raises(ValueError, match="environment_configuration_changed"):
        pool.write_configuration()


def test_cold_resume_cannot_infer_a_retained_environment(tmp_path):
    """The pinned resume schema lacks an environment override."""
    item = lease(tmp_path)
    pool = registry(tmp_path, item)
    pool.write_configuration()
    with pytest.raises(ValueError, match="environment_resume_requires_reconciliation"):
        pool.parameters(assignment(item), "thread/resume")


def test_registry_metadata_does_not_authorize_linux_admission(tmp_path):
    """A lease file does not prove process or filesystem isolation."""
    from hephaestus.automation.fleet_isolation import require_execution_platform

    item = lease(tmp_path)
    registry(tmp_path, item).write_configuration()
    with pytest.raises(ValueError, match="linux_execution_requires_verified_boundary"):
        require_execution_platform("linux")


def test_workspace_cannot_be_inside_private_runtime_home(tmp_path):
    """Authority storage must be disjoint from the tool workspace in both directions."""
    item = lease(tmp_path)
    home = tmp_path / "runtime"
    home.mkdir(mode=0o700)
    workspace = home / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="worker_storage_overlap"):
        registry(tmp_path, replace(item, workspace=workspace))


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_id", "other"),
        ("session_id", "other"),
        ("generation", 2),
        ("image_digest", "sha256:" + "b" * 64),
    ],
)
def test_restart_cannot_rebind_retained_environment_metadata(tmp_path, field, value):
    """Every immutable ownership field must survive configuration reconstruction."""
    item = lease(tmp_path)
    registry(tmp_path, item).write_configuration()
    replacement = registry(tmp_path, replace(item, **{field: value}))
    with pytest.raises(ValueError, match="environment_configuration_changed"):
        replacement.write_configuration()
