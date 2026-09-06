"""Tests for the Linux host-verification data contract."""

from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from hephaestus.automation.linux_host_verification_contract import (
    LinuxHostVerificationReceipt,
    LinuxHostVerificationRequest,
    canonical_digest,
    read_linux_host_verification_receipt,
    read_linux_host_verification_request,
    write_linux_host_verification_receipt,
    write_linux_host_verification_request,
)


def _request_payload() -> dict[str, object]:
    """Return one complete request fixture."""
    return {
        "schema_version": 1,
        "run_id": "run-20260905-01234567",
        "command_id": "review_python_ruff_check",
        "expected_head_sha": "a" * 40,
        "timeout_seconds": 900,
        "submit_hostname": "submit-01",
        "source_archive_path": "/shared/runs/run-20260905-01234567/source.tar",
        "source_extract_path": "/shared/runs/run-20260905-01234567/source",
        "source_archive_sha256": "b" * 64,
        "git_metadata_archive_path": "/shared/runs/run-20260905-01234567/git.tar",
        "git_metadata_extract_path": "/shared/runs/run-20260905-01234567/metadata.git",
        "git_metadata_archive_sha256": "c" * 64,
        "image_path": "/images/hephaestus.sqsh",
        "image_sha256": "d" * 64,
        "image_manifest_sha256": "e" * 64,
        "contract_root": "/opt/hephaestus-contract",
        "contract_sha256": "f" * 64,
        "allocation_driver_sha256": "1" * 64,
        "srun_path": "/usr/bin/srun",
        "scratch_path": "/shared/runs/run-20260905-01234567/scratch",
        "stdout_path": "/shared/runs/run-20260905-01234567/stdout.log",
        "stderr_path": "/shared/runs/run-20260905-01234567/stderr.log",
        "receipt_path": "/shared/runs/run-20260905-01234567/receipt.json",
    }


def _receipt_payload() -> dict[str, object]:
    """Return one complete success receipt fixture."""
    request = _request_payload()
    return {
        "schema_version": 1,
        "run_id": request["run_id"],
        "command_id": request["command_id"],
        "expected_head_sha": request["expected_head_sha"],
        "source_archive_sha256": request["source_archive_sha256"],
        "git_metadata_archive_sha256": request["git_metadata_archive_sha256"],
        "image_sha256": request["image_sha256"],
        "image_manifest_sha256": request["image_manifest_sha256"],
        "contract_sha256": request["contract_sha256"],
        "allocation_driver_sha256": request["allocation_driver_sha256"],
        "backend": "linux-pyxis",
        "allocation_job_id": "12345;cluster-a",
        "submit_hostname": request["submit_hostname"],
        "allocation_hostname": "compute-01",
        "return_code": 0,
        "timed_out": False,
        "outcome": "passed",
        "failure_classification": "none",
        "boundary_checks": {
            "workspace_read_only": True,
            "metadata_read_only": True,
            "scratch_writable": True,
            "root_read_only": True,
            "network_isolated": True,
            "credentials_scrubbed": True,
            "devices_restricted": True,
            "workspace_venv_absent": True,
            "installed_verifier_isolated": True,
        },
        "stdout_tail": "all checks passed\n",
        "stderr_tail": "",
    }


def test_canonical_digest_is_order_independent_and_domain_separated() -> None:
    """Digest ordering cannot alter one logical file-set identity."""
    first = canonical_digest(
        "contract-v1",
        [("hephaestus/a.py", b"alpha"), ("pyproject.toml", b"project")],
    )
    second = canonical_digest(
        "contract-v1",
        [("pyproject.toml", b"project"), ("hephaestus/a.py", b"alpha")],
    )

    assert first == second
    assert first != canonical_digest(
        "build-context-v1", [("hephaestus/a.py", b"alpha"), ("pyproject.toml", b"project")]
    )
    assert first != canonical_digest(
        "contract-v1", [("hephaestus/a.py", b"changed"), ("pyproject.toml", b"project")]
    )


@pytest.mark.parametrize("path", ["", "/absolute.py", "../escape.py", "a/../b.py", "a//b.py"])
def test_canonical_digest_rejects_noncanonical_logical_paths(path: str) -> None:
    """A digest does not silently normalize an ambiguous input path."""
    with pytest.raises(ValueError, match="logical path"):
        canonical_digest("contract-v1", [(path, b"data")])


def test_canonical_digest_rejects_duplicate_logical_paths() -> None:
    """Two contents must never claim one logical name."""
    with pytest.raises(ValueError, match="duplicate"):
        canonical_digest("contract-v1", [("a.py", b"one"), ("a.py", b"two")])


def test_request_round_trip_requires_the_closed_schema() -> None:
    """A complete request serializes and parses without losing its binding facts."""
    request = LinuxHostVerificationRequest.from_mapping(_request_payload())

    assert request.to_mapping() == _request_payload()
    assert request.expected_head_sha == "a" * 40


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "value"),
        ("source_archive_path", "relative/source.tar"),
        ("source_archive_sha256", "A" * 64),
        ("expected_head_sha", "a" * 39),
        ("timeout_seconds", 0),
    ],
)
def test_request_rejects_unknown_or_invalid_boundary_data(field: str, value: object) -> None:
    """Malformed leases fail before a scheduler or filesystem boundary."""
    payload = _request_payload()
    payload[field] = value

    with pytest.raises(ValueError):
        LinuxHostVerificationRequest.from_mapping(payload)


def test_receipt_round_trip_requires_all_binding_and_boundary_facts() -> None:
    """A success receipt includes all exact lease digests and probe results."""
    receipt = LinuxHostVerificationReceipt.from_mapping(_receipt_payload())
    request = LinuxHostVerificationRequest.from_mapping(_request_payload())

    assert receipt.to_mapping() == _receipt_payload()
    assert receipt.boundary_checks["network_isolated"] is True
    receipt.validate_against_request(request)


def test_receipt_rejects_a_request_binding_mismatch() -> None:
    """A valid receipt is still unusable when it names another lease."""
    receipt = LinuxHostVerificationReceipt.from_mapping(_receipt_payload())
    request_payload = _request_payload()
    request_payload["image_sha256"] = "9" * 64
    request = LinuxHostVerificationRequest.from_mapping(request_payload)

    with pytest.raises(ValueError, match="image_sha256"):
        receipt.validate_against_request(request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "value"),
        ("backend", "linux-shell"),
        ("failure_classification", "unknown"),
        ("stdout_tail", "x" * 16385),
    ],
)
def test_receipt_rejects_unknown_or_unbounded_data(field: str, value: object) -> None:
    """Receipts cannot add authority or unbounded diagnostics."""
    payload = _receipt_payload()
    payload[field] = value

    with pytest.raises(ValueError):
        LinuxHostVerificationReceipt.from_mapping(payload)


def test_success_receipt_rejects_failed_boundary_or_failure_state() -> None:
    """A passed result cannot hide a boundary failure or failure classification."""
    failed_boundary = deepcopy(_receipt_payload())
    checks = cast(dict[str, bool], failed_boundary["boundary_checks"])
    failed_boundary["boundary_checks"] = {
        **checks,
        "network_isolated": False,
    }
    with pytest.raises(ValueError, match="passed receipt"):
        LinuxHostVerificationReceipt.from_mapping(failed_boundary)

    failed_state = _receipt_payload()
    failed_state["failure_classification"] = "execution"
    with pytest.raises(ValueError, match="passed receipt"):
        LinuxHostVerificationReceipt.from_mapping(failed_state)


def test_request_and_receipt_files_round_trip_atomically(tmp_path: Path) -> None:
    """Lease files use private, atomic JSON serialization and closed readers."""
    request = LinuxHostVerificationRequest.from_mapping(_request_payload())
    receipt = LinuxHostVerificationReceipt.from_mapping(_receipt_payload())
    request_path = tmp_path / "request.json"
    receipt_path = tmp_path / "receipt.json"

    write_linux_host_verification_request(request_path, request)
    write_linux_host_verification_receipt(receipt_path, receipt)

    assert request_path.stat().st_mode & 0o077 == 0
    assert receipt_path.stat().st_mode & 0o077 == 0
    assert read_linux_host_verification_request(request_path) == request
    assert read_linux_host_verification_receipt(receipt_path) == receipt


@pytest.mark.parametrize(
    "reader", [read_linux_host_verification_request, read_linux_host_verification_receipt]
)
def test_lease_file_reader_rejects_symlink(
    tmp_path: Path, reader: Callable[[Path], object]
) -> None:
    """A lease reader never follows a symbolic link at the trusted path."""
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.json"
    os.symlink(target, linked)

    with pytest.raises(ValueError):
        reader(linked)
