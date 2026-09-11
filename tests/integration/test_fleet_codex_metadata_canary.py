"""Check the installed Codex profile protocol without authentication or a model turn."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from hephaestus.automation.fleet_provider import CodexAppServer, ProviderError
from hephaestus.automation.fleet_worker import FleetWorker

pytestmark = pytest.mark.integration


@pytest.fixture
def native_probe_root():
    """Create new authority storage outside scratch without opening existing auth."""
    if os.environ.get("FLEET_CODEX_METADATA_CANARY") != "1":
        pytest.skip("Set FLEET_CODEX_METADATA_CANARY=1 for installed-binary no-auth probes")
    configured = os.environ.get("FLEET_NATIVE_PROBE_ROOT")
    assert configured, "FLEET_NATIVE_PROBE_ROOT must identify an empty private probe directory"
    root = Path(configured).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert not root.stat().st_mode & 0o077
    with tempfile.TemporaryDirectory(prefix="no-auth-", dir=root) as directory:
        yield Path(directory)


def test_installed_codex_parses_remote_only_registry_without_starting_environments(
    native_probe_root,
):
    """Ask the actual provider to inspect configured, unstarted environments."""
    from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry

    binary = os.environ.get("FLEET_CODEX_NATIVE_BIN") or shutil.which("codex")
    assert binary is not None
    home = native_probe_root / "empty-codex-home"
    home.mkdir(mode=0o700)
    leases = []
    for number in (1, 2):
        workspace = native_probe_root / f"workspace-{number}"
        workspace.mkdir(mode=0o700)
        leases.append(
            EnvironmentLease(
                worker_id="parser-worker",
                session_id=f"parser-session-{number}",
                generation=1,
                environment_id=f"parser-env-{number}",
                container_id=str(number) * 64,
                image_digest="sha256:" + "a" * 64,
                workspace=workspace,
                engine_program=Path("/fleet-metadata-probe-no-engine"),
            )
        )
    registry = EnvironmentRegistry(home, leases)
    registry.write_configuration()

    class MetadataProvider(CodexAppServer):
        def request(self, method, params):
            assert method in {"initialize", "environment/status"}
            return super().request(method, params)

    provider = MetadataProvider([binary], home)
    try:
        provider.start()
        statuses = {
            name: provider.request("environment/status", {"environmentId": name})["status"]
            for name in ("parser-env-1", "parser-env-2", "local", "missing")
        }
        assert statuses == {
            "parser-env-1": "pending",
            "parser-env-2": "pending",
            "local": "unknown",
            "missing": "unknown",
        }
        assert not (home / "auth.json").exists()
        registry.write_configuration()
        print(json.dumps({"actualRegistryStatus": statuses, "modelOrToolCall": False}))
    finally:
        provider.close()


def test_installed_codex_loads_workspace_profile_without_auth_or_turn(native_probe_root):
    """Record the actual configured profile from an empty native Codex home."""
    if os.environ.get("FLEET_CODEX_METADATA_CANARY") != "1":
        pytest.skip("Set FLEET_CODEX_METADATA_CANARY=1 for the installed-binary metadata probe")
    binary = os.environ.get("FLEET_CODEX_NATIVE_BIN") or shutil.which("codex")
    assert binary is not None, "the pinned Codex binary must be installed"
    tmp_path = native_probe_root
    root = tmp_path / "workspaces"
    workspace = root / "metadata-canary"
    workspace.mkdir(parents=True)
    codex_home = tmp_path / "empty-codex-home"
    codex_home.mkdir(mode=0o700)
    captured = {}

    class RecordingProvider(CodexAppServer):
        def request(self, method, params):
            assert method in {"initialize", "thread/start"}, "metadata canary must not send a turn"
            try:
                result = super().request(method, params)
            except ProviderError as error:
                captured["rpcError"] = error.rpc_error
                raise
            captured[method] = result
            return result

    worker = FleetWorker(
        state_dir=tmp_path / "state",
        workspace_root=root,
        codex_home=codex_home,
        worker_id="metadata-canary",
        pool_id="native-canary",
        host_id="local",
        generation=1,
        capacity=1,
        provider_command=[binary],
    )
    worker.provider = RecordingProvider([binary], codex_home)
    try:
        worker.start()
        result = worker.handle(
            {
                "schema": "hi/fleet/v1",
                "workerId": "metadata-canary",
                "commandId": "canary-1",
                "idempotencyKey": "canary-1",
                "generation": 1,
                "targetKind": "sessions",
                "targetId": "metadata-session",
                "operation": "start",
                "workspace": str(workspace),
                "agentId": "metadata-agent",
                "taskId": "metadata-task",
                "executionId": "metadata-exec",
                "stage": "metadata-probe",
                "payload": {},
            }
        )
        expected_error = {
            "darwin": "native_macos_requires_isolated_linux_worker",
            "linux": "linux_execution_requires_verified_boundary",
        }.get(sys.platform, "unsupported_execution_platform")
        assert result["status"] == "failed", json.dumps(result)
        assert result["receipt"]["error"] == expected_error
        assert worker.inventory()["sessions"] == []
        # Metadata-only profile inspection is deliberately not task admission.
        worker.provider.request(
            "thread/start",
            {**worker._thread_parameters({"workspace": str(workspace)}), "ephemeral": True},
        )
        response = captured["thread/start"]
        assert response["activePermissionProfile"] == {"id": "fleet", "extends": None}
        assert response["cwd"] == str(workspace)
        assert response["runtimeWorkspaceRoots"] == [str(workspace)]
        assert response["approvalPolicy"] == "on-request"
        assert not (codex_home / "auth.json").exists()
        print(
            "Actual Codex 0.153.4 selected Fleet profile with :minimal filesystem reads; "
            "no auth or model turn used."
        )
    finally:
        worker.close()


def test_native_process_boundary_uses_only_synthetic_markers(native_probe_root):
    """Observe process permissions without auth/model calls and retain native admission gate."""
    from hephaestus.automation.fleet_isolation import (
        require_execution_platform,
        shell_environment_policy,
        validate_worker_storage,
    )

    if sys.platform != "darwin":
        pytest.skip("This probe measures the pinned macOS process sandbox")
    binary = os.environ.get("FLEET_CODEX_NATIVE_BIN") or shutil.which("codex")
    assert binary is not None
    home = native_probe_root / "empty-codex-home"
    home.mkdir(mode=0o700)
    workspace = native_probe_root / "workspace"
    workspace.mkdir(mode=0o700)
    sibling = native_probe_root / "sibling"
    sibling.mkdir(mode=0o700)
    state = native_probe_root / "state"
    state.mkdir(mode=0o700)
    validate_worker_storage(home, state, workspace)
    policy = shell_environment_policy(workspace)
    filesystem = {
        ":minimal": "read",
        str(workspace): "write",
        str(home): "deny",
        str(state): "deny",
    }
    lines = ['default_permissions = "fleet"', 'cli_auth_credentials_store = "file"']
    for section, values in (
        ("permissions.fleet.filesystem", filesystem),
        ("permissions.fleet.network", {"enabled": False}),
        ("features", {"shell_snapshot": False}),
        ("shell_environment_policy", {key: value for key, value in policy.items() if key != "set"}),
        ("shell_environment_policy.set", policy["set"]),
    ):
        lines.append(f"[{section}]")
        lines.extend(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in values.items())
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    markers = [workspace / "marker", sibling / "marker", home / "synthetic-marker"]
    for marker in markers:
        marker.write_text("synthetic probe data\n")

    class SyntheticProvider(CodexAppServer):
        """Limit this probe to initialization and a single fixed marker command."""

        def request(self, method, params):
            assert method in {"initialize", "command/exec"}
            if method == "command/exec":
                assert params["command"][:3] == ["/bin/sh", "-c", _SYNTHETIC_PROBE]
                assert params["permissionProfile"] == "fleet"
            return super().request(method, params)

    provider = SyntheticProvider([binary], home)
    try:
        provider.start()
        with tempfile.TemporaryDirectory(prefix="fleet-synthetic-", dir="/private/tmp") as scratch:
            marker = Path(scratch) / "marker"
            marker.write_text("synthetic scratch probe\n")
            result = provider.request(
                "command/exec",
                {
                    "cwd": str(workspace),
                    "permissionProfile": "fleet",
                    "timeoutMs": 3000,
                    "outputBytesCap": 4096,
                    "command": [
                        "/bin/sh",
                        "-c",
                        _SYNTHETIC_PROBE,
                        "fleet-synthetic-probe",
                        *map(str, markers),
                        str(marker),
                        policy["set"]["HOME"],
                    ],
                },
            )
        assert result["exitCode"] == 0, result
        observed = json.loads(result["stdout"])
        assert observed["workspaceRead"] is True
        assert observed["siblingRead"] is False
        assert observed["authorityRead"] is False
        assert observed["privateHome"] is True
        assert observed["providerHomeAbsent"] is True
        # This capability observation is why native macOS cannot admit Fleet work.
        assert observed["sharedScratchRead"] is True
        assert observed["sharedScratchWrite"] is True
        with pytest.raises(ValueError, match="native_macos_requires_isolated_linux_worker"):
            require_execution_platform(sys.platform)
        assert not (home / "auth.json").exists()
        print(
            "Actual Codex 0.153.4 native process observation: "
            + json.dumps(observed, sort_keys=True)
        )
    finally:
        provider.close()


_SYNTHETIC_PROBE = (
    "\nworkspace=false; sibling=false; authority=false; scratch=false; write=false; "
    "private_home=false; absent=false\n"
    'if /bin/cat "$1" >/dev/null 2>&1; then workspace=true; fi\n'
    'if /bin/cat "$2" >/dev/null 2>&1; then sibling=true; fi\n'
    'if /bin/cat "$3" >/dev/null 2>&1; then authority=true; fi\n'
    'if /bin/cat "$4" >/dev/null 2>&1; then scratch=true; fi\n'
    'if printf synthetic > "$4" 2>/dev/null; then write=true; fi\n'
    'if test "$HOME" = "$5"; then private_home=true; fi\n'
    'if test -z "${CODEX_HOME+x}"; then absent=true; fi\n'
    'printf \'{"workspaceRead":%s,"siblingRead":%s,"authorityRead":%s,'
    '"sharedScratchRead":%s,"sharedScratchWrite":%s,"privateHome":%s,'
    '"providerHomeAbsent":%s}\\n\' '
    '"$workspace" "$sibling" "$authority" "$scratch" "$write" "$private_home" "$absent"\n'
)
