"""Tests for explicit host capability contracts."""

from __future__ import annotations

import os
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_ATTACH_FAILED_TOKEN,
    QUOTA_AVAILABLE_TOKEN,
    QUOTA_CREATE_FAILED_TOKEN,
    QUOTA_DETACH_FAILED_TOKEN,
    CapabilityReceiptTarget,
    CapabilityRequestTarget,
    DirectoryQuotaBackend,
    FakeGitSigningProvider,
    HdiutilQuotaBackend,
    HostCapabilityError,
    HostCapabilityReceipt,
    HostCapabilityReceiptStore,
    ProcessLocalPreflightCache,
)


def _request(
    tmp_path: Path, *, request_id: str = "b" * 32, purpose: str = "scratch"
) -> CapabilityRequestTarget:
    """Return one valid mixed-case repository request."""
    return CapabilityRequestTarget(
        repository="HomericIntelligence/Hephaestus",
        issue_number=2903,
        pr_number=3239,
        repository_root=tmp_path,
        checkout_path=tmp_path,
        expected_head_sha="a" * 40,
        phase="pr_review",
        purpose=purpose,
        request_id=request_id,
    )


def _target(
    request: CapabilityRequestTarget, *, boundary: str = "test-boundary"
) -> CapabilityReceiptTarget:
    """Bind a request to worker-validated facts."""
    return CapabilityReceiptTarget(
        request=request,
        canonical_repository_root=request.repository_root.resolve(),
        root_device=request.repository_root.stat().st_dev,
        execution_boundary_id=boundary,
        source_head_sha=request.expected_head_sha,
    )


def _completed(
    returncode: int = 0, *, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    """Return one deterministic hdiutil result."""
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr=stderr)


def test_request_accepts_canonical_mixed_case_repository(tmp_path: Path) -> None:
    """GitHub repository identity keeps its canonical letter case."""
    assert _request(tmp_path).repository == "HomericIntelligence/Hephaestus"


def test_receipt_round_trip_keeps_exact_worker_target(tmp_path: Path) -> None:
    """A receipt cannot lose the facts that bind it to its execution target."""
    target = _target(_request(tmp_path))
    receipt = HostCapabilityReceipt.available_receipt(target, receipt_id="c" * 32)

    assert HostCapabilityReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.target == target
    assert receipt.outcome == "available"


def test_receipt_store_writes_private_file_and_reads_it_back(tmp_path: Path) -> None:
    """The durable store writes and reads one exact target-bound receipt."""
    receipt = HostCapabilityReceipt.available_receipt(
        _target(_request(tmp_path)), receipt_id="c" * 32
    )

    stored = HostCapabilityReceiptStore(tmp_path).store(receipt)

    assert stored == receipt
    receipt_path = (
        tmp_path
        / "build"
        / ".issue_implementer"
        / "host-capability-receipts"
        / f"{receipt.receipt_id}.json"
    )
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert receipt_path.parent.stat().st_mode & 0o777 == 0o700
    assert HostCapabilityReceiptStore(tmp_path).read(receipt.receipt_id) == receipt


def test_receipt_store_rejects_symlink_parent(tmp_path: Path) -> None:
    """A state-directory symlink cannot redirect a durable receipt."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / ".issue_implementer").symlink_to(outside, target_is_directory=True)
    receipt = HostCapabilityReceipt.available_receipt(
        _target(_request(tmp_path)), receipt_id="c" * 32
    )

    with pytest.raises(ValueError, match="unsafe"):
        HostCapabilityReceiptStore(tmp_path).store(receipt)


def test_preflight_create_failure_returns_typed_redacted_receipt(tmp_path: Path) -> None:
    """A failed image creation identifies the create capability step."""
    receipt = HdiutilQuotaBackend(
        command_runner=lambda *a, **k: _completed(1, stderr=b"token=secret create denied")
    ).preflight(_target(_request(tmp_path)))

    assert not receipt.available
    assert receipt.token == QUOTA_CREATE_FAILED_TOKEN
    assert receipt.failed_step == "create"
    assert "secret" not in receipt.stderr_tail


def test_preflight_attach_failure_identifies_attach_step(tmp_path: Path) -> None:
    """A preflight must attach the image after it creates the image."""
    results = iter((_completed(), _completed(1, stderr=b"attach denied")))
    receipt = HdiutilQuotaBackend(command_runner=lambda *a, **k: next(results)).preflight(
        _target(_request(tmp_path))
    )

    assert receipt.token == QUOTA_ATTACH_FAILED_TOKEN
    assert receipt.failed_step == "attach"
    assert receipt.stderr_tail == "attach denied"


def test_preflight_detach_failure_identifies_detach_step_and_retains_root(
    tmp_path: Path,
) -> None:
    """A failed detach keeps the request directory for operator recovery."""
    results = iter((_completed(), _completed(), _completed(1, stderr=b"detach busy")))
    receipt = HdiutilQuotaBackend(command_runner=lambda *a, **k: next(results)).preflight(
        _target(_request(tmp_path))
    )

    assert receipt.token == QUOTA_DETACH_FAILED_TOKEN
    assert receipt.failed_step == "detach"
    assert receipt.stderr_tail == "detach busy"
    assert receipt.retained_root
    assert Path(receipt.retained_root).is_dir()


def test_preflight_success_removes_probe_root(tmp_path: Path) -> None:
    """A successful probe does not leave a stable request directory."""
    receipt = HdiutilQuotaBackend(command_runner=lambda *a, **k: _completed()).preflight(
        _target(_request(tmp_path))
    )

    assert receipt.available
    assert receipt.token == QUOTA_AVAILABLE_TOKEN
    assert not (tmp_path / "build" / ".host-verification" / ("b" * 32)).exists()


def test_volume_detaches_when_source_check_raises(tmp_path: Path) -> None:
    """A source-check exception does not leave its quota volume attached."""
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        return _completed()

    backend = HdiutilQuotaBackend(command_runner=run)

    with pytest.raises(RuntimeError, match="source check failed"):
        with backend.volume(_target(_request(tmp_path)), "scratch"):
            raise RuntimeError("source check failed")

    assert [command[1] for command in commands] == ["create", "attach", "detach"]


def test_secure_temporary_root_rejects_symlink_parent(tmp_path: Path) -> None:
    """A symlink in the host-verification path fails before hdiutil starts."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / ".host-verification").symlink_to(outside, target_is_directory=True)
    called = False

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return _completed()

    receipt = HdiutilQuotaBackend(command_runner=run).preflight(_target(_request(tmp_path)))

    assert not receipt.available
    assert receipt.failed_step == "backend"
    assert not called


def test_secure_temporary_root_rejects_non_directory_parent(tmp_path: Path) -> None:
    """A non-directory parent fails before hdiutil starts."""
    (tmp_path / "build").write_text("not a directory", encoding="utf-8")
    receipt = HdiutilQuotaBackend(command_runner=lambda *a, **k: _completed()).preflight(
        _target(_request(tmp_path))
    )

    assert not receipt.available
    assert receipt.failed_step == "backend"


def test_preflight_cache_is_scoped_to_process_boundary_root_device_and_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache result authorizes only one process and execution boundary."""
    calls = 0

    class Backend(HdiutilQuotaBackend):
        backend_id = "fake-v1"

        def preflight(self, target: CapabilityReceiptTarget) -> HostCapabilityReceipt:
            nonlocal calls
            calls += 1
            return HostCapabilityReceipt.available_receipt(target, receipt_id=f"{calls:032x}")

    cache = ProcessLocalPreflightCache()
    backend = Backend()
    target = _target(_request(tmp_path, request_id="1" * 32))
    first = cache.preflight(backend, target)
    same_scope = cache.preflight(backend, _target(_request(tmp_path, request_id="2" * 32)))

    assert calls == 1
    assert not first.cached
    assert same_scope.cached
    assert same_scope.probe_receipt_id == first.receipt_id
    assert same_scope.target.request.request_id == "2" * 32

    cache.preflight(
        backend,
        _target(_request(tmp_path, request_id="3" * 32), boundary="other"),
    )
    monkeypatch.setattr(os, "getpid", lambda: 999_999)
    cache.preflight(backend, target)
    assert calls == 3


def test_shared_backend_opens_scratch_and_pi_log_volumes(tmp_path: Path) -> None:
    """One backend implements both bounded writable volume purposes."""
    backend = HdiutilQuotaBackend(command_runner=lambda *a, **k: _completed())

    with ExitStack() as stack:
        scratch = stack.enter_context(backend.volume(_target(_request(tmp_path)), "scratch"))
        pi_logs = stack.enter_context(
            backend.volume(
                _target(_request(tmp_path, request_id="d" * 32, purpose="pi_smoke_logs")),
                "pi_smoke_logs",
            )
        )
        assert scratch.name == "scratch"
        assert pi_logs.name == "pi-smoke-logs"


def test_pi_smoke_log_create_failure_is_typed(tmp_path: Path) -> None:
    """Pi-log creation uses the shared typed failure contract."""
    backend = HdiutilQuotaBackend(command_runner=lambda *a, **k: _completed(1))
    target = _target(_request(tmp_path, purpose="pi_smoke_logs"))

    with (
        pytest.raises(HostCapabilityError) as caught,
        backend.volume(target, "pi_smoke_logs"),
    ):
        pass
    assert caught.value.receipt.token == QUOTA_CREATE_FAILED_TOKEN


def test_directory_backend_probes_private_pyxis_root(tmp_path: Path) -> None:
    """The Pyxis backend proves one private writable request directory."""
    quota_root = tmp_path / "quota"
    quota_root.mkdir(mode=0o700)
    backend = DirectoryQuotaBackend(quota_root)
    target = _target(_request(tmp_path))

    receipt = backend.preflight(target)

    assert receipt.available is True
    assert list(quota_root.iterdir()) == []


def test_directory_backend_rejects_public_pyxis_root(tmp_path: Path) -> None:
    """A public Pyxis root produces a typed unavailable receipt."""
    quota_root = tmp_path / "quota"
    quota_root.mkdir(mode=0o755)
    backend = DirectoryQuotaBackend(quota_root)

    receipt = backend.preflight(_target(_request(tmp_path)))

    assert receipt.available is False
    assert receipt.token == QUOTA_CREATE_FAILED_TOKEN


def test_fake_signing_provider_is_independent_of_global_git_configuration(
    tmp_path: Path,
) -> None:
    """A test signing provider returns only its injected configuration."""
    expected = {
        "user.name": "Test User",
        "user.email": "test@example.invalid",
        "gpg.format": "ssh",
        "user.signingkey": str(tmp_path / "id_ed25519"),
        "gpg.ssh.program": "/usr/bin/ssh-keygen",
    }
    provider = FakeGitSigningProvider(expected)

    assert provider.environment(tmp_path, timeout=1) == expected
