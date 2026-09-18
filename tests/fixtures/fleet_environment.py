"""Build synthetic unstarted metadata without claiming a live attachment."""

from __future__ import annotations

import json
from pathlib import Path

from hephaestus.automation.fleet_attachment import binding_digest
from hephaestus.automation.fleet_containment import ContainerSpec
from hephaestus.automation.fleet_environments import EnvironmentLease


def environment_lease(
    *,
    worker_id: str,
    session_id: str,
    generation: int,
    environment_id: str,
    container_id: str,
    image_digest: str,
    workspace: Path,
    attachment_program: Path,
    execution_id: str,
    socket_path: Path,
    lease_id: str,
) -> EnvironmentLease:
    """Use a consistent canonical document for parser and routing-only fixtures."""
    spec = ContainerSpec(
        worker_id, session_id, execution_id, generation, workspace, image_digest, 1, 1024**3, 128
    )
    document = {
        "schema": "hi/fleet/containment/v1",
        "leaseId": lease_id,
        "spec": spec.document(),
        "engine": {"socket": "/synthetic-unstarted-engine.sock", "ownerUid": 0},
        "containerId": container_id,
    }
    return EnvironmentLease(
        worker_id=worker_id,
        session_id=session_id,
        generation=generation,
        environment_id=environment_id,
        container_id=container_id,
        image_digest=image_digest,
        workspace=workspace,
        attachment_program=attachment_program,
        execution_id=execution_id,
        socket_path=socket_path,
        lease_id=lease_id,
        binding_digest=binding_digest(document),
        binding_document=json.dumps(document, sort_keys=True),
    )
