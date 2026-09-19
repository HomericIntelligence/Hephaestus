"""Tests for explicit host capability contracts."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.host_capabilities import HdiutilQuotaBackend
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import (
    QUOTA_CREATE_FAILED_TOKEN,
    CapabilityReceiptTarget,
    CapabilityRequestTarget,
    HostCapabilityReceipt,
)


def _target(root: Path, *, phase: str = "pr_review") -> CapabilityRequestTarget:
    """Create one target without access to host configuration."""
    return CapabilityRequestTarget(
        "acme/repository",
        1,
        2,
        root,
        root,
        "a" * 40,
        phase,
        "scratch",
        "b" * 32,
        workspace=WorkspaceBinding.source(
            cwd=root,
            reusable_root=root,
            repository="acme/repository",
            ownership_key="owner",
            item_number=1,
            lane=SourceLane.REVIEW if phase == "pr_review" else SourceLane.IMPLEMENTATION,
            revision="a" * 40,
            generation=0,
            detached=phase == "pr_review",
        ),
        generation=1,
    )


@pytest.mark.parametrize("operation", ["rebase", "continue_rebase"])
def test_rebase_recovery_candidate_is_explicit_and_keyword_only(
    tmp_path: Path, operation: str
) -> None:
    """A pending candidate is separate from the admitted source binding."""
    from dataclasses import fields

    target = _target(tmp_path, phase="rebase")
    constructor: Any = GitJob
    job = constructor(
        "repository",
        operation,
        30,
        {},
        "Resume pending validation.",
        target.repository,
        None,
        target.workspace,
        None,
        None,
        capability_target=target,
        rebase_recovery_candidate="c" * 32,
    )
    assert job.rebase_recovery_candidate == "c" * 32
    assert job.workspace == target.workspace
    assert job.capability_target == target
    assert [field.name for field in fields(job) if not field.kw_only] == [
        "repo",
        "op",
        "timeout_s",
        "kwargs",
        "descr",
        "expected_repository",
        "deadline_s",
        "workspace",
        "repository_lock_wait_timeout_s",
        "repository_validation_preparation",
    ]


@pytest.mark.parametrize(
    "fault",
    ["short", "uppercase", "path", "boolean", "operation", "workspace", "target"],
)
def test_rebase_recovery_candidate_rejects_unbound_requests(tmp_path: Path, fault: str) -> None:
    """A candidate cannot authorize another operation or omit source identity."""
    target = _target(tmp_path, phase="rebase")
    values: dict[str, Any] = {
        "repo": "repository",
        "op": "rebase",
        "timeout_s": 30,
        "expected_repository": target.repository,
        "workspace": target.workspace,
        "capability_target": target,
        "rebase_recovery_candidate": "c" * 32,
    }
    if fault in {"short", "uppercase", "path", "boolean"}:
        values["rebase_recovery_candidate"] = {
            "short": "c" * 31,
            "uppercase": "C" * 32,
            "path": "../pending",
            "boolean": True,
        }[fault]
    elif fault == "operation":
        values["op"] = "commit_push"
        values["capability_target"] = None
    elif fault == "workspace":
        values["workspace"] = None
        values["capability_target"] = None
    else:
        values["capability_target"] = None
    constructor: Any = GitJob
    with pytest.raises(ValueError):
        constructor(**values)


def test_pending_rebase_discovery_does_not_require_a_supplied_workspace(tmp_path: Path) -> None:
    """Discovery can read current source ownership before stage reconstruction."""
    job = GitJob(
        "repository",
        "discover_pending_rebase",
        30,
        kwargs={"repo_root": str(tmp_path), "issue_number": 1},
        expected_repository="acme/repository",
    )
    assert job.workspace is None
    assert job.capability_target is None
    assert job.op == "discover_pending_rebase"


def test_capability_target_requires_explicit_workspace_and_generation(tmp_path: Path) -> None:
    """An operation target cannot omit its source and attempt ownership."""
    constructor: Any = CapabilityRequestTarget
    with pytest.raises(TypeError):
        constructor(
            "acme/repository",
            1,
            2,
            tmp_path,
            tmp_path,
            "a" * 40,
            "pr_review",
            "scratch",
            "b" * 32,
        )


@pytest.mark.parametrize("generation", [0, -1, True, "1"])
def test_capability_target_rejects_invalid_attempt_generation(
    tmp_path: Path, generation: Any
) -> None:
    """An attempt needs a positive integer generation, not a truth value."""
    with pytest.raises(ValueError):
        replace(_target(tmp_path), generation=generation)


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "other/repository"},
        {"revision": "c" * 40},
        {"item_number": 9},
        {"lane": SourceLane.IMPLEMENTATION},
        {"detached": False},
        {"ownership_key": ""},
        {"generation": True},
        {"cwd": Path("/another-checkout")},
        {"reusable_root": Path("/another-root")},
    ],
)
def test_capability_target_rejects_foreign_or_incomplete_workspace(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    """A request must not accept a different source or missing ownership."""
    target = _target(tmp_path)
    with pytest.raises(ValueError):
        replace(target, workspace=replace(target.workspace, **change))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_root", Path("relative")),
        ("checkout_path", Path("relative")),
        ("repository_root", "/untyped"),
        ("checkout_path", "/untyped"),
        ("repository_root", Path("/safe/../other")),
    ],
)
def test_capability_target_rejects_unsafe_path_data(tmp_path: Path, field: str, value: Any) -> None:
    """Reject malformed paths before a worker can use the target."""
    with pytest.raises(ValueError):
        replace(_target(tmp_path), **{field: value})


def test_capability_receipt_validation_has_no_filesystem_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pure receipt validation must use worker facts, not resolve paths again."""
    target = _target(tmp_path)

    def forbid_resolution(*args: object, **kwargs: object) -> Path:
        raise AssertionError("Pure receipt validation attempted filesystem resolution.")

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "resolve", forbid_resolution)
        receipt = CapabilityReceiptTarget(
            target, tmp_path, 1, "boundary-1", "a" * 40, backend="hdiutil-v1"
        )
    assert receipt.request == target


def test_rebase_capability_receipt_retains_input_and_result_heads(tmp_path: Path) -> None:
    """A rebase result keeps its input request and the worker-read resulting head."""
    target = _target(tmp_path, phase="rebase")
    receipt = CapabilityReceiptTarget(
        target, tmp_path, 1, "boundary-1", "c" * 40, backend="hdiutil-v1"
    )
    assert receipt.request.expected_head_sha == "a" * 40
    assert receipt.source_head_sha == "c" * 40


@pytest.mark.parametrize("phase", ["pr_review", "rebase"])
@pytest.mark.parametrize("pr_number", [None, 2, 0, -1, True, "2"])
def test_capability_request_pr_identity_depends_on_operation(
    tmp_path: Path, phase: str, pr_number: Any
) -> None:
    """Only an implementation rebase can start before a PR exists."""
    target = _target(tmp_path, phase=phase)
    valid = type(pr_number) is int and pr_number > 0
    valid = valid or (phase == "rebase" and pr_number is None)
    if not valid:
        with pytest.raises(ValueError):
            replace(target, pr_number=pr_number)
        return
    request = replace(target, pr_number=pr_number)
    assert request.pr_number == pr_number
    assert request.workspace == target.workspace
    assert request.expected_head_sha == target.expected_head_sha


@pytest.mark.parametrize("operation", ["rebase", "continue_rebase"])
def test_git_rebase_request_keeps_explicit_input_capability_target(
    tmp_path: Path, operation: str
) -> None:
    """Both Git rebase operations carry an explicit input capability request."""
    request = _target(tmp_path, phase="rebase")
    constructor: Any = GitJob
    job = constructor(
        "repository",
        operation,
        60,
        {"cwd": tmp_path},
        "rebase",
        request.repository,
        None,
        request.workspace,
        None,
        None,
        capability_target=request,
    )
    assert job.capability_target is request
    assert job.workspace == request.workspace
    assert job.transport_repository == request.repository
    assert job.kwargs == {"cwd": tmp_path}


@pytest.mark.parametrize("fault", ["type", "phase", "repository", "workspace", "operation"])
def test_git_rebase_rejects_foreign_capability_request(tmp_path: Path, fault: str) -> None:
    """A Git operation cannot use a foreign or malformed capability request."""
    request = _target(tmp_path, phase="rebase")
    job = GitJob(
        "repository",
        "rebase",
        60,
        expected_repository=request.repository,
        workspace=request.workspace,
        capability_target=request,
    )
    changes: dict[str, dict[str, Any]] = {
        "type": {"capability_target": object()},
        "phase": {"capability_target": _target(tmp_path)},
        "repository": {"expected_repository": "other/repository"},
        "workspace": {"workspace": replace(request.workspace, revision="d" * 40)},
        "operation": {"op": "commit_push"},
    }
    with pytest.raises(ValueError):
        replace(job, **changes[fault])


@pytest.mark.parametrize(
    ("source_repository", "transport_repository", "job_repository", "valid"),
    [
        ("repository", "acme/repository", "repository", True),
        ("repository", "other/repository", "repository", False),
        ("unrelated", "acme/repository", "repository", False),
        ("repository", "acme/repository", "unrelated", False),
    ],
    ids=["accepted-local-key", "foreign-owner", "foreign-source-key", "foreign-job-key"],
)
def test_rebase_request_keeps_local_source_and_canonical_transport_identities(
    tmp_path: Path,
    source_repository: str,
    transport_repository: str,
    job_repository: str,
    valid: bool,
) -> None:
    """A local source key requires the matching canonical Git transport identity."""
    original = _target(tmp_path, phase="rebase")

    def create_job() -> GitJob:
        workspace = replace(original.workspace, repository=source_repository)
        request = replace(original, workspace=workspace)
        return GitJob(
            job_repository,
            "rebase",
            60,
            expected_repository=transport_repository,
            workspace=workspace,
            capability_target=request,
        )

    if not valid:
        with pytest.raises(ValueError):
            create_job()
        return
    job = create_job()
    assert job.capability_target is not None
    assert job.capability_target.repository == "acme/repository"
    assert job.workspace is not None
    assert job.workspace.repository == "repository"
    assert job.transport_repository == "acme/repository"


def test_review_capability_rejects_a_short_source_repository_key(tmp_path: Path) -> None:
    """Source review still requires exact canonical repository identity."""
    target = _target(tmp_path)
    with pytest.raises(ValueError):
        replace(target, workspace=replace(target.workspace, repository="repository"))


def test_capability_receipt_requires_worker_verified_target() -> None:
    """An outcome without source and execution identity cannot be returned."""
    constructor: Any = HostCapabilityReceipt
    with pytest.raises(TypeError):
        constructor(False, QUOTA_CREATE_FAILED_TOKEN, "create", "scratch", "c" * 32)


def test_capability_receipt_requires_explicit_cleanup_state(tmp_path: Path) -> None:
    """An outcome cannot omit the cleanup state of its quota operation."""
    constructor: Any = HostCapabilityReceipt
    target = CapabilityReceiptTarget(
        _target(tmp_path),
        tmp_path,
        1,
        "boundary-1",
        "a" * 40,
        backend="hdiutil-v1",
    )
    with pytest.raises(TypeError):
        constructor(
            False,
            QUOTA_CREATE_FAILED_TOKEN,
            "create",
            "scratch",
            "c" * 32,
            target=target,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"available": 1},
        {"token": "unknown"},
        {"available": True},
        {"failed_step": None},
        {"return_code": True},
        {"exception_type": "x" * 4001},
        {"cached": True},
        {"probe_receipt_id": "not-an-id"},
    ],
)
def test_capability_receipt_rejects_contradictory_or_unbounded_data(
    change: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Reject result data that cannot prove one bounded capability outcome."""
    target = CapabilityReceiptTarget(
        _target(tmp_path), tmp_path, 1, "boundary-1", "a" * 40, backend="hdiutil-v1"
    )
    receipt = HostCapabilityReceipt(
        False,
        QUOTA_CREATE_FAILED_TOKEN,
        "create",
        "scratch",
        "c" * 32,
        target=target,
        cleanup_state="not_started",
    )
    with pytest.raises(ValueError):
        replace(receipt, **change)


def test_preflight_create_failure_returns_typed_redacted_receipt(tmp_path: Path) -> None:
    """A failed image creation identifies the create capability step."""
    target = _target(tmp_path)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=(), returncode=1, stdout=b"", stderr=b"token=secret create denied"
        )

    receipt = HdiutilQuotaBackend(
        host_probe=lambda: ("darwin", True), command_runner=run
    ).preflight(
        CapabilityReceiptTarget(
            target, tmp_path, tmp_path.stat().st_dev, "boundary-1", "a" * 40, backend="hdiutil-v1"
        )
    )

    assert not receipt.available
    assert receipt.token == QUOTA_CREATE_FAILED_TOKEN
    assert receipt.failed_step == "create"
    assert "secret" not in receipt.stderr_tail
