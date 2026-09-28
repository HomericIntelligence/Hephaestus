"""Check restricted Codex thread startup through one supervised tool attachment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_attachment import AttachmentEndpoint
from hephaestus.automation.fleet_containment import ContainedExecSupervisor, ContainerSpec
from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry
from hephaestus.automation.fleet_podman import LinuxKernel, PodmanEngine
from hephaestus.automation.fleet_provider import CodexAppServer, ProviderError
from hephaestus.automation.fleet_worker import FleetWorker


class StartupOnlyProvider(CodexAppServer):
    """Refuse model turns, account operations, and direct tool invocation."""

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Allow only initialization, status inspection, and thread metadata setup."""
        if method not in {"initialize", "environment/status", "thread/start"}:
            raise ValueError("probe_rpc_outside_scope")
        return super().request(method, params, timeout=timeout)


def run_probe(
    root: Path, engine_program: Path, socket_path: Path, codex_program: Path, image: str
) -> dict[str, Any]:
    """Use a new empty runtime and retain actual startup and disposal observations."""
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    root = root.resolve(strict=True)
    home, spool, state, workspaces = (
        root / name for name in ("codex-home", "spool", "worker", "workspaces")
    )
    for path in (home, spool, state, workspaces):
        path.mkdir(mode=0o700)
    workspace = workspaces / "synthetic-session"
    workspace.mkdir(mode=0o700)
    (workspace / "AGENTS.md").write_text("# Synthetic startup marker\nNo model task is assigned.\n")
    engine = PodmanEngine(engine_program, socket_path, root / "engine-home")
    owner = ContainedExecSupervisor(
        root / "supervisor", engine, LinuxKernel(), protected_roots=(home, spool, state)
    )
    worker = None
    endpoint = None
    listener = None
    lease = None
    result: dict[str, Any] = {
        "schema": "hi/fleet/contained-startup-probe/v1",
        "passed": False,
        "authorizesAdmission": False,
        "normalModelToolRoute": False,
        "threadStartAccepted": False,
        "codexExecutableSha256": hashlib.sha256(codex_program.read_bytes()).hexdigest(),
    }
    stage = "create"
    try:
        spec = ContainerSpec(
            "startup-worker",
            "startup-session",
            "startup-execution",
            1,
            workspace,
            image,
            1,
            1024**3,
            128,
        )
        lease = owner.create(spec)
        endpoint = AttachmentEndpoint(owner, lease["leaseId"])
        registration = EnvironmentLease.from_endpoint(endpoint, "startup-env", Path(sys.executable))
        registry = EnvironmentRegistry(home, [registration])
        listener = threading.Thread(target=endpoint.serve_once, name="startup-attachment")
        listener.start()
        worker = FleetWorker(
            state_dir=state,
            workspace_root=workspaces,
            codex_home=home,
            worker_id="startup-worker",
            pool_id="probe",
            host_id="probe",
            generation=1,
            capacity=1,
            environment_registry=registry,
        )
        worker.provider = StartupOnlyProvider([str(codex_program)], home)
        stage = "provider-initialize"
        worker.start()
        result["environmentStatuses"] = {
            name: worker.provider.request("environment/status", {"environmentId": name})["status"]
            for name in ("startup-env", "local", "missing")
        }
        if result["environmentStatuses"] != {
            "startup-env": "pending",
            "local": "unknown",
            "missing": "unknown",
        }:
            raise ValueError("unexpected_environment_inventory")
        session = {
            "workerId": "startup-worker",
            "sessionId": "startup-session",
            "executionId": "startup-execution",
            "generation": 1,
            "workspace": str(workspace),
        }
        stage = "restricted-thread-start"
        # This metadata-only probe does not invoke worker.handle or replace an
        # execution guard. The provider cannot accept a model/tool RPC here.
        thread = worker.provider.request(
            "thread/start",
            {
                **worker._thread_parameters(session),
                "ephemeral": True,
            },
        )
        result["threadStartAccepted"] = True
        result["threadCwd"] = thread["thread"].get("cwd")
        result["attachmentPhase"] = owner.inspect(lease["leaseId"])["phase"]
        if result["threadCwd"] != "/workspace" or result["attachmentPhase"] != "active":
            raise ValueError("startup_binding_not_observed")
        result["passed"] = True
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        result.update(stage=stage, error=type(error).__name__, errorCode=str(error)[:256])
        if isinstance(error, ProviderError) and error.rpc_error is not None:
            # All input and authority storage is synthetic and new. Keep this
            # bounded native diagnostic in the private probe artifact only.
            result["rpcError"] = str(error.rpc_error)[:2048]
    finally:
        try:
            if lease is not None:
                result["disposal"] = owner.dispose(lease["leaseId"])
                if result["disposal"]["phase"] != "disposed":
                    result["passed"] = False
        finally:
            if endpoint is not None:
                endpoint.close()
            if listener is not None:
                listener.join(timeout=2)
            try:
                if worker is not None:
                    worker.close()
            finally:
                try:
                    engine.close()
                finally:
                    owner.close()
        result["authAbsent"] = not (home / "auth.json").exists()
        if not result["authAbsent"]:
            result["passed"] = False
    return result


def main() -> int:
    """Run only the bounded startup probe selected by the local engine operator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("engine_program", type=Path)
    parser.add_argument("socket", type=Path)
    parser.add_argument("codex_program", type=Path)
    parser.add_argument("image")
    args = parser.parse_args()
    result = run_probe(args.root, args.engine_program, args.socket, args.codex_program, args.image)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
