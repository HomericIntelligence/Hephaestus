"""Check retained log contracts with explicitly synthetic producer records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_artifacts import ArtifactCatalog, ArtifactRequestError

pytestmark = pytest.mark.precommit


def canonical(value: object) -> bytes:
    """Encode the documented producer format independently of the service."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data: bytes) -> str:
    """Hash the exact fixture bytes."""
    return hashlib.sha256(data).hexdigest()


def private_file(path: Path, data: bytes) -> None:
    """Write a private test input without an actual build or credential."""
    path.write_bytes(data)
    path.chmod(0o600)


def retained_bundle(
    root: Path, *, stdout: bytes = "Aλ🦉Z\n".encode(), stderr: bytes = b"fixture warning\n"
) -> dict[str, Any]:
    """Make a synthetic terminal bundle; these records are not execution evidence."""
    root.mkdir(mode=0o700)
    manifest = canonical({"schema": "fixture/source/v1", "files": [], "baseCommit": "b" * 40})
    policy: dict[str, Any] = {
        "allocation": {"id": "allocation-fixture", "workerId": "worker-fixture", "generation": 3},
        "recipe": {"resources": {"outputBytes": 64 * 1024 * 1024}},
    }
    identity: dict[str, Any] = {
        "buildId": "build-fixture",
        "attempt": 1,
        "commandId": "build-fixture-start",
        "leaseId": "a" * 32,
        "parent": {"taskId": "task-fixture"},
        "policy": policy,
        "policyDigest": sha(canonical(policy)),
        "snapshot": {"manifestDigest": sha(manifest)},
    }
    materials = {"manifest": manifest, "stdout": stdout, "stderr": stderr}
    names = {"manifest": "manifest.json", "stdout": "stdout.txt", "stderr": "stderr.txt"}
    receipt = {
        "schema": "hi/hephaestus/build-result/v1",
        "identity": identity,
        "argv": ["just", "test-unit"],
        "outcome": "completed",
        "exitCode": 0,
        "cleanup": {"schedulerTerminal": True, "kernelEmpty": True, "step": {"fixture": True}},
        "files": {
            role: {"path": names[role], "bytes": len(data), "digest": sha(data)}
            for role, data in materials.items()
        },
        "artifacts": [],
    }
    for role, data in materials.items():
        private_file(root / names[role], data)
    private_file(root / "receipt.json", canonical(receipt))
    logs = {
        "reference": "output-" + identity["leaseId"],
        "digest": sha(canonical({"stdout": stdout.decode(), "stderr": stderr.decode()})),
    }
    return {
        "directory": str(root),
        "receiptDigest": sha(canonical(receipt)),
        "identityDigest": sha(canonical(identity)),
        "buildId": identity["buildId"],
        "attempt": identity["attempt"],
        "snapshotDigest": sha(manifest),
        "workerId": policy["allocation"]["workerId"],
        "allocationId": policy["allocation"]["id"],
        "generation": policy["allocation"]["generation"],
        "logs": logs,
    }


def page(catalog: ArtifactCatalog, registration: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Read a page through the public retained-log contract."""
    request = {
        "build_id": registration["buildId"],
        "attempt": registration["attempt"],
        "snapshot_digest": registration["snapshotDigest"],
        "stream": "stdout",
        "after": 0,
        "limit": 65536,
    }
    request.update(changes)
    return catalog.page(**request)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("output", [b"", "Aλ🦉Z\n".encode(), b'\x00\t\r\n"\\' * 4000])
def test_pages_reconstruct_exact_retained_bytes(tmp_path: Path, stream: str, output: bytes) -> None:
    """Preserve bytes, terminal identity, and both digest meanings across pages."""
    registration = retained_bundle(tmp_path / "bundle", **{stream: output})
    catalog = ArtifactCatalog([registration])
    chunks: list[bytes] = []
    after = 0
    while True:
        result = page(catalog, registration, stream=stream, after=after, limit=7)
        chunk = result["data"].encode()
        assert result["chunkDigest"] == sha(chunk)
        assert result["manifest"] == registration["logs"]
        assert result["truncated"] is True
        assert result["next"] == after + len(chunk)
        assert result["complete"] is (result["next"] == len(output))
        assert chunk or result["complete"]
        chunks.append(chunk)
        after = result["next"]
        if result["complete"]:
            break
    assert b"".join(chunks) == output
    eof = page(catalog, registration, stream=stream, after=after)
    assert eof["data"] == ""
    assert eof["chunkDigest"] == sha(b"")
    assert eof["next"] == after
    assert eof["complete"] is True


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"after": 10}, 409),
        ({"after": 2}, 400),
        ({"after": 3, "limit": 3}, 422),
        ({"after": -1}, 400),
        ({"after": True}, 400),
        ({"after": 1.0}, 400),
        ({"after": 2**63}, 400),
        ({"limit": 0}, 400),
        ({"limit": 65537}, 400),
        ({"stream": "combined"}, 400),
        ({"attempt": 0}, 400),
        ({"attempt": 2}, 404),
        ({"snapshot_digest": "f" * 64}, 404),
        ({"build_id": "unknown"}, 404),
        ({"build_id": "../receipt.json"}, 400),
    ],
)
def test_page_rejects_invalid_or_unregistered_request(
    tmp_path: Path,
    changes: dict[str, Any],
    status: int,
) -> None:
    """Distinguish an ahead cursor, scalar boundary, and unknown terminal identity."""
    registration = retained_bundle(tmp_path / "bundle")
    with pytest.raises(ArtifactRequestError) as error:
        page(ArtifactCatalog([registration]), registration, **changes)
    assert error.value.status == status


def test_maximum_escaped_page_stays_inside_client_response_bound(tmp_path: Path) -> None:
    """Include JSON expansion when checking the client's encoded byte ceiling."""
    registration = retained_bundle(tmp_path / "bundle", stdout=b"\x00" * 65537)
    result = page(ArtifactCatalog([registration]), registration)
    assert len(result["data"].encode()) == 65536
    assert len(canonical(result)) < 400000
    assert result["complete"] is False


@pytest.mark.parametrize(
    "field",
    [
        "receiptDigest",
        "identityDigest",
        "snapshotDigest",
        "buildId",
        "attempt",
        "workerId",
        "allocationId",
        "generation",
    ],
)
def test_registration_requires_each_trusted_identity_commitment(tmp_path: Path, field: str) -> None:
    """Reject a different build, attempt, worker, generation, or digest."""
    registration = retained_bundle(tmp_path / "bundle")
    value = registration[field]
    registration[field] = (
        value + 1
        if isinstance(value, int)
        else ("0" * 64 if field.endswith("Digest") else "different-fixture")
    )
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


@pytest.mark.parametrize("member", ["receipt.json", "manifest.json", "stdout.txt", "stderr.txt"])
def test_catalog_rejects_changed_member_bytes(tmp_path: Path, member: str) -> None:
    """Require exact retained bytes, even when a change preserves file length."""
    registration = retained_bundle(tmp_path / "bundle")
    member_path = tmp_path / "bundle" / member
    original = member_path.read_bytes()
    private_file(member_path, b"X" + original[1:])
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


def rewrite_receipt(registration: dict[str, Any], receipt: dict[str, Any]) -> None:
    """Rebind only the fixture receipt digest to exercise its other commitments."""
    encoded = canonical(receipt)
    private_file(Path(registration["directory"]) / "receipt.json", encoded)
    registration["receiptDigest"] = sha(encoded)


@pytest.mark.parametrize(
    "mutation",
    [
        "full-identity",
        "member-size",
        "member-path",
        "member-digest",
        "outcome",
        "exit-type",
        "artifacts",
        "extra-field",
        "log-digest",
        "log-reference",
        "output-bound",
    ],
)
def test_catalog_rejects_inconsistent_receipt(tmp_path: Path, mutation: str) -> None:
    """Check independent record commitments after accepting the receipt digest."""
    registration = retained_bundle(tmp_path / "bundle")
    receipt = json.loads((tmp_path / "bundle" / "receipt.json").read_bytes())
    if mutation == "full-identity":
        receipt["identity"]["parent"]["taskId"] = "different-fixture"
    elif mutation == "member-size":
        receipt["files"]["stdout"]["bytes"] += 1
    elif mutation == "member-path":
        receipt["files"]["stdout"]["path"] = "stderr.txt"
    elif mutation == "member-digest":
        receipt["files"]["stdout"]["digest"] = "0" * 64
    elif mutation == "outcome":
        receipt["outcome"] = "running"
    elif mutation == "exit-type":
        receipt["exitCode"] = False
    elif mutation == "artifacts":
        receipt["artifacts"] = ["unsupported"]
    elif mutation == "extra-field":
        receipt["extra"] = True
    elif mutation == "log-digest":
        registration["logs"]["digest"] = "0" * 64
    elif mutation == "log-reference":
        registration["logs"]["reference"] = "output-other"
    elif mutation == "output-bound":
        receipt["identity"]["policy"]["recipe"]["resources"]["outputBytes"] = 1
        registration["identityDigest"] = sha(canonical(receipt["identity"]))
    rewrite_receipt(registration, receipt)
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


@pytest.mark.parametrize(
    "data",
    [
        b"{",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":"\xff"}',
        b'{"x":"\\ud800"}',
        b'{"x":' + b"[" * 18 + b"0" + b"]" * 18 + b"}",
    ],
)
def test_catalog_rejects_invalid_receipt_json(tmp_path: Path, data: bytes) -> None:
    """Reject invalid JSON before retained output becomes available."""
    registration = retained_bundle(tmp_path / "bundle")
    private_file(tmp_path / "bundle" / "receipt.json", data)
    registration["receiptDigest"] = sha(data)
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


@pytest.mark.parametrize(
    "violation",
    [
        "file-mode",
        "directory-mode",
        "symlink",
        "parent-symlink",
        "hardlink",
        "fifo",
        "size",
    ],
)
def test_catalog_rejects_nonprivate_or_unbounded_files(tmp_path: Path, violation: str) -> None:
    """Fail before reading an unsafe retained member or a blocking special file."""
    registration = retained_bundle(tmp_path / "bundle")
    member = tmp_path / "bundle" / "stdout.txt"
    if violation == "file-mode":
        member.chmod(0o644)
    elif violation == "directory-mode":
        member.parent.chmod(0o755)
    elif violation == "symlink":
        member.rename(member.with_name("original"))
        member.symlink_to("original")
    elif violation == "parent-symlink":
        link = tmp_path / "linked-bundle"
        link.symlink_to(member.parent, target_is_directory=True)
        registration["directory"] = str(link)
    elif violation == "hardlink":
        os.link(member, member.with_name("second-name"))
    elif violation == "fifo":
        member.unlink()
        os.mkfifo(member, 0o600)
    elif violation == "size":
        with member.open("wb") as output:
            output.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises((ValueError, OSError)):
        ArtifactCatalog([registration])


def test_catalog_is_immutable_and_restart_rechecks_commitments(tmp_path: Path) -> None:
    """Keep a loaded view and reject changed disk bytes on the next load."""
    registration = retained_bundle(tmp_path / "bundle")
    catalog = ArtifactCatalog([registration])
    expected = page(catalog, registration)
    assert page(ArtifactCatalog([registration]), registration) == expected
    private_file(tmp_path / "bundle" / "stdout.txt", b"changed fixture")
    assert page(catalog, registration) == expected
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


def test_catalog_rejects_invalid_utf8_with_matching_member_descriptor(tmp_path: Path) -> None:
    """Reject a byte stream that cannot supply the JSON text page contract."""
    registration = retained_bundle(tmp_path / "bundle")
    receipt = json.loads((tmp_path / "bundle" / "receipt.json").read_bytes())
    invalid = b"\xff"
    private_file(tmp_path / "bundle" / "stdout.txt", invalid)
    receipt["files"]["stdout"].update(bytes=len(invalid), digest=sha(invalid))
    rewrite_receipt(registration, receipt)
    with pytest.raises(ValueError):
        ArtifactCatalog([registration])


def test_duplicate_and_excess_registrations_fail_before_serving(tmp_path: Path) -> None:
    """A registration cannot replace an existing attempt or exceed the count bound."""
    registration = retained_bundle(tmp_path / "bundle")
    with pytest.raises(ValueError):
        ArtifactCatalog([registration, registration])
    with pytest.raises(ValueError):
        ArtifactCatalog([registration] * 257)


def test_two_valid_bundles_cannot_exceed_aggregate_retained_capacity(tmp_path: Path) -> None:
    """Enforce the real 64 MiB aggregate bound across distinct build attempts."""
    first = retained_bundle(tmp_path / "first", stdout=b"x" * (33 * 1024 * 1024), stderr=b"")
    second = retained_bundle(tmp_path / "second", stdout=b"y" * (32 * 1024 * 1024), stderr=b"")
    receipt = json.loads((tmp_path / "second" / "receipt.json").read_bytes())
    receipt["identity"]["attempt"] = 2
    second["attempt"] = 2
    second["identityDigest"] = sha(canonical(receipt["identity"]))
    rewrite_receipt(second, receipt)
    with pytest.raises(ValueError):
        ArtifactCatalog([first, second])
