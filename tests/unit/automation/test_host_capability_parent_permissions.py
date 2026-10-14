"""Test protected shared parents for host capability operations."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.host_capabilities import HdiutilQuotaBackend
from hephaestus.automation.models import DEFAULT_STATE_DIR
from tests.unit.automation.test_host_capabilities import _request, _runner


@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_preflight_rejects_writable_build_before_commands(tmp_path: Path, mode: int) -> None:
    """Reject shared write access before a host command starts."""
    build = tmp_path / "build"
    build.mkdir(mode=0o755)
    build.chmod(mode)
    run = _runner()
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path))
    assert not receipt.available
    run.assert_not_called()
    assert stat.S_IMODE(build.stat().st_mode) == mode


def test_preflight_accepts_protected_shared_build_without_chmod(tmp_path: Path) -> None:
    """Keep an owned shared parent at its protected mode."""
    build = tmp_path / "build"
    build.mkdir(mode=0o755)
    build.chmod(0o755)
    run = _runner()
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path))
    assert receipt.available
    assert [call.args[0][1] for call in run.call_args_list] == ["create", "attach", "detach"]
    assert stat.S_IMODE(build.stat().st_mode) == 0o755
    state = tmp_path / DEFAULT_STATE_DIR / "host-capability-receipts"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / f"{receipt.receipt_id}.json").stat().st_mode) == 0o600


def test_preflight_rejects_foreign_build_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a foreign parent through the descriptor metadata boundary."""
    build = tmp_path / "build"
    build.mkdir(mode=0o755)
    identity = build.stat()
    original = os.fstat

    def foreign_build(descriptor: int) -> os.stat_result:
        info = original(descriptor)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    run = _runner()
    monkeypatch.setattr(os, "fstat", foreign_build)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path))
    assert not receipt.available
    run.assert_not_called()


def test_receipt_parent_rejection_preserves_primary_detach_failure(tmp_path: Path) -> None:
    """Keep cleanup evidence when the parent becomes unsafe before storage."""
    build = tmp_path / "build"
    build.mkdir(mode=0o755)
    calls: list[str] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        step = argv[1]
        calls.append(step)
        if step == "detach":
            build.chmod(0o775)
        return subprocess.CompletedProcess(
            argv, int(step == "detach"), b"", b"detach remained uncertain"
        )

    target = _request(tmp_path)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    assert calls == ["create", "attach", "detach", "detach"]
    assert not receipt.available
    assert (receipt.failed_step, receipt.token) == (
        "detach",
        "host_verification_quota_detach_failed",
    )
    assert receipt.return_code == 1
    assert "detach remained uncertain" in receipt.stderr_tail
    assert receipt.cleanup_state == "retained"
    assert receipt.retained_root == str(build / ".host-verification" / target.request.request_id)
    assert Path(receipt.retained_root).is_dir()
    assert receipt.persistence_error
    assert receipt.persistence_exception_type == "ValueError"
    assert stat.S_IMODE(build.stat().st_mode) == 0o775
