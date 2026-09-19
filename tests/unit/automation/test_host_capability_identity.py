"""Test capability identity checks with controlled metadata."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import host_capabilities as capabilities
from hephaestus.automation.pipeline.host_capabilities import CapabilityDeadline
from tests.unit.automation.test_host_capabilities import _request, _runner


def _different_inode(info: os.stat_result) -> os.stat_result:
    """Change only the reported inode, not the physical entry."""
    fields = list(info)
    fields[1] += 1
    return os.stat_result(fields)


@pytest.mark.parametrize("boundary", ["before_write", "after_readback"])
def test_preflight_rejects_receipt_directory_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Do not report availability after receipt namespace identity changes."""
    original_stat, original_fstat = os.stat, os.fstat
    original_lock = capabilities._receipt_lock
    original_readback = capabilities._receipt_readback
    active = False
    identity: tuple[int, int] | None = None
    observations = 0

    def observed(info: os.stat_result) -> os.stat_result:
        nonlocal observations
        if active and (info.st_dev, info.st_ino) == identity:
            observations += 1
            return _different_inode(info)
        return info

    def read_stat(*args: Any, **kwargs: Any) -> os.stat_result:
        return observed(original_stat(*args, **kwargs))

    def read_fstat(descriptor: int) -> os.stat_result:
        return observed(original_fstat(descriptor))

    @contextmanager
    def lock(descriptor: int, deadline: CapabilityDeadline) -> Iterator[None]:
        nonlocal active, identity
        info = original_fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        with original_lock(descriptor, deadline):
            active = boundary == "before_write"
            yield

    def readback(descriptor: int, name: str, expected: str) -> None:
        nonlocal active
        original_readback(descriptor, name, expected)
        active = True

    monkeypatch.setattr(os, "stat", read_stat)
    monkeypatch.setattr(os, "fstat", read_fstat)
    monkeypatch.setattr(capabilities, "_receipt_lock", lock)
    monkeypatch.setattr(capabilities, "_receipt_readback", readback)
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=_runner()
    ).preflight(_request(tmp_path))
    assert not receipt.available
    assert receipt.failed_step == "storage"
    assert receipt.persistence_error
    assert observations > 0


def test_preflight_rejects_receipt_lock_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held lock must identify the live lock entry."""
    original = os.stat
    observations = 0

    def read_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal observations
        info = original(path, *args, **kwargs)
        if path == "receipts.lock":
            observations += 1
            return _different_inode(info)
        return info

    monkeypatch.setattr(os, "stat", read_stat)
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=_runner()
    ).preflight(_request(tmp_path))
    assert not receipt.available
    assert receipt.failed_step == "storage"
    assert receipt.persistence_error
    assert observations > 0


def test_preflight_rejects_probe_identity_mismatch_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the created probe identity, not a new identity at first use."""
    original = capabilities._quota_path_identity
    original_volume = capabilities.quota_backed_volume
    active = False
    observations = 0

    def observed(root: Path, mountpoint: Path) -> tuple[int, int, int, int]:
        nonlocal observations
        device, inode, mount_device, mount_inode = original(root, mountpoint)
        if active:
            observations += 1
            inode += 1
        return device, inode, mount_device, mount_inode

    @contextmanager
    def volume(*args: Any, **kwargs: Any) -> Iterator[Path]:
        nonlocal active
        active = True
        with original_volume(*args, **kwargs) as mountpoint:
            yield mountpoint

    monkeypatch.setattr(capabilities, "_quota_path_identity", observed)
    monkeypatch.setattr(capabilities, "quota_backed_volume", volume)
    run = _runner()
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path))
    assert not receipt.available
    run.assert_not_called()
    assert observations > 0
