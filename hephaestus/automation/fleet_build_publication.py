"""Publish private build evidence from the retained execution owner."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from hephaestus.automation.fleet_build_collection import _identity, _manifest, _receipt
from hephaestus.automation.fleet_build_contract import (
    Json,
    digest,
    encoded,
    equal,
    fields,
    validate_command,
    validate_policy,
)
from hephaestus.automation.fleet_build_executor import _result, budget, read_private_file
from hephaestus.automation.fleet_snapshot import _read_regular
from hephaestus.automation.fleet_snapshot_files import (
    directory,
    private_parent,
    publish as publish_tree,
)
from hephaestus.automation.fleet_snapshot_policy import MAX_MANIFEST_BYTES

if TYPE_CHECKING:
    from hephaestus.automation.fleet_build_supervisor import BuildSnapshot


class BuildEvidenceOwner(Protocol):
    """Supply retained result bytes and exact observed step cleanup."""

    def evidence(self, lease: Json, *, deadline: float) -> Json | None:
        """Read the exact retained lease result without another execution."""
        ...


class PrivateBuildPublisher:
    """Bind private publication to the actual snapshot and execution owner."""

    def __init__(
        self, *, evidence_root: Path, snapshot: BuildSnapshot, owner: BuildEvidenceOwner
    ) -> None:
        """Accept explicit private capabilities without a remote artifact path."""
        if owner is None or snapshot is None:
            raise ValueError("private build evidence capability is unavailable")
        if (
            not evidence_root.is_absolute()
            or evidence_root.resolve(strict=True) != evidence_root
            or not snapshot.artifact.is_absolute()
            or snapshot.artifact.resolve(strict=True) != snapshot.artifact
            or evidence_root.is_relative_to(snapshot.artifact)
            or snapshot.artifact.is_relative_to(evidence_root)
        ):
            raise ValueError("private evidence and snapshot roots must be separate")
        self._root, self._snapshot, self._owner = evidence_root, snapshot, owner

    def _inputs(
        self, command: Json, lease: Json, terminal: Json, deadline: float
    ) -> tuple[Json, Json]:
        policy = validate_policy(lease["policy"])
        command = validate_command(command, policy)
        if command["operation"] != "start":
            raise ValueError("result publication requires the original start identity")
        payload, allocation = command["payload"], policy["allocation"]
        expected = {
            "schema": "hi/hephaestus/build-lease/v1",
            "leaseId": lease["leaseId"],
            "buildId": command["targetId"],
            "attempt": payload["attempt"],
            "commandId": command["commandId"],
            "workerId": allocation["workerId"],
            "allocationId": allocation["id"],
            "generation": allocation["generation"],
            "policyDigest": payload["policyDigest"],
            "snapshotDigest": payload["snapshot"]["manifestDigest"],
            "workspace": lease["workspace"],
            "policy": policy,
        }
        workspace = Path(lease["workspace"])
        if (
            not equal(lease, expected)
            or not workspace.is_absolute()
            or workspace.resolve(strict=True) != workspace
            or workspace.is_relative_to(self._root)
            or self._root.is_relative_to(workspace)
        ):
            raise ValueError("result lease or private authority differs from admission")
        identity = _identity(
            {
                "buildId": command["targetId"],
                "attempt": payload["attempt"],
                "commandId": command["commandId"],
                "leaseId": lease["leaseId"],
                "parent": payload["parent"],
                "policy": policy,
                "policyDigest": payload["policyDigest"],
                "snapshot": payload["snapshot"],
            },
            self._snapshot.policy,
        )
        evidence = fields(
            self._owner.evidence(lease, deadline=deadline), "schema lease result cleanup"
        )
        if evidence["schema"] != "hi/hephaestus/build-step-result/v1" or not equal(
            evidence["lease"], lease
        ):
            raise ValueError("execution evidence changed its retained lease")
        result = _result(evidence["result"], policy["recipe"]["resources"]["outputBytes"])
        if any(not equal(terminal[name], result[name]) for name in ("outcome", "exitCode")):
            raise ValueError("terminal outcome differs from the actual retained result")
        return identity, {"result": result, "cleanup": evidence["cleanup"]}

    def _materials(self, identity: Json, evidence: Json, deadline: float) -> dict[str, bytes]:
        manifest, _ = _read_regular(
            self._snapshot.artifact, "manifest.json", MAX_MANIFEST_BYTES, deadline
        )
        _manifest(manifest, identity["snapshot"], self._snapshot.policy, identity["policy"])
        result = evidence["result"]
        files = {
            "manifest.json": manifest,
            "stdout.txt": result["stdout"].encode(),
            "stderr.txt": result["stderr"].encode(),
        }
        receipt = {
            "schema": "hi/hephaestus/build-result/v1",
            "identity": identity,
            "argv": ["just", "test-unit"],
            "outcome": result["outcome"],
            "exitCode": result["exitCode"],
            "cleanup": evidence["cleanup"],
            "files": {
                role: {
                    "path": name,
                    "bytes": len(files[name]),
                    "digest": hashlib.sha256(files[name]).hexdigest(),
                }
                for role, name in (
                    ("manifest", "manifest.json"),
                    ("stdout", "stdout.txt"),
                    ("stderr", "stderr.txt"),
                )
            },
            # The fixed first profile declares only captured logs and source evidence.
            "artifacts": [],
        }
        raw = encoded(receipt)
        _receipt(raw, identity)
        return {**files, "receipt.json": raw}

    @staticmethod
    def _verify(bundle: Path, files: dict[str, bytes], deadline: float) -> None:
        with directory(bundle, deadline) as owner:
            private_parent(os.fstat(owner))
            if set(os.listdir(owner)) != set(files):
                raise RuntimeError("private result membership changed")
            for name, data in files.items():
                actual, hashed, size = read_private_file(
                    bundle, name, max_bytes=len(data), deadline=deadline, retain=True
                )
                if (
                    actual != data
                    or size != len(data)
                    or hashed != hashlib.sha256(data).hexdigest()
                ):
                    raise RuntimeError("private result bytes changed")

    def _retain(self, reference: str, files: dict[str, bytes], deadline: float) -> None:
        bundle = self._root / reference
        with directory(self._root, deadline) as parent:
            private_parent(os.fstat(parent))
            if not bundle.exists() and not bundle.is_symlink():
                publish_tree(
                    bundle, {name: (data, 0o600) for name, data in files.items()}, deadline
                )
            self._verify(bundle, files, deadline)
            with directory(bundle, deadline) as owner:
                for name in files:
                    budget(deadline)
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=owner
                    )
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                os.fsync(owner)
            os.fsync(parent)
            self._verify(bundle, files, deadline)

    def publish(self, command: Json, lease: Json, terminal: Json, *, deadline: float) -> Json:
        """Retain exact private result references before controller publication."""
        try:
            budget(deadline)
            identity, evidence = self._inputs(command, lease, terminal, deadline)
            files = self._materials(identity, evidence, deadline)
            reference = "result-" + identity["leaseId"]
            self._retain(reference, files, deadline)
            result = evidence["result"]
            return {
                "receipt": {
                    "reference": reference,
                    "digest": hashlib.sha256(files["receipt.json"]).hexdigest(),
                },
                "logs": {
                    "reference": "output-" + identity["leaseId"],
                    "digest": digest({name: result[name] for name in ("stdout", "stderr")}),
                },
                "artifacts": {"reference": reference, "digest": digest([])},
            }
        except Exception as error:
            raise RuntimeError("private build publication requires reconciliation") from error
