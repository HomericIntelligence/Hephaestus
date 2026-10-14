"""Test retained evidence when probe cleanup cannot complete."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation import host_capabilities as capabilities
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.host_capabilities import QUOTA_UNAVAILABLE_TOKEN
from tests.unit.automation.test_host_capabilities import _request, _runner


@pytest.mark.parametrize("failure", ["request_open", "mount_mkdir", "mount_open", "mount_fstat"])
def test_preflight_partial_setup_retains_owned_root_and_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Retain an owned partial probe when setup fails before host commands."""
    failure_messages = {
        "request_open": "request directory open failed",
        "mount_mkdir": "mount directory creation failed",
        "mount_open": "mount directory open failed",
        "mount_fstat": "mount directory identity failed",
    }
    target = _request(tmp_path)
    probe = tmp_path / "build" / ".host-verification" / target.request.request_id
    real_mkdir = os.mkdir
    real_open = os.open
    real_fstat = os.fstat
    mount_fd: int | None = None
    mount_fstat_pending = False

    def mkdir_entry(path: str | bytes | os.PathLike[str], *args: Any, **kwargs: Any) -> None:
        if failure == "mount_mkdir" and str(path) == "mount":
            raise OSError("mount directory creation failed")
        real_mkdir(path, *args, **kwargs)

    def open_entry(path: str | bytes | os.PathLike[str], *args: Any, **kwargs: Any) -> int:
        nonlocal mount_fd, mount_fstat_pending
        name = str(path)
        if failure == "request_open" and name == target.request.request_id:
            raise OSError("request directory open failed")
        descriptor = real_open(path, *args, **kwargs)
        if name == "mount":
            if failure == "mount_open":
                os.close(descriptor)
                raise OSError("mount directory open failed")
            mount_fd = descriptor
            mount_fstat_pending = failure == "mount_fstat"
        return descriptor

    def fstat_entry(descriptor: int) -> os.stat_result:
        nonlocal mount_fstat_pending
        if mount_fstat_pending and descriptor == mount_fd:
            mount_fstat_pending = False
            raise OSError("mount directory identity failed")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "mkdir", mkdir_entry)
    monkeypatch.setattr(os, "open", open_entry)
    monkeypatch.setattr(os, "fstat", fstat_entry)
    run = _runner()

    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)

    assert not receipt.available
    assert receipt.token == QUOTA_UNAVAILABLE_TOKEN
    assert receipt.failed_step == "backend"
    assert receipt.cleanup_state == "retained"
    assert receipt.retained_root == str(probe)
    assert probe.is_dir()
    assert receipt.exception_type == "OSError"
    assert failure_messages[failure] in receipt.operating_system_error
    run.assert_not_called()
    stored = json.loads(
        (
            tmp_path / DEFAULT_STATE_DIR / "host-capability-receipts" / f"{receipt.receipt_id}.json"
        ).read_text()
    )
    assert stored["receipt"]["cleanup_state"] == "retained"
    assert stored["receipt"]["retained_root"] == str(probe)
    assert stored["receipt"]["operating_system_error"] == receipt.operating_system_error


def test_preflight_partial_setup_does_not_claim_preexisting_foreign_path(
    tmp_path: Path,
) -> None:
    """Leave a preexisting unsafe request path untouched and unclaimed."""
    target = _request(tmp_path)
    build = tmp_path / "build"
    verification = build / ".host-verification"
    foreign = verification / target.request.request_id
    build.mkdir(mode=0o755)
    build.chmod(0o755)
    verification.mkdir(mode=0o700)
    verification.chmod(0o700)
    foreign.mkdir(mode=0o755)
    foreign.chmod(0o755)
    run = _runner()

    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)

    assert not receipt.available
    assert receipt.failed_step == "backend"
    assert receipt.cleanup_state == "not_started"
    assert receipt.retained_root == ""
    assert receipt.exception_type == "FileExistsError"
    assert foreign.is_dir()
    assert foreign.stat().st_mode & 0o777 == 0o755
    run.assert_not_called()


@pytest.mark.parametrize("create_fails", [False, True])
def test_preflight_cleanup_rejection_retains_root_and_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_fails: bool
) -> None:
    """Keep the original result and inspection path after cleanup rejection."""
    cleanup = Mock(side_effect=ValueError("The quota directory identity changed before cleanup."))
    monkeypatch.setattr(capabilities, "_remove_probe_directory", cleanup)
    run = _runner(int(create_fails))
    target = _request(tmp_path)
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    cleanup.assert_called_once()
    assert not receipt.available
    root = tmp_path / "build/.host-verification" / target.request.request_id
    assert receipt.retained_root == str(root)
    assert root.is_dir()
    assert receipt.cleanup_state == "retained"
    assert "identity changed before cleanup" in receipt.operating_system_error
    if create_fails:
        assert [call.args[0][1] for call in run.call_args_list] == ["create"]
        assert (receipt.failed_step, receipt.token) == (
            "create",
            "host_verification_quota_create_failed",
        )
        assert receipt.return_code == 1
        assert receipt.stderr_tail == "probe result"
    else:
        assert [call.args[0][1] for call in run.call_args_list] == ["create", "attach", "detach"]
        assert (receipt.failed_step, receipt.token) == (
            "backend",
            QUOTA_UNAVAILABLE_TOKEN,
        )


def test_preflight_initial_identity_read_failure_retains_root_without_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the created probe when its physical identity cannot be read."""
    identity = Mock(side_effect=OSError("The probe identity is unavailable."))
    monkeypatch.setattr(capabilities, "_quota_path_identity", identity)
    run = _runner()
    target = _request(tmp_path)
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    assert identity.called
    run.assert_not_called()
    assert not receipt.available
    root = tmp_path / "build/.host-verification" / target.request.request_id
    assert receipt.retained_root == str(root)
    assert root.is_dir()
    assert receipt.cleanup_state == "retained"
    assert "probe identity is unavailable" in receipt.operating_system_error
