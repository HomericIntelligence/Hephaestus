"""Regression tests for legacy auto-merge containment during the #2054 bootstrap."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hephaestus.automation.auto_merge_coordinator import AutoMergeCoordinator
from hephaestus.automation.ci_run_coordinator import CIDriveRunCoordinator
from hephaestus.automation.models import CIDriverOptions


def _coordinator(gh_call: Any, gh_pr_state: Any, *, dry_run: bool = False) -> AutoMergeCoordinator:
    """Build the legacy coordinator with inert collaborators for containment tests."""
    return AutoMergeCoordinator(
        options_provider=lambda: cast(CIDriverOptions, SimpleNamespace(dry_run=dry_run)),
        status_tracker_provider=lambda: SimpleNamespace(update_slot=lambda *_args: None),
        get_pr_branch=lambda _pr_number: "feature",
        is_bot_pr_mode=lambda _issue_number, _pr_number: False,
        gh_call=gh_call,
        gh_pr_state=gh_pr_state,
        gh_pr_checks=lambda _pr_number, _dry_run: [],
        failing_required_check_names=lambda _pr_number: [],
        pending_required_check_names=lambda _pr_number: [],
        fix_flow=SimpleNamespace(),
        arming=SimpleNamespace(),
        review_threads=SimpleNamespace(),
        attempt_mechanical_rebase=lambda _issue_number, _pr_number, _slot: False,
        recheck_and_arm_after_fix=lambda *_args, **_kwargs: None,
    )


def test_legacy_open_pr_sweep_disables_prearmed_auto_merge() -> None:
    """The legacy final sweep must contain an existing arm, not skip it."""
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            ),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None}),
            ),
        ]
    )
    calls: list[list[str]] = []

    def gh_call(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return next(responses)

    coordinator = _coordinator(gh_call, lambda _pr_number: {"state": "OPEN"})

    remaining = coordinator.arm_all_unarmed_open_prs(
        [{"number": 42, "autoMergeRequest": {"enabledAt": "stale"}}]
    )

    assert remaining == [{"number": 42, "autoMergeRequest": None}]
    assert calls == [
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
        ["pr", "merge", "42", "--disable-auto"],
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
    ]


def test_legacy_open_pr_sweep_raises_when_containment_cannot_be_verified() -> None:
    """A failed disable/readback cannot be reported as ordinary remaining work."""
    coordinator = _coordinator(
        lambda _args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="gh failed"),
        lambda _pr_number: {"state": "OPEN"},
    )

    with pytest.raises(RuntimeError, match="could not verify auto-merge disabled"):
        coordinator.arm_all_unarmed_open_prs([{"number": 42}])


def test_legacy_open_pr_sweep_rejects_a_pr_without_a_positive_number() -> None:
    """A malformed sweep record fails closed instead of skipping containment."""
    coordinator = _coordinator(
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN"},
    )

    with pytest.raises(RuntimeError, match="invalid PR number"):
        coordinator.arm_all_unarmed_open_prs([{"number": None}])


def test_legacy_final_sweep_propagates_containment_failure() -> None:
    """The final sweep cannot complete after its containment seam fails."""

    def containment_failure(_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise RuntimeError("could not verify auto-merge disabled for legacy open PR #42")

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(dry_run=False, issues=[]),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(list_open_prs_remaining=lambda: [{"number": 42}]),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=containment_failure),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    with pytest.raises(RuntimeError, match="could not verify auto-merge disabled"):
        coordinator._final_open_prs({})


def test_legacy_scoped_final_sweep_preserves_the_unknown_discovery_sentinel() -> None:
    """Issue scoping cannot turn a failed discovery into an empty final sweep."""
    unknown = [{"number": -1, "title": "(unknown: gh api pulls failed)"}]

    def containment_failure(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert prs == unknown
        raise RuntimeError("cannot verify auto-merge disabled: invalid PR number")

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(dry_run=False, issues=[2054]),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(list_open_prs_remaining=lambda: unknown),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=containment_failure),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    with pytest.raises(RuntimeError, match="cannot verify auto-merge disabled"):
        coordinator._final_open_prs({2054: 42})


def test_legacy_scoped_final_sweep_contains_same_head_siblings() -> None:
    """Issue filtering retains every open PR sharing a scoped PR's head."""
    remaining = [
        {"number": 42, "headRefName": "2054-auto-impl"},
        {"number": 43, "headRefName": "2054-auto-impl"},
    ]
    contained: list[dict[str, Any]] = []

    def contain(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contained.extend(prs)
        return prs

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(dry_run=False, issues=[2054]),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(list_open_prs_remaining=lambda: remaining),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=contain),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    assert coordinator._final_open_prs({2054: 42}) == remaining
    assert contained == remaining


def test_legacy_empty_workset_still_runs_the_final_containment_sweep() -> None:
    """A failed direct-PR discovery must not bypass final open-PR containment."""
    remaining = [{"number": 42, "headRefName": "2054-auto-impl"}]
    contained: list[dict[str, Any]] = []

    def contain(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contained.extend(prs)
        return prs

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(
            dry_run=False,
            issues=[],
            prs=[42],
            include_bot_prs=False,
            max_workers=1,
        ),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(
            discover_workset=lambda _issues: SimpleNamespace(pr_map={}, shared_pr_issues={}),
            list_open_prs_remaining=lambda: remaining,
        ),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=contain),
        arming=SimpleNamespace(sweep_orphaned_records=lambda: None),
        set_shared_pr_issues=lambda _shared: None,
    )

    assert coordinator.run() == {}
    assert contained == remaining


def test_legacy_empty_issue_workset_does_not_filter_away_final_containment() -> None:
    """An empty issue discovery result cannot prove that unrelated PRs are safe."""
    remaining = [{"number": 42, "headRefName": "2054-auto-impl"}]
    contained: list[dict[str, Any]] = []

    def contain(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contained.extend(prs)
        return prs

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(
            dry_run=False,
            issues=[2054],
            prs=[],
            include_bot_prs=False,
            max_workers=1,
        ),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(
            discover_workset=lambda _issues: SimpleNamespace(pr_map={}, shared_pr_issues={}),
            list_open_prs_remaining=lambda: remaining,
        ),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=contain),
        arming=SimpleNamespace(sweep_orphaned_records=lambda: None),
        set_shared_pr_issues=lambda _shared: None,
    )

    assert coordinator.run() == {}
    assert contained == remaining


def test_legacy_drive_stops_after_verified_auto_merge_deferral() -> None:
    """A legacy drive must not poll or wait on a pre-existing auto-merge arm."""
    deferred: list[int] = []

    class _Status:
        @contextmanager
        def slot(self):
            yield 0

    def defer_auto_merge(pr_number: int) -> bool:
        deferred.append(pr_number)
        return True

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(),
        worktree_manager=SimpleNamespace(),
        status_tracker=_Status(),
        discovery=SimpleNamespace(),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(defer_auto_merge=defer_auto_merge),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    result = coordinator.drive_issue(issue_number=7, pr_number=42, slot_id=0)

    assert deferred == [42]
    assert result.success is False
    assert result.error == "strict_gate_unavailable"


def test_legacy_dry_run_still_delegates_auto_merge_deferral() -> None:
    """Dry-run records the would-defer action instead of returning before containment."""
    deferred: list[int] = []

    class _Status:
        @contextmanager
        def slot(self):
            yield 0

    def defer_auto_merge(pr_number: int) -> bool:
        deferred.append(pr_number)
        return True

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(dry_run=True),
        worktree_manager=SimpleNamespace(),
        status_tracker=_Status(),
        discovery=SimpleNamespace(),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(defer_auto_merge=defer_auto_merge),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    result = coordinator.drive_issue(issue_number=7, pr_number=42, slot_id=0)

    assert deferred == [42]
    assert result.error == "strict_gate_unavailable"


def test_legacy_drive_reports_failed_auto_merge_deferral() -> None:
    """Legacy CI returns the explicit containment failure rather than continuing."""

    class _Status:
        @contextmanager
        def slot(self):
            yield 0

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(dry_run=False),
        worktree_manager=SimpleNamespace(),
        status_tracker=_Status(),
        discovery=SimpleNamespace(),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(defer_auto_merge=lambda _pr_number: False),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    result = coordinator.drive_issue(issue_number=7, pr_number=42, slot_id=0)

    assert result.success is False
    assert result.error == "auto_merge_disable_failed"


def test_legacy_coordinator_dry_run_deferral_avoids_gh_mutation() -> None:
    """The compatibility defer seam logs a no-op rather than calling gh in dry-run."""
    calls: list[list[str]] = []
    coordinator = _coordinator(
        lambda args, **_kwargs: calls.append(args),
        lambda _pr_number: {"state": "OPEN"},
        dry_run=True,
    )

    assert coordinator.defer_auto_merge(42) is True
    assert calls == []


def test_legacy_arm_and_wait_refuses_even_during_dry_run() -> None:
    """The retired compatibility entry reports the unavailable strict gate, never success."""
    coordinator = _coordinator(
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN"},
        dry_run=True,
    )

    result = coordinator.arm_and_wait_for_merge(issue_number=7, pr_number=42, acquired_slot=0)

    assert result.success is False
    assert result.error == "strict_gate_unavailable"


def test_legacy_arm_and_wait_reports_failed_auto_merge_containment() -> None:
    """The retired entry surfaces a failed disable/readback as a containment error."""
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "still"}}),
                stderr="",
            ),
        ]
    )
    coordinator = _coordinator(
        lambda _args, **_kwargs: next(responses),
        lambda _pr_number: {"state": "OPEN"},
    )

    result = coordinator.arm_and_wait_for_merge(issue_number=7, pr_number=42, acquired_slot=0)

    assert result.success is False
    assert result.error == "auto-merge containment failed for PR ProjectHephaestus#42"


def test_legacy_enable_auto_merge_contains_a_prearmed_pr_before_refusing() -> None:
    """The retired armer keeps the same view-disable-readback containment contract."""
    calls: list[list[str]] = []
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            ),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps({"state": "OPEN", "autoMergeRequest": None}),
            ),
        ]
    )

    def gh_call(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return next(responses)

    coordinator = _coordinator(gh_call, lambda _pr_number: {"state": "OPEN"})

    assert coordinator.enable_auto_merge(42) is False
    assert calls == [
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
        ["pr", "merge", "42", "--disable-auto"],
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
    ]


def test_legacy_coordinator_rejects_an_incomplete_open_pr_state() -> None:
    """A compatibility caller cannot treat an omitted arm field as unarmed."""

    def gh_call(_args: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"state": "OPEN"}))

    coordinator = _coordinator(gh_call, lambda _pr_number: {"state": "OPEN"})

    assert coordinator.defer_auto_merge(42) is False
