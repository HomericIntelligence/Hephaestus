"""Independently collect private result bytes and compare current eligible source."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from hephaestus.automation.fleet_build_contract import (
    Json,
    digest,
    encoded,
    equal,
    fields,
    identifier,
    integer,
    sha,
    validate_command,
    validate_policy,
)
from hephaestus.automation.fleet_build_executor import (
    budget,
    read_private_file,
    validate_allocation,
    validate_step,
)
from hephaestus.automation.fleet_snapshot import SnapshotPolicy, export_snapshot
from hephaestus.automation.fleet_snapshot_files import directory, private_parent
from hephaestus.automation.fleet_snapshot_policy import (
    MAX_MANIFEST_BYTES,
    canonical,
    valid_path,
    validate_manifest,
)

_MAX_ARTIFACTS = 1000
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class FleetBuildEvidence:
    """Supply an actual private receipt and its independently trusted lease."""

    evidence_root: Path
    reference: Json
    expected: Json

    def __post_init__(self) -> None:
        """Copy the trusted identity and private reference before collection."""
        object.__setattr__(self, "reference", dict(json.loads(encoded(self.reference))))
        object.__setattr__(self, "expected", dict(json.loads(encoded(self.expected))))


def _identity(value: Json, policy: SnapshotPolicy) -> Json:
    """Reuse the controller validator for parent, policy and snapshot relationships."""
    fields(value, "buildId attempt commandId leaseId parent policy policyDigest snapshot")
    sha(value["leaseId"], 32)
    admitted = validate_policy(value["policy"])
    attempt = integer(value["attempt"])
    build_id = identifier(value["buildId"])
    validate_command(
        {
            "schema": "hi/fleet/v1",
            "targetKind": "build-jobs",
            "targetId": build_id,
            "workerId": admitted["allocation"]["workerId"],
            "generation": admitted["allocation"]["generation"],
            "operation": "start",
            "commandId": value["commandId"],
            "idempotencyKey": value["commandId"],
            "payload": {
                "schema": "hi/fleet/build-command/v1",
                "attempt": attempt,
                "parent": value["parent"],
                "policy": admitted,
                "policyDigest": value["policyDigest"],
                "parametersDigest": digest({}),
                "snapshot": value["snapshot"],
                "snapshotWorkspace": f"{build_id}-attempt-{attempt}",
                "requiresRunGrant": True,
            },
        },
        admitted,
    )
    if value["snapshot"]["policyDigest"] != policy.digest:
        raise ValueError("collector snapshot selection policy changed")
    return cast(Json, json.loads(encoded(value)))


def _descriptor(value: Json, maximum: int) -> Json:
    fields(value, "path bytes digest")
    if not valid_path(value["path"]) or integer(value["bytes"], minimum=0) > maximum:
        raise ValueError("invalid bounded evidence member")
    sha(value["digest"])
    return cast(Json, json.loads(encoded(value)))


def _outcome(value: Json) -> None:
    outcome, code = value["outcome"], value["exitCode"]
    if outcome in ("timed_out", "cancelled"):
        if code is not None:
            raise ValueError("interrupted result cannot invent an exit status")
    elif (
        outcome not in ("completed", "failed")
        or type(code) is not int
        or not -255 <= code <= 255
        or (outcome == "completed") != (code == 0)
    ):
        raise ValueError("invalid collected build outcome")


def _cleanup(value: Json, identity: Json) -> None:
    """Bind the private owner's observations without upgrading them to a live probe."""
    fields(value, "step schedulerTerminal kernelEmpty")
    if value["schedulerTerminal"] is not True or value["kernelEmpty"] is not True:
        raise ValueError("collected cleanup is not confirmed")
    step = value["step"]
    fields(step, "schema allocation leaseId launchNonce stepId stepStart cgroup")
    allocation = validate_allocation(step["allocation"])
    admitted = identity["policy"]["allocation"]
    if (
        allocation["allocationId"] != admitted["id"]
        or not equal(allocation["generation"], admitted["generation"])
        or allocation["qualificationReceiptDigest"] != admitted["qualificationReceiptDigest"]
    ):
        raise ValueError("collected allocation differs from admission")
    validate_step(step, allocation, identity["leaseId"], step["launchNonce"])


def _receipt(data: bytes, identity: Json) -> tuple[Json, list[Json]]:
    value = fields(
        json.loads(data), "schema identity argv outcome exitCode cleanup files artifacts"
    )
    if (
        encoded(value) != data
        or value["schema"] != "hi/hephaestus/build-result/v1"
        or not equal(value["identity"], identity)
        or value["argv"] != ["just", "test-unit"]
    ):
        raise ValueError("result receipt is not the exact canonical attempt")
    _outcome(value)
    _cleanup(value["cleanup"], identity)
    files = fields(value["files"], "manifest stdout stderr")
    limits = identity["policy"]["recipe"]["resources"]
    records = [_descriptor(files["manifest"], MAX_MANIFEST_BYTES)]
    for stream in ("stdout", "stderr"):
        records.append(_descriptor(files[stream], limits["outputBytes"]))
    if sum(record["bytes"] for record in records[1:]) > limits["outputBytes"]:
        raise ValueError("result logs exceed the admitted aggregate bound")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > _MAX_ARTIFACTS:
        raise ValueError("result artifact membership is not bounded")
    checked = [_descriptor(item, limits["artifactBytes"]) for item in artifacts]
    if sum(record["bytes"] for record in checked) > limits["artifactBytes"]:
        raise ValueError("result artifacts exceed the admitted aggregate bound")
    records.extend(checked)
    names = ["receipt.json", *(record["path"].lower() for record in records)]
    if len(set(names)) != len(names):
        raise ValueError("result evidence contains aliased member roles")
    return value, records


def _manifest(data: bytes, commitment: Json, policy: SnapshotPolicy, admitted: Json) -> None:
    value = validate_manifest(json.loads(data), policy)
    if (
        canonical(value) != data
        or hashlib.sha256(data).hexdigest() != commitment["manifestDigest"]
        or value["baseCommit"] != commitment["baseCommit"]
        or len(value["files"]) != commitment["members"]
        or sum(item["size"] for item in value["files"]) != commitment["bytes"]
    ):
        raise ValueError("result source manifest differs from the admitted snapshot")
    files = {item["path"]: item for item in value["files"]}
    for name, field in (("justfile", "recipeDigest"), ("uv.lock", "lockDigest")):
        if name not in files or files[name]["sha256"] != admitted["recipe"][field]:
            raise ValueError("collected recipe or lock differs from the admitted policy")


def _read_record(root: Path, record: Json, deadline: float, *, retain: bool = False) -> bytes:
    data, hashed, size = read_private_file(
        root,
        record["path"],
        max_bytes=record["bytes"],
        deadline=deadline,
        retain=retain,
    )
    if hashed != record["digest"] or size != record["bytes"]:
        raise RuntimeError("referenced evidence bytes differ from their commitment")
    return data


def _membership(root: Path, records: list[Json], deadline: float) -> None:
    """Require the complete private bundle to contain only declared files and parents."""
    files = {"receipt.json", *(record["path"] for record in records)}
    parents = {
        "/".join(name.split("/")[:index])
        for name in files
        for index in range(1, len(name.split("/")))
    }
    if len(files | parents) > 40000 or files & parents:
        raise ValueError("result membership exceeds its bound or aliases a directory")
    found: set[str] = set()

    def visit(descriptor: int, prefix: str) -> None:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                budget(deadline)
                name = prefix + entry.name
                if name not in files and name not in parents:
                    raise ValueError("result bundle has undeclared evidence")
                metadata = entry.stat(follow_symlinks=False)
                found.add(name)
                if name in parents:
                    private_parent(metadata)
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        visit(child, name + "/")
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("result member is not a regular file")

    with directory(root, deadline) as descriptor:
        private_parent(os.fstat(descriptor))
        visit(descriptor, "")
    if found != files | parents:
        raise ValueError("result bundle is incomplete")


def _current_source(
    source: Path, root: Path, identity: Json, policy: SnapshotPolicy, deadline: float
) -> bool:
    """Use the real exporter; the caller retains exclusive source construction ownership."""
    with tempfile.TemporaryDirectory(prefix=".collection-", dir=root) as temporary:
        current = export_snapshot(
            source,
            Path(temporary) / "snapshot",
            reference=identity["snapshot"]["reference"],
            policy=policy,
            timeout=min(30, budget(deadline)),
        )
        budget(deadline)
        return equal(current, identity["snapshot"])


def collect_build_result(
    evidence_root: Path,
    *,
    reference: Json,
    expected: Json,
    source: Path,
    snapshot_policy: SnapshotPolicy,
    deadline: float,
) -> Json:
    """Hash all referenced bytes, then distinguish a current result from history.

    The caller owns an exclusive source/evidence lease. This is independent byte
    collection, not a new scheduler probe, execution grant or controller flag.
    """
    try:
        budget(deadline)
        fields(reference, "id digest")
        reference = {"id": identifier(reference["id"]), "digest": sha(reference["digest"])}
        identity = _identity(expected, snapshot_policy)
        if (
            not source.is_absolute()
            or source.resolve(strict=True) != source
            or not evidence_root.is_absolute()
            or evidence_root.resolve(strict=True) != evidence_root
            or evidence_root.is_relative_to(source)
            or source.is_relative_to(evidence_root)
        ):
            raise ValueError("source and evidence authority must be separate canonical roots")
        bundle = evidence_root / reference["id"]
        with (
            directory(evidence_root, deadline) as root_fd,
            directory(bundle, deadline) as bundle_fd,
        ):
            private_parent(os.fstat(root_fd))
            private_parent(os.fstat(bundle_fd))
            raw, hashed, size = read_private_file(
                bundle,
                "receipt.json",
                max_bytes=_MAX_RECEIPT_BYTES,
                deadline=deadline,
                retain=True,
            )
            if hashed != reference["digest"]:
                raise ValueError("private result receipt digest changed")
            receipt, records = _receipt(raw, identity)
            _membership(bundle, records, deadline)
            manifest = _read_record(bundle, records[0], deadline, retain=True)
            _manifest(manifest, identity["snapshot"], snapshot_policy, identity["policy"])
            for record in records[1:]:
                _read_record(bundle, record, deadline)
            current = _current_source(source, evidence_root, identity, snapshot_policy, deadline)
            # Re-read the complete referenced set after the separate source capture.
            for record in [{"path": "receipt.json", "bytes": size, "digest": hashed}, *records]:
                _read_record(bundle, record, deadline)
            _membership(bundle, records, deadline)
            private_parent(os.fstat(root_fd))
            private_parent(os.fstat(bundle_fd))
            result = {
                "schema": "hi/hephaestus/build-collection/v1",
                "reference": reference,
                "identity": identity,
                "outcome": receipt["outcome"],
                "exitCode": receipt["exitCode"],
                "artifacts": receipt["artifacts"],
                "sourceCurrent": current,
                "status": "verified_current" if current else "historical",
            }
        return result
    except Exception as error:
        raise RuntimeError("build collection is incomplete or requires reconciliation") from error
