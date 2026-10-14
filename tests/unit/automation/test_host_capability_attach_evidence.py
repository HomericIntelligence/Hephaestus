"""Test primary attach evidence when later identity checks fail."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation import host_capabilities as capabilities
from tests.unit.automation.test_host_capabilities import _request


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_attach_failure_survives_identity_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    """Keep attach diagnostics and the inspection root after an identity error."""
    original = capabilities._quota_path_identity
    attached = False
    calls: list[str] = []

    def identity(root: Path, mount: Path) -> tuple[int, int, int, int]:
        if attached:
            raise error_type("Identity inspection failed after attach.")
        return original(root, mount)

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal attached
        del kwargs
        calls.append(argv[1])
        attached = argv[1] == "attach"
        return subprocess.CompletedProcess(
            argv, 17 if attached else 0, b"attach output", b"primary attach failure"
        )

    monkeypatch.setattr(capabilities, "_quota_path_identity", identity)
    target = _request(tmp_path)
    receipt = capabilities.HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(target)
    assert not receipt.available
    assert calls == ["create", "attach"]
    assert (receipt.failed_step, receipt.token) == (
        "attach",
        "host_verification_quota_attach_failed",
    )
    assert receipt.return_code == 17
    assert receipt.stdout_tail == "attach output"
    assert receipt.stderr_tail == "primary attach failure"
    assert "Identity inspection failed after attach" in receipt.operating_system_error
    root = tmp_path / "build/.host-verification" / target.request.request_id
    assert receipt.cleanup_state == "retained"
    assert receipt.retained_root == str(root)
    assert root.is_dir()
