"""Bind logical sessions to fixed contained exec-server attachments.

This module controls selection only. It cannot authorize process execution or
confirm container cleanup. The runtime must enforce those separate boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_attachment import AttachmentEndpoint, binding_digest


@dataclass(frozen=True)
class EnvironmentLease:
    """Identify one external tool container and its sole workspace owner."""

    worker_id: str
    session_id: str
    generation: int
    environment_id: str
    container_id: str
    image_digest: str
    workspace: Path
    attachment_program: Path
    execution_id: str | None = None
    socket_path: Path | None = None
    lease_id: str | None = None
    binding_digest: str | None = None
    binding_document: str | None = None

    def __post_init__(self) -> None:
        """Reject partial identities and unbounded launcher inputs."""
        for identity in (self.worker_id, self.session_id, self.environment_id):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identity):
                raise ValueError("invalid_environment_identity")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("invalid_environment_generation")
        if not re.fullmatch(r"[0-9a-f]{64}", self.container_id):
            raise ValueError("invalid_container_identity")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("invalid_image_digest")
        if not self.attachment_program.is_absolute():
            raise ValueError("attachment_program_must_be_absolute")
        workspace = self.workspace.resolve(strict=True)
        if self.workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("invalid_environment_workspace")
        object.__setattr__(self, "workspace", workspace)

    @classmethod
    def from_endpoint(
        cls, endpoint: AttachmentEndpoint, environment_id: str, program: Path
    ) -> EnvironmentLease:
        """Use the supervisor's immutable assignment as the registry source."""
        lease = endpoint.supervisor.inspect(endpoint.lease_id)
        spec = lease["spec"]
        return cls(
            worker_id=spec["workerId"],
            session_id=spec["sessionId"],
            execution_id=spec["executionId"],
            generation=spec["generation"],
            environment_id=environment_id,
            container_id=lease["containerId"],
            image_digest=spec["imageDigest"],
            workspace=Path(spec["workspace"]),
            attachment_program=program,
            socket_path=endpoint.path,
            lease_id=endpoint.lease_id,
            binding_digest=endpoint.binding_digest,
            binding_document=json.dumps(
                {key: lease[key] for key in ("schema", "leaseId", "spec", "engine", "containerId")},
                sort_keys=True,
            ),
        )


def _validate_attachment(lease: EnvironmentLease) -> None:
    """Bind every declared assignment field to the endpoint's canonical lease."""
    if (
        lease.socket_path is None
        or not lease.socket_path.is_absolute()
        or not isinstance(lease.lease_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", lease.lease_id) is None
        or not isinstance(lease.binding_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", lease.binding_digest) is None
        or not isinstance(lease.binding_document, str)
    ):
        raise ValueError("supervised_attachment_required")
    expected = {
        "workerId": lease.worker_id,
        "sessionId": lease.session_id,
        "executionId": lease.execution_id,
        "generation": lease.generation,
        "workspace": str(lease.workspace),
        "imageDigest": lease.image_digest,
    }
    try:
        document = json.loads(lease.binding_document)
        actual = {field: document["spec"][field] for field in expected}
        matches = (
            actual == expected
            and document["containerId"] == lease.container_id
            and document["leaseId"] == lease.lease_id
            and binding_digest(document) == lease.binding_digest
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("environment_binding_mismatch") from error
    if not matches:
        raise ValueError("environment_binding_mismatch")


class EnvironmentRegistry:
    """Write an immutable runtime inventory with no implicit local selection."""

    def __init__(self, codex_home: Path, leases: Iterable[EnvironmentLease]) -> None:
        """Reject shared identities before writing provider configuration."""
        self._leases = tuple(leases)
        if not 1 <= len(self._leases) <= 24:
            raise ValueError("invalid_environment_capacity")
        for index, lease in enumerate(self._leases):
            for other in self._leases[:index]:
                if (
                    lease.session_id == other.session_id
                    or lease.environment_id == other.environment_id
                    or lease.container_id == other.container_id
                    or lease.workspace.is_relative_to(other.workspace)
                    or other.workspace.is_relative_to(lease.workspace)
                ):
                    raise ValueError("environment_lease_overlap")
        home = codex_home.resolve(strict=True)
        if codex_home.is_symlink() or home.stat().st_mode & 0o077:
            raise ValueError("codex_home_must_be_private")
        if any(
            home.is_relative_to(item.workspace) or item.workspace.is_relative_to(home)
            for item in self._leases
        ):
            raise ValueError("worker_storage_overlap")
        for lease in self._leases:
            if lease.socket_path is not None and any(
                lease.socket_path.parent.resolve().is_relative_to(item.workspace)
                or item.workspace.is_relative_to(lease.socket_path.parent.resolve())
                for item in self._leases
            ):
                raise ValueError("worker_storage_overlap")
        self.path = home / "environments.toml"
        self._contents = self._serialize()

    def _serialize(self) -> bytes:
        bindings = json.dumps(
            [asdict(item) for item in self._leases], default=str, sort_keys=True
        ).encode()
        digest = hashlib.sha256(bindings).hexdigest()
        lines = [f"# fleetLeaseDigest = {digest}", "include_local = false", 'default = "none"']
        for lease in self._leases:
            _validate_attachment(lease)
            values = {
                "id": lease.environment_id,
                "program": str(lease.attachment_program),
                "args": [
                    "-m",
                    "hephaestus.automation.fleet_attachment",
                    "--socket",
                    str(lease.socket_path),
                    "--lease-id",
                    lease.lease_id,
                    "--binding-digest",
                    lease.binding_digest,
                ],
            }
            lines.append("\n[[environments]]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
        return ("\n".join(lines) + "\n").encode()

    def write_configuration(self) -> Path:
        """Create private configuration once, or verify its exact retained bytes."""
        if self.path.exists() or self.path.is_symlink():
            self._verify_configuration()
            return self.path
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self._verify_configuration()
            return self.path
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(self._contents)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self.path

    def _verify_configuration(self) -> None:
        if (
            self.path.is_symlink()
            or not self.path.is_file()
            or self.path.stat().st_mode & 0o077
            or self.path.read_bytes() != self._contents
        ):
            raise ValueError("environment_configuration_changed")

    def lease_for(self, session: dict[str, Any]) -> EnvironmentLease:
        """Return the fixed lease only while configuration and ownership still match."""
        self._verify_configuration()
        lease = next(
            (item for item in self._leases if item.session_id == session.get("sessionId")), None
        )
        if lease is None or (
            lease.worker_id != session.get("workerId")
            or lease.execution_id != session.get("executionId")
            or type(session.get("generation")) is not int
            or lease.generation != session["generation"]
            or str(lease.workspace) != session.get("workspace")
        ):
            raise ValueError("environment_not_owned")
        return lease

    def parameters(self, session: dict[str, Any], operation: str) -> dict[str, Any]:
        """Return the exact selected environment after checking assignment ownership."""
        lease = self.lease_for(session)
        if operation == "thread/resume":
            raise ValueError("environment_resume_requires_reconciliation")
        if operation not in {"thread/start", "turn/start"}:
            raise ValueError("unsupported_environment_operation")
        return {
            "environments": [
                {
                    "environmentId": lease.environment_id,
                    "cwd": "/workspace",
                    "runtimeWorkspaceRoots": ["/workspace"],
                }
            ]
        }
