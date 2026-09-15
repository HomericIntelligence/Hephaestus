"""Tests for explicit host capability contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_CREATE_FAILED_TOKEN,
    CapabilityRequestTarget,
    HdiutilQuotaBackend,
)


def test_preflight_create_failure_returns_typed_redacted_receipt(tmp_path: Path) -> None:
    """A failed image creation identifies the create capability step."""
    target = CapabilityRequestTarget(
        repository="acme/repository",
        issue_number=1,
        pr_number=2,
        repository_root=tmp_path,
        checkout_path=tmp_path,
        expected_head_sha="a" * 40,
        phase="pr_review",
        purpose="scratch",
        request_id="b" * 32,
    )

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=(), returncode=1, stdout=b"", stderr=b"token=secret create denied"
        )

    receipt = HdiutilQuotaBackend(command_runner=run).preflight(target)

    assert not receipt.available
    assert receipt.token == QUOTA_CREATE_FAILED_TOKEN
    assert receipt.failed_step == "create"
    assert "secret" not in receipt.stderr_tail
