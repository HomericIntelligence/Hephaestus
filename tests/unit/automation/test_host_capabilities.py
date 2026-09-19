"""Test durable, confined host capability evidence and process-local reuse."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.host_capabilities import HdiutilQuotaBackend, PyxisQuotaBackend
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.host_capabilities import (
    CapabilityReceiptTarget,
    CapabilityRequestTarget,
)
from hephaestus.automation.pyxis_artifact_io import CrossNodePathBinding
from hephaestus.automation.source_worktree import _PreparationDeadline


def _request(root: Path) -> CapabilityReceiptTarget:
    """Create a request with no ambient host identity dependency."""
    request = CapabilityRequestTarget(
        "acme/repository",
        1,
        2,
        root,
        root,
        "a" * 40,
        "pr_review",
        "scratch",
        "b" * 32,
        workspace=WorkspaceBinding.source(
            cwd=root,
            reusable_root=root,
            repository="acme/repository",
            ownership_key="owner",
            item_number=1,
            lane=SourceLane.REVIEW,
            revision="a" * 40,
            generation=0,
            detached=True,
        ),
        generation=1,
    )
    return CapabilityReceiptTarget(
        request, root, root.stat().st_dev, "test-boundary", "a" * 40, backend="hdiutil-v1"
    )


def _runner(returncode: int = 0) -> Mock:
    """Control only the external host command boundary."""
    return Mock(return_value=subprocess.CompletedProcess((), returncode, b"", b"probe result"))


@pytest.mark.parametrize("returncode", [0, 1])
def test_preflight_persists_private_target_bound_receipt(tmp_path: Path, returncode: int) -> None:
    """Each success or failure has durable target data before it returns."""
    target = _request(tmp_path)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=_runner(returncode)
    ).preflight(target)
    path = tmp_path / DEFAULT_STATE_DIR / "host-capability-receipts" / f"{receipt.receipt_id}.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["receipt"]["receipt_id"] == receipt.receipt_id
    assert data["target"]["request"]["request_id"] == target.request.request_id
    assert data["target"]["source_head_sha"] == target.source_head_sha
    assert data["target"]["root_device"] == tmp_path.stat().st_dev


@pytest.mark.parametrize("component", ["state", "host-capability-receipts"])
def test_preflight_rejects_symlink_receipt_parent(tmp_path: Path, component: str) -> None:
    """An unsafe receipt parent cannot produce available capability evidence."""
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / DEFAULT_STATE_DIR
    state.parent.mkdir(mode=0o755)
    if component == "host-capability-receipts":
        state.mkdir(mode=0o700)
    link = state if component == "state" else state / component
    link.symlink_to(outside, target_is_directory=True)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=_runner()
    ).preflight(_request(tmp_path))
    assert not receipt.available
    assert receipt.failed_step == "storage"
    assert list(outside.iterdir()) == []


def test_preflight_cache_rebinds_receipts_and_does_not_survive_provider_restart(
    tmp_path: Path,
) -> None:
    """One provider reuses its probe but gives each request a distinct receipt."""
    target = _request(tmp_path)
    run = _runner()
    provider = HdiutilQuotaBackend(host_probe=lambda: ("darwin", True), command_runner=run)
    first = provider.preflight(target)
    probe_calls = run.call_count
    assert probe_calls > 0
    second_target = replace(
        target, request=replace(target.request, request_id="c" * 32, pr_number=3)
    )
    second = provider.preflight(second_target)
    assert second.available
    assert run.call_count == probe_calls
    assert second.cached is True
    assert second.probe_receipt_id == first.receipt_id
    assert second.receipt_id != first.receipt_id
    third = HdiutilQuotaBackend(host_probe=lambda: ("darwin", True), command_runner=run).preflight(
        replace(target, request=replace(target.request, request_id="d" * 32))
    )
    assert third.available
    assert not third.cached
    assert run.call_count > probe_calls


def test_preflight_cache_does_not_cross_a_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied provider must probe again after its process identity changes."""
    target = _request(tmp_path)
    run = _runner()
    provider = HdiutilQuotaBackend(host_probe=lambda: ("darwin", True), command_runner=run)
    first = provider.preflight(target)
    assert first.available
    calls = run.call_count
    monkeypatch.setattr(os, "getpid", lambda: 987654321)
    second = provider.preflight(
        replace(target, request=replace(target.request, request_id="e" * 32))
    )
    assert second.available
    assert not second.cached
    assert run.call_count > calls


def test_controlled_command_runner_does_not_select_a_macos_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command substitute must not grant an unsupported platform capability."""
    run = _runner()
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, "platform", "linux")
        receipt = HdiutilQuotaBackend(command_runner=run).preflight(_request(tmp_path))
    assert not receipt.available
    assert receipt.failed_step == "backend"
    run.assert_not_called()


@pytest.mark.parametrize("component", [".host-verification", "request"])
def test_preflight_rejects_symlink_probe_directory(tmp_path: Path, component: str) -> None:
    """The host command must not receive a path through a substituted directory."""
    request = _request(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "build" / ".host-verification"
    link = parent if component == ".host-verification" else parent / request.request.request_id
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    run = _runner()
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(request)
    assert not receipt.available
    run.assert_not_called()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("purpose", ["scratch", "pi_smoke_logs"])
def test_preflight_checks_attach_and_detach_before_availability(
    tmp_path: Path, purpose: str
) -> None:
    """Creation alone does not establish the complete quota lifecycle."""
    run = _runner()
    target = _request(tmp_path)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(replace(target, request=replace(target.request, purpose=purpose)))
    assert receipt.available
    assert [call.args[0][1] for call in run.call_args_list] == ["create", "attach", "detach"]


@pytest.mark.parametrize("failed_step", ["attach", "detach"])
def test_preflight_preserves_lifecycle_failure_and_cleanup_state(
    tmp_path: Path, failed_step: str
) -> None:
    """A failed attach or exhausted detach has a distinct bounded receipt."""
    calls: list[str] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        step = argv[1]
        calls.append(step)
        return subprocess.CompletedProcess(
            argv, int(step == failed_step), b"", f"{step} diagnostic".encode()
        )

    request = _request(tmp_path)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(request)
    assert not receipt.available
    assert receipt.failed_step == failed_step
    assert f"{failed_step} diagnostic" in receipt.stderr_tail
    assert "detach" in calls
    if failed_step == "detach":
        assert calls.count("detach") == 2
        assert receipt.retained_root
        assert Path(receipt.retained_root).is_dir()
        assert Path(receipt.retained_root).is_relative_to(tmp_path / "build" / ".host-verification")
    else:
        assert receipt.retained_root == ""


def test_preflight_preserves_attach_cause_when_detach_also_fails(tmp_path: Path) -> None:
    """An uncertain attach keeps its primary error and the cleanup error."""

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        step = argv[1]
        return subprocess.CompletedProcess(
            argv, int(step != "create"), b"", f"{step} diagnostic".encode()
        )

    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path))
    assert not receipt.available
    assert receipt.failed_step == "attach"
    assert "attach diagnostic" in receipt.stderr_tail
    assert "detach diagnostic" in receipt.operating_system_error
    assert receipt.retained_root


@pytest.mark.parametrize("output_kind", ["text", "bytes"])
@pytest.mark.parametrize("detach_fails", [False, True])
def test_preflight_timeout_preserves_redacted_output_and_cleanup(
    tmp_path: Path, output_kind: str, detach_fails: bool
) -> None:
    """Keep timeout output and the primary cause after cleanup."""
    calls: list[str] = []
    stdout = "x" * 5000 + " token=secret stdout-end"
    stderr = "y" * 5000 + " token=secret stderr-end"

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        step = argv[1]
        calls.append(step)
        if step == "attach":
            raise subprocess.TimeoutExpired(
                argv,
                1,
                output=stdout.encode() if output_kind == "bytes" else stdout,
                stderr=stderr.encode() if output_kind == "bytes" else stderr,
            )
        return subprocess.CompletedProcess(
            argv,
            int(step == "detach" and detach_fails),
            b"",
            b"detach diagnostic",
        )

    target = _request(tmp_path)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    assert not receipt.available
    assert (receipt.failed_step, receipt.token) == (
        "attach",
        "host_verification_quota_attach_failed",
    )
    assert receipt.exception_type == "TimeoutExpired"
    assert receipt.return_code is None
    assert "timed out" in receipt.operating_system_error
    assert calls == ["create", "attach", "detach"] + (["detach"] if detach_fails else [])
    root = tmp_path / "build/.host-verification" / target.request.request_id
    assert receipt.cleanup_state == ("retained" if detach_fails else "complete")
    assert receipt.retained_root == (str(root) if detach_fails else "")
    assert root.exists() is detach_fails
    if detach_fails:
        assert "detach diagnostic" in receipt.operating_system_error
    for tail, ending in (
        (receipt.stdout_tail, "stdout-end"),
        (receipt.stderr_tail, "stderr-end"),
    ):
        assert tail.endswith(ending)
        assert 0 < len(tail) <= 4000
        assert "secret" not in tail
    path = tmp_path / DEFAULT_STATE_DIR / "host-capability-receipts" / f"{receipt.receipt_id}.json"
    stored = json.loads(path.read_text())["receipt"]
    assert stored["stdout_tail"] == receipt.stdout_tail
    assert stored["stderr_tail"] == receipt.stderr_tail
    assert stored["cleanup_state"] == receipt.cleanup_state
    assert stored["retained_root"] == receipt.retained_root


@pytest.mark.parametrize("detach_restores", [True, False])
def test_preflight_accepts_owned_mount_identity_transition(
    tmp_path: Path, detach_restores: bool
) -> None:
    """A successful mount can change its inode before a verified detach."""
    calls: list[str] = []
    mountpoint: Path | None = None
    original: Path | None = None

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal mountpoint, original
        del kwargs
        step = argv[1]
        calls.append(step)
        if step == "attach":
            mountpoint = Path(argv[argv.index("-mountpoint") + 1])
            original = mountpoint.with_name("unmounted-directory")
            mountpoint.rename(original)
            mountpoint.mkdir(mode=0o700)
        elif step == "detach" and detach_restores:
            assert mountpoint is not None and original is not None
            mountpoint.rmdir()
            original.rename(mountpoint)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True),
        command_runner=run,
    ).preflight(_request(tmp_path))
    assert receipt.available is detach_restores
    if detach_restores:
        assert calls == ["create", "attach", "detach"]
        assert receipt.retained_root == ""
    else:
        assert calls == ["create", "attach", "detach", "detach"]
        assert receipt.failed_step == "detach"
        assert receipt.retained_root


def test_receipt_storage_failure_keeps_uncertain_detach_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later storage failure must not hide the retained inspection root."""
    target = _request(tmp_path)

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        return subprocess.CompletedProcess(
            argv, int(argv[1] == "detach"), b"", b"detach remained uncertain"
        )

    monkeypatch.setattr(
        "hephaestus.automation.host_capabilities._write_receipt",
        Mock(side_effect=OSError("receipt storage unavailable")),
    )
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    retained = tmp_path / "build/.host-verification" / target.request.request_id
    assert not receipt.available
    assert (receipt.failed_step, receipt.token) == (
        "detach",
        "host_verification_quota_detach_failed",
    )
    assert retained.is_dir()
    assert receipt.retained_root == str(retained)
    assert "detach remained uncertain" in receipt.stderr_tail
    assert receipt.persistence_error == "receipt storage unavailable"
    assert receipt.persistence_exception_type == "OSError"
    assert receipt.cleanup_state == "retained"
    assert receipt.return_code == 1


@pytest.mark.parametrize("expires_after_create", [False, True])
def test_preflight_bounds_commands_with_the_remaining_operation_budget(
    tmp_path: Path, expires_after_create: bool
) -> None:
    """The provider uses the remaining budget and still bounds detach cleanup."""
    clock = [0.0]
    calls: list[tuple[str, float]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv[1], float(str(kwargs["timeout"]))))
        clock[0] += 2.0 if expires_after_create else 0.3
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(_request(tmp_path), deadline=_PreparationDeadline(1.0, lambda: clock[0]))
    assert calls[0] == ("create", 1.0)
    if expires_after_create:
        assert [step for step, _ in calls] == ["create", "detach"]
        assert not receipt.available
        assert receipt.failed_step == "attach"
    else:
        assert receipt.available
        assert calls[1] == ("attach", 0.7)
    assert calls[-1][0] == "detach"
    assert 0 < calls[-1][1] <= 30


def test_preflight_cache_lock_wait_cannot_outlive_its_budget(tmp_path: Path) -> None:
    """An unavailable cache lock cannot start a probe after the deadline."""
    clock = [0.0]
    run = _runner()
    backend = HdiutilQuotaBackend(host_probe=lambda: ("darwin", True), command_runner=run)
    lock = MagicMock()

    def unavailable(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        clock[0] += 2.0
        return False

    lock.acquire.side_effect = unavailable
    lock.__enter__.side_effect = unavailable
    backend._lock = lock
    try:
        receipt = backend.preflight(
            _request(tmp_path), deadline=_PreparationDeadline(1.0, lambda: clock[0])
        )
    except RuntimeError:
        pass
    else:
        assert not receipt.available
    run.assert_not_called()
    lock.release.assert_not_called()


def test_preflight_receipt_lock_wait_is_nonblocking_and_deadline_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held receipt lock must not extend the caller's operation budget."""
    import fcntl

    clock = [0.0]
    flags: list[int] = []

    def locked(descriptor: int, operation: int) -> None:
        del descriptor
        flags.append(operation)
        clock[0] += 2.0
        raise BlockingIOError("Receipt lock is held.")

    monkeypatch.setattr(fcntl, "flock", locked)
    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=_runner()
    ).preflight(_request(tmp_path), deadline=_PreparationDeadline(1.0, lambda: clock[0]))
    assert not receipt.available
    assert flags == [fcntl.LOCK_EX | fcntl.LOCK_NB]
    assert receipt.failed_step == "storage"
    assert receipt.persistence_error


@pytest.mark.parametrize("fault", ["none", "runtime", "image", "quota", "binding", "deadline"])
def test_linux_preflight_requires_existing_runtime_image_and_retained_quota_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Linux preflight must keep all configured isolation checks and close its binding."""
    clock = [0.0]
    target = replace(_request(tmp_path), backend="pyxis-v1")
    quota_root = tmp_path / "quota"
    binding = MagicMock(spec=CrossNodePathBinding)
    binding.path = quota_root
    if fault == "binding":
        binding.revalidate.side_effect = OSError("Quota identity changed.")
    image = Mock(
        side_effect=ValueError("Image authority is invalid.") if fault == "image" else None
    )
    quota = Mock(
        return_value=binding,
        side_effect=ValueError("Quota is unavailable.") if fault == "quota" else None,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.host_verification_pyxis.validate_pyxis_image", image
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.host_verification_pyxis.validate_pyxis_quota_root", quota
    )

    def runtime_available() -> bool:
        if fault == "deadline":
            clock[0] += 2
        return fault != "runtime"

    runtime = Mock(side_effect=runtime_available)
    monkeypatch.setattr(
        "hephaestus.automation.host_capabilities.subprocess.run",
        Mock(side_effect=AssertionError("Linux must not run an hdiutil probe.")),
    )
    receipt = PyxisQuotaBackend(
        image=tmp_path / "image.sqsh",
        image_sha256="a" * 64,
        authority=tmp_path / "authority.json",
        quota_root=quota_root,
        runtime_available=runtime,
        execution_boundary_id="test-boundary",
    ).preflight(target, deadline=_PreparationDeadline(1.0, lambda: clock[0]))
    runtime.assert_called_once_with()
    assert receipt.available is (fault == "none")
    if fault in {"runtime", "deadline"}:
        image.assert_not_called()
        quota.assert_not_called()
    else:
        image.assert_called_once_with(
            tmp_path / "image.sqsh",
            expected_sha256="a" * 64,
            provenance=tmp_path / "authority.json",
        )
        if fault == "image":
            quota.assert_not_called()
        else:
            quota.assert_called_once_with(quota_root, retain_binding=True)
    if fault in {"none", "binding"}:
        binding.revalidate.assert_called_once_with()
        binding.close.assert_called_once_with()
    assert receipt.target == target
