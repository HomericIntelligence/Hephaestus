"""Collect real private recipe output and reject changed evidence/source bytes.

The allocation and cleanup records are typed fixtures. Actual Git, snapshot and
Just processes provide file-content observations, not Slurm/Pyxis qualification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_build_collection import collect_build_result
from hephaestus.automation.fleet_build_contract import encoded
from hephaestus.automation.fleet_snapshot import SnapshotPolicy, export_snapshot

FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_build"


def file_record(root: Path, name: str, data: bytes) -> dict[str, Any]:
    """Write actual bytes and record their independently recomputable identity."""
    target = root / name
    target.write_bytes(data)
    target.chmod(0o600)
    return {"path": name, "bytes": len(data), "digest": hashlib.sha256(data).hexdigest()}


def recipe_output(source: Path) -> subprocess.CompletedProcess[bytes]:
    """Run the fixed real Just recipe with no inherited credentials or stdin."""
    just = shutil.which("just")
    assert just is not None, "the supported Just executable is required"
    return subprocess.run(
        [just, "--justfile", str(source / "justfile"), "test-unit"],
        cwd=source,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"PATH": os.defpath},
        timeout=5,
        check=False,
    )


def collection_fixture(root: Path) -> dict[str, Any]:
    """Create a real Git snapshot and real output beneath disjoint private roots."""
    source = root / "source"
    source.mkdir(mode=0o700)
    data = json.loads((FIXTURE / "controller.json").read_bytes())
    for name, value in data["sourceFiles"].items():
        (source / name).write_bytes(value.encode())
        (source / name).chmod(0o644)
    git = shutil.which("git")
    assert git is not None, "the existing Git executable is required"
    home = root / "git-home"
    home.mkdir(mode=0o700)
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for arguments in (
        ("init", "--quiet"),
        ("add", "justfile", "uv.lock"),
        ("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture source"),
    ):
        subprocess.run(
            [git, *arguments],
            cwd=source,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=True,
        )
    policy = SnapshotPolicy(max_members=20, max_bytes=8192)
    artifact = root / "snapshot"
    commitment = export_snapshot(source, artifact, reference="collection-fixture", policy=policy)
    output = recipe_output(source)
    assert output.returncode == 0
    assert output.stdout == b"fixture build\n"
    command = data["admission"]["command"]
    registered_policy = command["payload"]["policy"]
    assert (
        hashlib.sha256(encoded(registered_policy)).hexdigest() == command["payload"]["policyDigest"]
    )
    assert (
        registered_policy["workspace"]["snapshotPolicyDigest"]
        == command["payload"]["snapshot"]["policyDigest"]
        == commitment["policyDigest"]
        == policy.digest
    )
    identity = {
        "buildId": command["targetId"],
        "attempt": command["payload"]["attempt"],
        "commandId": command["commandId"],
        "leaseId": "fedcba9876543210fedcba9876543210",
        "parent": command["payload"]["parent"],
        "policy": command["payload"]["policy"],
        "policyDigest": command["payload"]["policyDigest"],
        "snapshot": commitment,
    }
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    bundle = evidence / "result-fixture"
    bundle.mkdir(mode=0o700)
    receipt = {
        "schema": "hi/hephaestus/build-result/v1",
        "identity": identity,
        "argv": ["just", "test-unit"],
        "outcome": "completed",
        "exitCode": output.returncode,
        "cleanup": {
            "step": {
                "schema": "hi/hephaestus/slurm-step/v1",
                "allocation": {
                    "schema": "hi/hephaestus/slurm-allocation/v1",
                    "allocationId": identity["policy"]["allocation"]["id"],
                    "generation": identity["policy"]["allocation"]["generation"],
                    "qualificationReceiptDigest": identity["policy"]["allocation"][
                        "qualificationReceiptDigest"
                    ],
                    "cluster": "fixture-cluster",
                    "jobId": "77001",
                    "jobStart": "2026-09-13T00:00:00Z",
                    "node": "fixture-node",
                    "uid": os.getuid(),
                    "bootId": "fixture-boot",
                },
                "leaseId": identity["leaseId"],
                "launchNonce": "fixture-launch",
                "stepId": "0",
                "stepStart": "fixture-step-incarnation",
                "cgroup": "fixture-cgroup",
            },
            "schedulerTerminal": True,
            "kernelEmpty": True,
        },
        "files": {
            "manifest": file_record(
                bundle, "manifest.json", (artifact / "manifest.json").read_bytes()
            ),
            "stdout": file_record(bundle, "stdout.txt", output.stdout),
            "stderr": file_record(bundle, "stderr.txt", output.stderr),
        },
        "artifacts": [file_record(bundle, "test-output.txt", output.stdout)],
    }
    file_record(bundle, "receipt.json", encoded(receipt))
    reference = {"id": bundle.name, "digest": hashlib.sha256(encoded(receipt)).hexdigest()}
    return {
        "source": source,
        "evidence": evidence,
        "bundle": bundle,
        "receipt": receipt,
        "reference": reference,
        "identity": identity,
        "policy": policy,
    }


def collect(fixture: dict[str, Any]) -> dict[str, Any] | None:
    """Read through the public collector with finite caller-owned bounds."""
    return collect_build_result(
        fixture["evidence"],
        reference=fixture["reference"],
        expected=fixture["identity"],
        source=fixture["source"],
        snapshot_policy=fixture["policy"],
        deadline=time.monotonic() + 10,
    )


def test_collects_actual_recipe_bytes_and_current_source(tmp_path: Path) -> None:
    """A verified result binds actual logs/artifact bytes and unchanged source."""
    fixture = collection_fixture(tmp_path)
    value = collect(fixture)
    assert isinstance(value, dict), "independent collection returned no evidence"
    assert value["status"] == "verified_current"
    assert value["sourceCurrent"] is True
    assert value["exitCode"] == 0
    assert value["reference"] == fixture["reference"]
    assert value["identity"] == fixture["identity"]
    assert value["artifacts"] == fixture["receipt"]["artifacts"]
    assert "collectionVerified" not in value


@pytest.mark.parametrize("name", ["receipt.json", "stdout.txt", "manifest.json", "test-output.txt"])
def test_changed_referenced_bytes_cannot_be_collected(tmp_path: Path, name: str) -> None:
    """Identical metadata cannot conceal a changed receipt, log, manifest or artifact."""
    fixture = collection_fixture(tmp_path)
    (fixture["bundle"] / name).write_bytes(b"changed\n")
    with pytest.raises(RuntimeError):
        collect(fixture)


@pytest.mark.parametrize("change", ["tracked", "untracked", "mode"])
def test_changed_current_source_makes_valid_result_historical(tmp_path: Path, change: str) -> None:
    """A complete old result cannot become current-checkout success after edits."""
    fixture = collection_fixture(tmp_path)
    source = fixture["source"]
    if change == "tracked":
        (source / "uv.lock").write_bytes(b"version = 2\n")
    elif change == "untracked":
        (source / "new-test.py").write_bytes(b"assert False\n")
    else:
        (source / "justfile").chmod(0o755)
    value = collect(fixture)
    assert isinstance(value, dict), "a valid historical result was not classified"
    assert value["status"] == "historical"
    assert value["sourceCurrent"] is False
    assert value["reference"] == fixture["reference"]


@pytest.mark.parametrize("shape", ["symlink", "hardlink", "public", "missing"])
def test_collector_rejects_borrowed_or_incomplete_files(tmp_path: Path, shape: str) -> None:
    """A readable alias or incomplete output is not an independently owned artifact."""
    fixture = collection_fixture(tmp_path)
    target = fixture["bundle"] / "test-output.txt"
    data = target.read_bytes()
    if shape == "public":
        target.chmod(0o644)
    else:
        target.unlink()
        if shape != "missing":
            sibling = tmp_path / "outside-output"
            sibling.write_bytes(data)
            sibling.chmod(0o600)
            if shape == "symlink":
                target.symlink_to(sibling)
            else:
                os.link(sibling, target)
    with pytest.raises(RuntimeError):
        collect(fixture)


@pytest.mark.parametrize("change", ["attempt", "cleanup"])
def test_different_expected_attempt_or_unproved_cleanup_is_rejected(
    tmp_path: Path,
    change: str,
) -> None:
    """The caller's attempt and both disposal observations are required."""
    fixture = collection_fixture(tmp_path)
    if change == "attempt":
        fixture["identity"] = copy.deepcopy(fixture["identity"])
        fixture["identity"]["commandId"] = "other-command"
    else:
        fixture["receipt"]["cleanup"]["kernelEmpty"] = False
        data = encoded(fixture["receipt"])
        file_record(fixture["bundle"], "receipt.json", data)
        fixture["reference"]["digest"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(RuntimeError):
        collect(fixture)
