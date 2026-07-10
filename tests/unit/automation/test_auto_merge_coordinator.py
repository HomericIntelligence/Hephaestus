"""Regression tests for legacy auto-merge containment during the #2054 bootstrap."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

from hephaestus.automation.auto_merge_coordinator import AutoMergeCoordinator
from hephaestus.automation.ci_run_coordinator import CIDriveRunCoordinator
from hephaestus.automation.models import CIDriverOptions


def _coordinator(gh_call: Any, gh_pr_state: Any) -> AutoMergeCoordinator:
    """Build the legacy coordinator with inert collaborators for containment tests."""
    return AutoMergeCoordinator(
        options_provider=lambda: cast(CIDriverOptions, SimpleNamespace(dry_run=False)),
        status_tracker_provider=lambda: SimpleNamespace(),
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
    states = iter(
        [
            {"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}},
            {"state": "OPEN", "autoMergeRequest": None},
        ]
    )
    calls: list[list[str]] = []

    def gh_call(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    coordinator = _coordinator(gh_call, lambda _pr_number: next(states))

    remaining = coordinator.arm_all_unarmed_open_prs(
        [{"number": 42, "autoMergeRequest": {"enabledAt": "stale"}}]
    )

    assert remaining == [{"number": 42, "autoMergeRequest": {"enabledAt": "stale"}}]
    assert calls == [["pr", "merge", "42", "--disable-auto"]]


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
