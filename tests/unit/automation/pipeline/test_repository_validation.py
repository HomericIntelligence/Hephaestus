"""Check admission of local and CI evidence for one immutable review plan."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind

HEAD = "a" * 40
BASE = "b" * 40
SOURCES = ((".github/workflows/ci.yml", 512, "c" * 64), ("uv.lock", 4096, "d" * 64))


def _api() -> ModuleType:
    name = "hephaestus.automation.pipeline.repository_validation"
    assert importlib.util.find_spec(name) is not None, "Repository validation is not available."
    return importlib.import_module(name)


def _plan(api: ModuleType, root: Path) -> Any:
    workspace = WorkspaceBinding.source(
        cwd=root,
        reusable_root=root.parent,
        repository="LLM360/comet",
        ownership_key="comet-review-1639",
        item_number=1623,
        lane=SourceLane.REVIEW,
        revision=HEAD,
        generation=1,
        detached=True,
    )
    checks = tuple(
        api.RepositoryValidationCheck(
            check_id=check_id,
            argv=argv,
            source_digests=SOURCES,
        )
        for check_id, argv in (
            ("comet.python.ruff-check", ("uv", "run", "--locked", "ruff", "check", ".")),
            (
                "comet.python.ruff-format",
                ("uv", "run", "--locked", "ruff", "format", "--check", "."),
            ),
        )
    )
    return api.RepositoryValidationPlan(
        repository="LLM360/comet",
        issue_number=1623,
        pr_number=1639,
        reviewed_head=HEAD,
        reviewed_base=BASE,
        diff_base_sha=BASE,
        source_workspace=workspace,
        changes=(("M", "src/comet/proxy/app.py"),),
        profile_id="comet-v1",
        profile_digest="e" * 64,
        checks=checks,
    )


def _receipt(api: ModuleType, plan: Any, index: int, kind: str = "ci") -> Any:
    check = plan.checks[index]
    return api.RepositoryValidationReceipt(
        repository=plan.repository,
        pr_number=plan.pr_number,
        plan_id=plan.plan_id,
        check_id=check.check_id,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        argv=check.argv,
        source_digests=check.source_digests,
        evidence_kind=kind,
        status="success",
    )


def _merge(api: ModuleType, plan: Any, receipts: tuple[Any, ...], **current: str) -> Any:
    return api.merge_repository_validation_receipts(
        plan,
        receipts,
        reviewed_head=current.get("reviewed_head", HEAD),
        reviewed_base=current.get("reviewed_base", BASE),
    )


@pytest.mark.parametrize("kinds", [("ci", "ci"), ("local", "local"), ("ci", "local")])
def test_complete_coverage_accepts_each_evidence_source(
    tmp_path: Path, kinds: tuple[str, str]
) -> None:
    """Check that complete coverage accepts each evidence source."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipts = tuple(_receipt(api, plan, index, kind) for index, kind in enumerate(kinds))

    result = _merge(api, plan, receipts)

    assert result.status == "complete"
    assert result.receipts == receipts
    assert result.uncovered_check_ids == ()
    assert result.gaps == ()


def test_partial_ci_keeps_success_and_identifies_only_the_uncovered_check(tmp_path: Path) -> None:
    """Check that partial ci keeps success and identifies only the uncovered check."""
    api = _api()
    plan = _plan(api, tmp_path)
    ci = _receipt(api, plan, 0)

    partial = _merge(api, plan, (ci,))

    assert partial.status == "gap"
    assert partial.receipts == (ci,)
    assert partial.uncovered_check_ids == (plan.checks[1].check_id,)
    assert (
        _merge(api, plan, (*partial.receipts, _receipt(api, plan, 1, "local"))).status == "complete"
    )


@pytest.mark.parametrize("field", ["reviewed_head", "reviewed_base"])
def test_current_revision_drift_invalidates_complete_evidence(tmp_path: Path, field: str) -> None:
    """Check that current revision drift invalidates complete evidence."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipts = tuple(_receipt(api, plan, index) for index in range(2))

    result = _merge(api, plan, receipts, **{field: "f" * 40})

    assert result.status == "gap"
    assert result.receipts == ()
    assert result.uncovered_check_ids == tuple(check.check_id for check in plan.checks)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "foreign/comet"),
        ("pr_number", 1640),
        ("plan_id", "f" * 64),
        ("check_id", "comet.python.ty-check"),
        ("reviewed_head", "f" * 40),
        ("reviewed_base", "f" * 40),
        ("argv", ("uv", "run", "echo", "success")),
        ("source_digests", (("uv.lock", 4096, "f" * 64),)),
        ("evidence_kind", "agent"),
        ("evidence_kind", "local"),
        ("status", "completed"),
    ],
)
def test_changed_receipt_cannot_cover_a_check(tmp_path: Path, field: str, value: Any) -> None:
    """Check that changed receipt cannot cover a check."""
    api = _api()
    plan = _plan(api, tmp_path)
    local = _receipt(api, plan, 0, "local")
    changed = _receipt(api, plan, 1)
    object.__setattr__(changed, field, value)

    result = _merge(api, plan, (local, changed))

    assert result.status == "gap"
    assert result.receipts == (local,)
    assert plan.checks[1].check_id in result.uncovered_check_ids


@pytest.mark.parametrize("order", [(0, 1, 1), (1, 0, 1), (1, 1, 0)])
@pytest.mark.parametrize("duplicated_kind", ["ci", "local"])
def test_duplicate_receipt_does_not_remove_other_evidence_kind(
    tmp_path: Path, order: tuple[int, ...], duplicated_kind: str
) -> None:
    """Check that duplicate receipt does not remove other evidence kind."""
    api = _api()
    plan = _plan(api, tmp_path)
    retained_kind = "local" if duplicated_kind == "ci" else "ci"
    retained = _receipt(api, plan, 0, retained_kind)
    duplicate = _receipt(api, plan, 1, duplicated_kind)

    values = (retained, duplicate)
    result = _merge(api, plan, tuple(values[index] for index in order))

    assert result.status == "gap"
    assert result.receipts == (retained,)


@pytest.mark.parametrize("failed_first", [True, False])
@pytest.mark.parametrize("failed_kind", ["ci", "local"])
def test_known_failed_check_cannot_be_replaced_by_other_success(
    tmp_path: Path, failed_first: bool, failed_kind: str
) -> None:
    """Check that known failed check cannot be replaced by other success."""
    api = _api()
    plan = _plan(api, tmp_path)
    failed = replace(_receipt(api, plan, 0, failed_kind), status="failed")
    other_kind = "local" if failed_kind == "ci" else "ci"
    other = tuple(_receipt(api, plan, index, other_kind) for index in range(2))

    receipts = (failed, *other) if failed_first else (*other, failed)
    result = _merge(api, plan, receipts)

    assert result.status == "gap"
    assert result.gaps


def test_blocked_validation_control_cannot_accept_passing_receipts(tmp_path: Path) -> None:
    """Check that blocked validation control cannot accept passing receipts."""
    api = _api()
    plan = replace(
        _plan(api, tmp_path),
        execution_allowed=False,
        coverage_reason="validation_control_changed",
    )
    receipts = tuple(_receipt(api, plan, index) for index in range(2))

    result = _merge(api, plan, receipts)

    assert result.status == "gap"
    assert result.receipts == ()


def test_empty_selection_is_not_complete_coverage(tmp_path: Path) -> None:
    """Check that empty selection is not complete coverage."""
    api = _api()
    plan = replace(_plan(api, tmp_path), checks=())
    assert _merge(api, plan, ()).status == "gap"


@pytest.mark.parametrize(
    "field",
    ["issue_number", "pr_number", "changes", "profile_digest", "source_workspace", "diff_base_sha"],
)
def test_plan_identity_includes_context_and_selection(tmp_path: Path, field: str) -> None:
    """Check that plan identity includes context and selection."""
    api = _api()
    plan = _plan(api, tmp_path)
    changes = {
        "issue_number": 1624,
        "pr_number": 1640,
        "changes": (("M", "src/comet/gateway_client.py"),),
        "profile_digest": "f" * 64,
        "diff_base_sha": "f" * 40,
        "source_workspace": replace(plan.source_workspace, generation=2),
    }
    updates = {field: changes[field]}
    if field == "issue_number":
        updates["source_workspace"] = replace(plan.source_workspace, item_number=1624)
    assert replace(plan, **updates).plan_id != plan.plan_id


@pytest.mark.parametrize("field", ["reviewed_head", "repository", "source_workspace"])
def test_mutated_plan_is_not_a_valid_coverage_authority(tmp_path: Path, field: str) -> None:
    """Check that mutated plan is not a valid coverage authority."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    values = {
        "reviewed_head": "f" * 40,
        "repository": "foreign/comet",
        "source_workspace": replace(plan.source_workspace, detached=False),
    }
    object.__setattr__(plan, field, values[field])
    assert _merge(api, plan, receipts).status == "gap"


def test_mutated_nested_binding_cannot_reuse_the_stored_plan_identity(tmp_path: Path) -> None:
    """Check that mutated nested binding cannot reuse the stored plan identity."""
    api = _api()
    plan = _plan(api, tmp_path)
    identity = plan.plan_id
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    object.__setattr__(plan.source_workspace, "generation", 2)

    result = _merge(api, plan, receipts)

    assert plan.plan_id == identity
    assert result.status == "gap"
    assert result.receipts == ()


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "repository",
        "issue_number",
        "pr_number",
        "reviewed_head",
        "reviewed_base",
        "diff_base_sha",
        "source_workspace",
        "changes",
        "profile_id",
        "profile_digest",
        "checks",
        "execution_allowed",
        "coverage_reason",
        "plan_id",
    ],
)
def test_each_changed_plan_field_invalidates_its_stored_identity(
    tmp_path: Path, field: str
) -> None:
    """Check that each changed plan field invalidates its stored identity."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    changes = {
        "schema_version": 2,
        "repository": "foreign/comet",
        "issue_number": 1624,
        "pr_number": 1640,
        "reviewed_head": "f" * 40,
        "reviewed_base": "f" * 40,
        "diff_base_sha": "f" * 40,
        "source_workspace": replace(plan.source_workspace, generation=2),
        "changes": (("M", "src/comet/gateway_client.py"),),
        "profile_id": "comet-v2",
        "profile_digest": "f" * 64,
        "checks": tuple(reversed(plan.checks)),
        "execution_allowed": False,
        "coverage_reason": "validation_control_changed",
        "plan_id": "f" * 64,
    }
    object.__setattr__(plan, field, changes[field])

    result = _merge(api, plan, receipts)

    assert result.status == "gap"
    assert result.receipts == ()


@pytest.mark.parametrize("field", ["check_id", "argv", "source_digests", "source_size"])
def test_nested_check_mutation_invalidates_plan_identity(tmp_path: Path, field: str) -> None:
    """Check that nested check mutation invalidates plan identity."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    values = {
        "check_id": "comet.python.ty-check",
        "argv": ("uv", "run", "echo", "success"),
        "source_digests": (("uv.lock", 4096, "f" * 64),),
        "source_size": ((".github/workflows/ci.yml", 513, "c" * 64), SOURCES[1]),
    }
    target = "source_digests" if field == "source_size" else field
    object.__setattr__(plan.checks[0], target, values[field])
    assert _merge(api, plan, receipts).status == "gap"


@pytest.mark.parametrize("item_number", [1639, 9999])
def test_source_binding_must_belong_to_the_issue(tmp_path: Path, item_number: int) -> None:
    """Check that source binding must belong to the issue."""
    api = _api()
    plan = _plan(api, tmp_path)
    with pytest.raises(ValueError):
        replace(plan, source_workspace=replace(plan.source_workspace, item_number=item_number))


def test_a_failed_receipt_cannot_be_mutated_into_success(tmp_path: Path) -> None:
    """Check that a failed receipt cannot be mutated into success."""
    api = _api()
    plan = _plan(api, tmp_path)
    receipt = replace(_receipt(api, plan, 0), status="failed")
    object.__setattr__(receipt, "status", "success")
    assert _merge(api, plan, (receipt, _receipt(api, plan, 1))).status == "gap"


def test_pr_only_plan_uses_pr_workspace_ownership(tmp_path: Path) -> None:
    """Check that pr only plan uses pr workspace ownership."""
    api = _api()
    original = _plan(api, tmp_path)
    plan = replace(
        original,
        issue_number=None,
        source_workspace=replace(original.source_workspace, item_number=original.pr_number),
    )
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    assert _merge(api, plan, receipts).status == "complete"
    with pytest.raises(ValueError):
        replace(plan, source_workspace=replace(plan.source_workspace, item_number=1623))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", WorkspaceKind.SESSION_ONLY),
        ("kind", "source"),
        ("lane", SourceLane.IMPLEMENTATION),
        ("lane", "review"),
        ("detached", False),
        ("repository", "foreign/comet"),
        ("revision", "f" * 40),
        ("schema_version", 2),
        ("dirty_claim", object()),
        ("ownership_key", ""),
        ("generation", -1),
        ("cwd", Path("relative")),
        ("reusable_root", None),
    ],
)
def test_plan_rejects_invalid_source_authority(tmp_path: Path, field: str, value: Any) -> None:
    """Check that plan rejects invalid source authority."""
    api = _api()
    plan = _plan(api, tmp_path)
    workspace = replace(plan.source_workspace, **{field: value})
    with pytest.raises(ValueError):
        replace(plan, source_workspace=workspace)


@pytest.mark.parametrize("duplicated_kind", ["ci", "local"])
def test_duplicate_for_one_check_keeps_other_checks(tmp_path: Path, duplicated_kind: str) -> None:
    """Keep valid evidence for other checks when one check has a duplicate."""
    api = _api()
    plan = _plan(api, tmp_path)
    first = _receipt(api, plan, 0, duplicated_kind)
    duplicate = _receipt(api, plan, 1, duplicated_kind)
    other_kind = "local" if duplicated_kind == "ci" else "ci"
    replacement = _receipt(api, plan, 1, other_kind)

    result = _merge(api, plan, (first, duplicate, duplicate, replacement))

    assert result.status == "gap"
    assert result.receipts == (first, replacement)
    assert result.uncovered_check_ids == ()


@pytest.mark.parametrize("failed_kind", ["ci", "local"])
def test_failure_remains_after_another_merge(tmp_path: Path, failed_kind: str) -> None:
    """Keep a failed check when later successful evidence is added."""
    api = _api()
    plan = _plan(api, tmp_path)
    failure = replace(_receipt(api, plan, 0, failed_kind), status="failed")
    first = _merge(api, plan, (failure,))
    other_kind = "local" if failed_kind == "ci" else "ci"
    successes = tuple(_receipt(api, plan, index, other_kind) for index in range(2))

    result = _merge(api, plan, (*first.receipts, *successes))

    assert result.status == "gap"
    assert failure in result.receipts


@pytest.mark.parametrize("diff_base", [None, "", "short", "f" * 39, "g" * 40])
def test_executable_plan_requires_exact_diff_base(tmp_path: Path, diff_base: object) -> None:
    """Reject execution without an immutable diff base."""
    with pytest.raises(ValueError, match="diff base"):
        replace(_plan(_api(), tmp_path), diff_base_sha=diff_base)


def test_source_inspection_placeholder_cannot_grant_coverage(tmp_path: Path) -> None:
    """Keep an unknown diff base confined to a nonexecutable plan."""
    api = _api()
    plan = replace(
        _plan(api, tmp_path),
        diff_base_sha=None,
        execution_allowed=False,
        coverage_reason="Source inspection is incomplete.",
    )
    receipts = tuple(_receipt(api, plan, index) for index in range(2))
    assert _merge(api, plan, receipts).status == "gap"


def _attempt_api() -> ModuleType:
    api = _api()
    assert hasattr(api, "RepositoryValidationAttempt"), "The live validation attempt is missing."
    return api


def _attempt(api: ModuleType, root: Path) -> Any:
    return api.RepositoryValidationAttempt(_plan(api, root), 1, "1" * 32)


def _coverage(api: ModuleType, attempt: Any) -> Any:
    return api.validation_attempt_coverage(attempt, reviewed_head=HEAD, reviewed_base=BASE)


def test_attempt_combines_partial_ci_and_local_evidence(tmp_path: Path) -> None:
    """Complete coverage only after both correlated callbacks arrive."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    checks = tuple(check.check_id for check in attempt.plan.checks)
    attempt, ci = api.begin_validation_request(attempt, "ci", checks)
    assert _coverage(api, attempt).status == "gap"
    attempt = api.consume_validation_result(attempt, ci, (_receipt(api, attempt.plan, 0),))
    assert attempt.pending is None
    assert _coverage(api, attempt).uncovered_check_ids == (checks[1],)
    attempt, local = api.begin_validation_request(attempt, "local", (checks[1],))
    attempt = api.consume_validation_result(
        attempt, local, (_receipt(api, attempt.plan, 1, "local"),)
    )
    assert _coverage(api, attempt).status == "complete"
    assert len(attempt.consumed_request_ids) == 2


@pytest.mark.parametrize("field", ["generation", "request_nonce", "plan", "check_ids"])
def test_attempt_rejects_stale_callback_without_clearing_pending(
    tmp_path: Path, field: str
) -> None:
    """Keep a stale callback as a terminal gap after a valid callback."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    checks = tuple(check.check_id for check in attempt.plan.checks)
    attempt, request = api.begin_validation_request(attempt, "ci", checks)
    mutations = {
        "generation": 2,
        "request_nonce": "2" * 32,
        "plan": replace(attempt.plan, profile_digest="f" * 64),
        "check_ids": (checks[0],),
    }
    stale = replace(request, **{field: mutations[field]})
    attempt = api.consume_validation_result(attempt, stale, ())
    assert attempt.pending == request
    assert attempt.gaps
    attempt = api.consume_validation_result(
        attempt, request, tuple(_receipt(api, attempt.plan, index) for index in range(2))
    )
    assert attempt.pending is None
    assert _coverage(api, attempt).status == "gap"
    assert attempt.gaps


def test_attempt_duplicate_invalidates_only_its_evidence_slot(tmp_path: Path) -> None:
    """Preserve other checks when a completed request arrives again."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    checks = tuple(check.check_id for check in attempt.plan.checks)
    attempt, first = api.begin_validation_request(attempt, "ci", (checks[0],))
    receipt = _receipt(api, attempt.plan, 0)
    attempt = api.consume_validation_result(attempt, first, (receipt,))
    attempt, second = api.begin_validation_request(attempt, "local", (checks[1],))
    other = _receipt(api, attempt.plan, 1, "local")
    attempt = api.consume_validation_result(attempt, second, (other,))
    assert _coverage(api, attempt).status == "complete"
    attempt = api.consume_validation_result(attempt, first, (receipt,))
    assert attempt.receipts == (other,)
    assert _coverage(api, attempt).status == "gap"
    for _ in range(3):
        attempt = api.consume_validation_result(attempt, first, (receipt,))
        assert attempt.receipts == (other,)
        assert _coverage(api, attempt).uncovered_check_ids == (checks[0],)


@pytest.mark.parametrize("first_kind", ["ci", "local"])
def test_attempt_failure_survives_a_later_success(tmp_path: Path, first_kind: str) -> None:
    """Do not erase a failed check with success from the other source."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    checks = tuple(check.check_id for check in attempt.plan.checks)
    attempt, first = api.begin_validation_request(attempt, first_kind, checks)
    failed = replace(_receipt(api, attempt.plan, 0, first_kind), status="failed")
    attempt = api.consume_validation_result(attempt, first, (failed,))
    other_kind = "local" if first_kind == "ci" else "ci"
    attempt, second = api.begin_validation_request(attempt, other_kind, checks)
    attempt = api.consume_validation_result(
        attempt, second, tuple(_receipt(api, attempt.plan, i, other_kind) for i in range(2))
    )
    assert failed in attempt.receipts
    assert _coverage(api, attempt).status == "gap"


@pytest.mark.parametrize("callback", ["unknown", "wrong-kind", "unrequested-check", "oversized"])
def test_attempt_rejects_malformed_callback(tmp_path: Path, callback: str) -> None:
    """Retain bounded terminal gaps without storing rejected raw objects."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    first = attempt.plan.checks[0].check_id
    attempt, request = api.begin_validation_request(attempt, "ci", (first,))
    receipts: tuple[Any, ...] = (_receipt(api, attempt.plan, 0),)
    if callback == "unknown":
        request = object()
    elif callback == "wrong-kind":
        receipts = (_receipt(api, attempt.plan, 0, "local"),)
    elif callback == "unrequested-check":
        receipts = (_receipt(api, attempt.plan, 1),)
    else:
        receipts = receipts * 129
    attempt = api.consume_validation_result(attempt, request, receipts)
    assert _coverage(api, attempt).status == "gap"
    assert attempt.gaps
    assert len(attempt.gaps) <= 64
    assert attempt.receipts == ()


def test_attempt_cannot_replace_an_active_request(tmp_path: Path) -> None:
    """Keep one pending request until its correlated callback arrives."""
    api = _attempt_api()
    attempt = _attempt(api, tmp_path)
    checks = tuple(check.check_id for check in attempt.plan.checks)
    pending, request = api.begin_validation_request(attempt, "ci", checks)
    with pytest.raises(ValueError, match="pending"):
        api.begin_validation_request(pending, "local", checks)
    assert pending.pending == request
    assert attempt.pending is None


@pytest.mark.parametrize(
    "fault",
    ["none", "both", "ci", "head", "base", "plan", "command", "digest", "gap_check"],
)
def test_local_result_rejects_conflicting_evidence(tmp_path: Path, fault: str) -> None:
    """Reject local evidence that does not identify the complete execution request."""
    from tests.unit.automation.pipeline.test_jobs import _repository_execution

    api = _api()
    execution = _repository_execution(tmp_path)
    receipt = _receipt(api, execution.plan, 0, "local")
    gap = api.RepositoryValidationGap(execution.check_id, "local_execution_unavailable")
    if fault in {"none", "both"}:
        arguments = {} if fault == "none" else {"receipt": receipt, "gap": gap}
    elif fault == "gap_check":
        arguments = {"gap": api.RepositoryValidationGap("other", gap.reason)}
    else:
        overrides: dict[str, dict[str, Any]] = {
            "ci": {"evidence_kind": "ci"},
            "head": {"reviewed_head": "0" * 40},
            "base": {"reviewed_base": "0" * 40},
            "plan": {"plan_id": "0" * 64},
            "command": {"argv": ("echo", "other")},
        }
        if fault == "digest":
            object.__setattr__(receipt, "receipt_id", "0" * 64)
        else:
            receipt = replace(receipt, **overrides[fault])
        arguments = {"receipt": receipt}
    with pytest.raises(ValueError):
        api.RepositoryValidationLocalRead(execution, **arguments)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/project"),
        ("generation", 0),
        ("request_nonce", "short"),
        ("deadline_s", 0),
        ("deadline_s", float("inf")),
        ("deadline_s", True),
        ("changes", [("M", "src/comet/proxy/app.py")]),
        ("changes", (("M", "../outside"),)),
    ],
)
def test_source_preparation_request_rejects_incomplete_identity(
    tmp_path: Path, field: str, value: Any
) -> None:
    """Keep source preparation finite, immutable, and bound to the review."""
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationSourceRead,
        RepositoryValidationSourceRequest,
    )

    plan = _plan(_api(), tmp_path)
    request = RepositoryValidationSourceRequest(
        plan.repository,
        plan.issue_number,
        plan.pr_number,
        plan.source_workspace,
        plan.reviewed_head,
        plan.reviewed_base,
        plan.diff_base_sha,
        plan.changes,
        1,
        "f" * 32,
        123.0,
    )
    assert RepositoryValidationSourceRead(request, plan).plan == plan
    with pytest.raises(ValueError):
        replace(request, **{field: value})
    with pytest.raises(ValueError):
        RepositoryValidationSourceRead(request, replace(plan, reviewed_base="e" * 40))


@pytest.mark.parametrize("fault", ["ci", "multiple", "deadline", "foreign_execution"])
def test_runtime_preparation_rejects_incomplete_invocation(tmp_path: Path, fault: str) -> None:
    """A runtime result cannot change the reserved local invocation."""
    from hephaestus.automation.pipeline.repository_validation_preparation import (
        RepositoryValidationRuntimeRead,
        RepositoryValidationRuntimeRequest,
    )
    from tests.unit.automation.pipeline.test_jobs import _repository_execution

    api = _api()
    execution = _repository_execution(tmp_path)
    invocation = api.RepositoryValidationInvocation(
        execution.plan, 1, "f" * 32, "local", (execution.check_id,)
    )
    request = RepositoryValidationRuntimeRequest(invocation, 123.0)
    if fault == "foreign_execution":
        with pytest.raises(ValueError):
            RepositoryValidationRuntimeRead(request, replace(execution, request_nonce="e" * 32))
        return
    if fault == "ci":
        invocation = replace(invocation, evidence_kind="ci")
    elif fault == "multiple":
        invocation = replace(
            invocation, check_ids=tuple(check.check_id for check in execution.plan.checks)
        )
    with pytest.raises(ValueError):
        RepositoryValidationRuntimeRequest(
            invocation, float("nan") if fault == "deadline" else 123.0
        )
