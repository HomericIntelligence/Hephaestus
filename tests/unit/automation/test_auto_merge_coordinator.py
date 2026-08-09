"""Regression tests for legacy auto-merge containment during the #2054 bootstrap."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from hephaestus.automation.auto_merge_coordinator import AutoMergeCoordinator
from hephaestus.automation.ci_run_coordinator import (
    CiConclusion,
    CIDriveRunCoordinator,
    PrMergeState,
    _contain_remaining_prs,
    _poll_post_fix_required,
    classify_ci_state,
    classify_pr_merge_state,
)
from hephaestus.automation.git_utils import pr_ref
from hephaestus.automation.models import CIDriverOptions, WorkerResult


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


def test_wait_for_pr_terminal_routes_any_failed_required_check() -> None:
    """Every failed required check is actionable while waiting for merge."""
    coordinator = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN", "mergeStateStatus": "BLOCKED"},
    )
    coordinator._failing_required_check_names = lambda _pr_number: ["auto-merge-policy"]

    assert coordinator.wait_for_pr_terminal(7, 42) == "FAILING"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"state": "MERGED"}, "MERGED"),
        ({"state": "CLOSED"}, "CLOSED"),
        ({"state": "OPEN", "mergeStateStatus": "DIRTY"}, "DIRTY"),
        ({"state": "OPEN", "mergeStateStatus": "BLOCKED"}, "BLOCKED"),
    ],
)
def test_wait_for_pr_terminal_routes_live_terminal_states(
    state: dict[str, Any], expected: str
) -> None:
    """Live PR facts produce their corresponding actionable terminal state."""
    coordinator = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: state,
    )

    assert coordinator.wait_for_pr_terminal(7, 42) == expected


def test_wait_for_pr_terminal_bounds_dry_run_and_persistent_pending_state() -> None:
    """Dry-run and a persistently open PR both stop within their configured bound."""
    dry = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN"},
        dry_run=True,
    )
    assert dry.wait_for_pr_terminal(7, 42) == "TIMEOUT"

    pending = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN", "mergeStateStatus": "BLOCKED"},
    )
    pending._pending_required_check_names = lambda _pr_number: ["tests"]
    with patch("hephaestus.automation.auto_merge_coordinator.read_timeout_env", return_value=0):
        assert pending.wait_for_pr_terminal(7, 42) == "TIMEOUT"


def test_resolve_dirty_pr_rechecks_after_mechanical_rebase() -> None:
    """A successful mechanical rebase is revalidated before reporting success."""
    expected = WorkerResult(issue_number=7, success=False, pr_number=42, error="recheck")
    coordinator = AutoMergeCoordinator(
        options_provider=lambda: cast(CIDriverOptions, SimpleNamespace(dry_run=False)),
        status_tracker_provider=lambda: SimpleNamespace(update_slot=lambda *_args: None),
        get_pr_branch=lambda _pr: "feature",
        is_bot_pr_mode=lambda *_args: False,
        gh_call=cast(Any, lambda *_args, **_kwargs: SimpleNamespace(stdout="{}")),
        gh_pr_state=lambda _pr: {"state": "OPEN"},
        gh_pr_checks=lambda *_args: [],
        failing_required_check_names=lambda _pr: [],
        pending_required_check_names=lambda _pr: [],
        fix_flow=SimpleNamespace(),
        arming=SimpleNamespace(),
        review_threads=SimpleNamespace(),
        attempt_mechanical_rebase=lambda *_args: True,
        recheck_and_arm_after_fix=lambda *_args, **_kwargs: expected,
    )

    assert coordinator.resolve_dirty_pr(7, 42, 1) is expected


def test_resolve_dirty_pr_routes_conflict_context_through_fix_flow() -> None:
    """An unresolved mechanical conflict is handed to the fixing agent with its base."""
    contexts: list[str] = []
    fixed = WorkerResult(issue_number=7, success=True, pr_number=42)

    def attempt(*_args: object, extra_context: str) -> WorkerResult:
        contexts.append(extra_context)
        return fixed

    coordinator = AutoMergeCoordinator(
        options_provider=lambda: cast(CIDriverOptions, SimpleNamespace(dry_run=False)),
        status_tracker_provider=lambda: SimpleNamespace(update_slot=lambda *_args: None),
        get_pr_branch=lambda _pr: "feature",
        is_bot_pr_mode=lambda *_args: False,
        gh_call=cast(
            Any,
            lambda *_args, **_kwargs: SimpleNamespace(stdout='{"baseRefName":"develop"}'),
        ),
        gh_pr_state=lambda _pr: {"state": "OPEN"},
        gh_pr_checks=lambda *_args: [],
        failing_required_check_names=lambda _pr: [],
        pending_required_check_names=lambda _pr: [],
        fix_flow=SimpleNamespace(attempt_ci_fixes=attempt),
        arming=SimpleNamespace(),
        review_threads=SimpleNamespace(),
        attempt_mechanical_rebase=lambda *_args: False,
        recheck_and_arm_after_fix=lambda *_args, **_kwargs: None,
    )

    assert coordinator.resolve_dirty_pr(7, 42, 1) is fixed
    assert "origin/develop" in contexts[0]


def test_resolve_dirty_pr_reports_failed_agent_resolution_and_dry_run() -> None:
    """Failed resolution remains explicit while dry-run avoids all mutation."""
    coordinator = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(stdout="not-json"),
        lambda _pr_number: {"state": "OPEN"},
    )
    coordinator._fix_flow = SimpleNamespace(attempt_ci_fixes=lambda *_args, **_kwargs: None)
    result = coordinator.resolve_dirty_pr(7, 42, 1)
    assert result.success is False
    assert result.error is not None and "unresolved merge conflict" in result.error

    dry = _coordinator(
        lambda *_args, **_kwargs: SimpleNamespace(stdout="{}"),
        lambda _pr_number: {"state": "OPEN"},
        dry_run=True,
    )
    assert dry.resolve_dirty_pr(7, 42, 1).success is True


def test_implementation_go_label_lookup_handles_success_and_invalid_json() -> None:
    """The legacy label gate reads GO and fails closed on malformed GitHub data."""
    responses = iter(
        [
            SimpleNamespace(stdout='{"labels":[{"name":"state:implementation-go"}]}'),
            SimpleNamespace(stdout="not-json"),
        ]
    )
    coordinator = _coordinator(lambda *_args, **_kwargs: next(responses), lambda _pr: {})

    assert coordinator.pr_has_implementation_go(42) is True
    assert coordinator.pr_has_implementation_go(42) is False


def test_handle_failing_pr_routes_any_failed_required_check_to_fix_flow() -> None:
    """A former advisory-check failure cannot bypass the CI fix flow."""
    calls: list[tuple[int, int, int]] = []
    expected = WorkerResult(issue_number=7, success=False, pr_number=42, error="fix failed")

    def attempt_ci_fixes(issue: int, pr: int, slot: int) -> WorkerResult:
        calls.append((issue, pr, slot))
        return expected

    coordinator = CIDriveRunCoordinator(
        options_provider=lambda: SimpleNamespace(max_fix_iterations=1),
        worktree_manager=SimpleNamespace(),
        status_tracker=SimpleNamespace(),
        discovery=SimpleNamespace(),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(attempt_ci_fixes=attempt_ci_fixes),
        auto_merge=SimpleNamespace(),
        arming=SimpleNamespace(),
        set_shared_pr_issues=lambda _shared: None,
    )

    result = coordinator.handle_failing_pr(
        7,
        42,
        0,
        [{"name": "auto-merge-policy", "conclusion": "failure"}],
    )

    assert calls == [(7, 42, 0)]
    assert result is expected


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


def test_legacy_open_pr_sweep_contains_valid_prs_after_a_malformed_row() -> None:
    """An invalid row cannot prevent containment of later valid PRs."""
    deferred: list[int] = []
    coordinator = _coordinator(
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN"},
    )

    def defer_auto_merge(pr_number: int) -> bool:
        deferred.append(pr_number)
        return True

    with patch.object(coordinator, "defer_auto_merge", side_effect=defer_auto_merge):
        with pytest.raises(RuntimeError, match="invalid PR number"):
            coordinator.arm_all_unarmed_open_prs([{"number": None}, {"number": 42}])

    assert deferred == [42]


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


def test_legacy_scoped_final_sweep_contains_all_when_head_identity_is_incomplete() -> None:
    """A blank scoped head makes the target scope untrustworthy, so contain every row."""
    remaining = [
        {"number": 42, "headRefName": ""},
        {"number": 43, "headRefName": "other-head"},
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


def test_legacy_scoped_final_sweep_contains_all_when_sibling_head_is_incomplete() -> None:
    """An unscoped blank head may be a sibling, so it cannot be filtered away."""
    remaining = [
        {"number": 42, "headRefName": "2054-auto-impl"},
        {"number": 43, "headRefName": ""},
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


def test_legacy_discovery_error_still_runs_final_containment() -> None:
    """A discovery exception cannot bypass the final whole-repository sweep."""
    remaining = [{"number": 42, "headRefName": "2054-auto-impl"}]
    contained: list[dict[str, Any]] = []

    def contain(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contained.extend(prs)
        return prs

    def discovery_error(_issues: list[int]) -> SimpleNamespace:
        raise RuntimeError("malformed discovery response")

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
            discover_workset=discovery_error,
            list_open_prs_remaining=lambda: remaining,
        ),
        check_inspector=SimpleNamespace(),
        fix_flow=SimpleNamespace(),
        auto_merge=SimpleNamespace(arm_all_unarmed_open_prs=contain),
        arming=SimpleNamespace(sweep_orphaned_records=lambda: None),
        set_shared_pr_issues=lambda _shared: None,
    )

    with pytest.raises(RuntimeError, match="malformed discovery response"):
        coordinator.run()
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
        options_provider=SimpleNamespace,
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
    assert result.error == "merge_wait_unavailable"


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
    assert result.error == "merge_wait_unavailable"


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


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], CiConclusion.NO_CHECKS),
        ([{"status": "queued", "required": True}], CiConclusion.PENDING),
        (
            [{"status": "completed", "conclusion": "success", "required": True}],
            CiConclusion.GREEN,
        ),
        (
            [{"status": "completed", "conclusion": "failure", "required": True}],
            CiConclusion.FAILING,
        ),
        (
            [
                {"status": "queued", "required": False},
                {"status": "completed", "conclusion": "neutral", "required": True},
            ],
            CiConclusion.GREEN,
        ),
    ],
)
def test_ci_state_classification_matches_required_check_contract(
    checks: list[dict[str, Any]], expected: CiConclusion
) -> None:
    """Required-check states map to one stable aggregate conclusion."""
    assert classify_ci_state(checks) is expected


@pytest.mark.parametrize(
    ("state", "failing", "pending", "expected"),
    [
        ({"state": "MERGED"}, [], [], PrMergeState.MERGED),
        ({"state": "CLOSED"}, [], [], PrMergeState.CLOSED),
        ({"state": "OPEN"}, ["lint"], [], PrMergeState.FAILING),
        (
            {"state": "OPEN", "mergeStateStatus": "CONFLICTING"},
            [],
            [],
            PrMergeState.DIRTY,
        ),
        (
            {"state": "OPEN", "mergeStateStatus": "BLOCKED"},
            [],
            [],
            PrMergeState.BLOCKED,
        ),
        ({"state": "OPEN"}, [], ["tests"], PrMergeState.PENDING),
        (None, [], [], PrMergeState.PENDING),
    ],
)
def test_pr_merge_state_classification_is_total(
    state: dict[str, Any] | None,
    failing: list[str],
    pending: list[str],
    expected: PrMergeState,
) -> None:
    """Every observable PR state maps to a drive-green state."""
    assert classify_pr_merge_state(state, failing, pending) is expected


class _RunStatus:
    """Deterministic worker-slot and status-update fake."""

    def __init__(self, slot: int | None = 0) -> None:
        self.slot_id = slot
        self.updates: list[tuple[int, str]] = []

    @contextmanager
    def slot(self):
        yield self.slot_id

    def update_slot(self, slot: int, message: str) -> None:
        self.updates.append((slot, message))


def _run_coordinator(**overrides: Any) -> CIDriveRunCoordinator:
    options = overrides.pop(
        "options",
        SimpleNamespace(
            dry_run=False,
            issues=[7],
            prs=[],
            include_bot_prs=False,
            max_workers=2,
            max_fix_iterations=2,
            poll_max_wait=5,
        ),
    )
    return CIDriveRunCoordinator(
        options_provider=lambda: options,
        worktree_manager=overrides.pop(
            "worktrees", SimpleNamespace(cleanup_all=lambda: None, preserved=[])
        ),
        status_tracker=overrides.pop("status", _RunStatus()),
        discovery=overrides.pop("discovery", SimpleNamespace()),
        check_inspector=overrides.pop("checks", SimpleNamespace()),
        fix_flow=overrides.pop("fix", SimpleNamespace()),
        auto_merge=overrides.pop("auto", SimpleNamespace()),
        arming=overrides.pop("arming", SimpleNamespace()),
        set_shared_pr_issues=overrides.pop("set_shared", lambda _shared: None),
    )


def test_drive_issue_reports_slot_exhaustion_and_unexpected_failure() -> None:
    """Worker admission and unexpected errors become explicit failed results."""
    no_slot = _run_coordinator(status=_RunStatus(None))
    assert no_slot.drive_issue(7, 42, 0).error == "Failed to acquire worker slot"

    exploding = _run_coordinator(
        status=_RunStatus(),
        auto=SimpleNamespace(
            defer_auto_merge=lambda _pr: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
    )
    assert exploding.drive_issue(7, 42, 0).error == "boom"


def test_poll_ci_handles_absent_concluded_and_timed_out_checks() -> None:
    """CI polling distinguishes absent, concluded, and timed-out checks."""
    empty = _run_coordinator(
        checks=SimpleNamespace(gh_pr_checks=lambda _pr, _dry: []),
        status=_RunStatus(),
    )
    assert empty.poll_ci_until_concluded(7, 42, 0, 5) is None

    completed = [{"status": "completed", "conclusion": "failure", "required": True}]
    concluded = _run_coordinator(
        checks=SimpleNamespace(gh_pr_checks=lambda _pr, _dry: completed),
        status=_RunStatus(),
    )
    assert concluded.poll_ci_until_concluded(7, 42, 0, 5) == (completed, completed)

    status = _RunStatus()
    pending = _run_coordinator(
        checks=SimpleNamespace(
            gh_pr_checks=lambda _pr, _dry: [{"status": "queued", "required": True}]
        ),
        status=status,
    )
    with patch("hephaestus.automation.ci_run_coordinator.time.sleep"):
        assert pending.poll_ci_until_concluded(7, 42, 0, 1) is None
    assert len(status.updates) == 1
    assert status.updates[0][0] == 0
    assert "waiting for CI checks" in status.updates[0][1]


def test_poll_ci_retries_until_required_checks_conclude() -> None:
    """Pending checks are polled until their required conclusion arrives."""
    responses = iter(
        [
            [{"status": "queued", "required": True}],
            [{"status": "completed", "conclusion": "success", "required": True}],
        ]
    )
    coordinator = _run_coordinator(
        checks=SimpleNamespace(gh_pr_checks=lambda _pr, _dry: next(responses)),
        status=_RunStatus(),
    )
    with patch("hephaestus.automation.ci_run_coordinator.time.sleep"):
        result = coordinator.poll_ci_until_concluded(7, 42, 0, 5)
    assert result is not None and result[1][0]["conclusion"] == "success"


def test_handle_failing_pr_covers_no_failure_and_fix_outcomes() -> None:
    """Failure handling preserves no-op, explicit, and exhausted outcomes."""
    no_failure = _run_coordinator()
    assert no_failure.handle_failing_pr(7, 42, 0, [{"conclusion": "cancelled"}]).success

    failed = WorkerResult(issue_number=7, success=False, pr_number=42, error="cannot fix")
    coordinator = _run_coordinator(
        fix=SimpleNamespace(attempt_ci_fixes=lambda *_args: failed),
    )
    assert coordinator.handle_failing_pr(7, 42, 0, [{"conclusion": "failure"}]) is failed

    coordinator = _run_coordinator(
        fix=SimpleNamespace(attempt_ci_fixes=lambda *_args: None),
    )
    result = coordinator.handle_failing_pr(7, 42, 0, [{"conclusion": "failure"}])
    assert result.error is not None and "2 attempt" in result.error


def test_handle_successful_fix_prefers_recheck_result() -> None:
    """A post-fix recheck result supersedes the optimistic fix result."""
    fixed = WorkerResult(issue_number=7, success=True, pr_number=42)
    coordinator = _run_coordinator(
        fix=SimpleNamespace(attempt_ci_fixes=lambda *_args: fixed),
    )
    replacement = WorkerResult(issue_number=7, success=False, pr_number=42, error="recheck")
    with patch.object(coordinator, "recheck_and_arm_after_fix", return_value=replacement):
        assert coordinator.handle_failing_pr(7, 42, 0, [{"conclusion": "failure"}]) is replacement


def test_recheck_after_fix_routes_dry_pending_failed_and_missing_go() -> None:
    """Recheck routing respects dry-run, CI, and implementation authority."""
    dry = _run_coordinator(options=SimpleNamespace(dry_run=True))
    assert dry.recheck_and_arm_after_fix(7, 42, 0) is None

    base_options = SimpleNamespace(dry_run=False, poll_max_wait=0)
    pending = _run_coordinator(
        options=base_options,
        checks=SimpleNamespace(gh_pr_checks=lambda *_args: []),
        status=_RunStatus(),
    )
    assert pending.recheck_and_arm_after_fix(7, 42, 0) is None

    failing = _run_coordinator(
        options=SimpleNamespace(dry_run=False, poll_max_wait=1),
        checks=SimpleNamespace(
            gh_pr_checks=lambda *_args: [
                {"status": "completed", "conclusion": "failure", "required": True}
            ]
        ),
        status=_RunStatus(),
    )
    assert failing.recheck_and_arm_after_fix(7, 42, 0) is None

    no_go = _run_coordinator(
        options=SimpleNamespace(dry_run=False, poll_max_wait=1),
        checks=SimpleNamespace(
            gh_pr_checks=lambda *_args: [
                {"status": "completed", "conclusion": "success", "required": True}
            ]
        ),
        status=_RunStatus(),
        auto=SimpleNamespace(pr_has_implementation_go=lambda _pr: False),
    )
    result = no_go.recheck_and_arm_after_fix(7, 42, 0)
    assert result is not None and result.success


def test_recheck_after_fix_arms_waits_and_resolves_dirty() -> None:
    """A green authorized head is armed before conflict resolution is routed."""
    recorded: list[tuple[int, str, str]] = []
    auto = SimpleNamespace(
        pr_has_implementation_go=lambda _pr: True,
        enable_auto_merge=lambda *_args, **_kwargs: True,
        _gh_pr_state=lambda _pr: {"headRefOid": "a" * 40},
        _get_pr_branch=lambda _pr: "feature",
        wait_for_pr_terminal=lambda *_args: "DIRTY",
        resolve_dirty_pr=lambda issue, pr, slot: WorkerResult(
            issue_number=issue, success=False, pr_number=pr, error=f"dirty-{slot}"
        ),
    )
    coordinator = _run_coordinator(
        options=SimpleNamespace(dry_run=False, poll_max_wait=1),
        checks=SimpleNamespace(
            gh_pr_checks=lambda *_args: [
                {"status": "completed", "conclusion": "success", "required": True}
            ]
        ),
        status=_RunStatus(),
        discovery=SimpleNamespace(is_bot_pr_mode=lambda *_args: False),
        auto=auto,
        arming=SimpleNamespace(
            record_arming=lambda pr, branch, sha: recorded.append((pr, branch, sha))
        ),
    )
    result = coordinator.recheck_and_arm_after_fix(7, 42, 3)
    assert result is not None
    assert result.error == "dirty-3"
    assert recorded == [(42, "feature", "a" * 40)]


def test_recheck_after_fix_reports_auto_merge_failure() -> None:
    """A failed legacy auto-merge request is returned as an explicit error."""
    coordinator = _run_coordinator(
        options=SimpleNamespace(dry_run=False, poll_max_wait=1),
        checks=SimpleNamespace(
            gh_pr_checks=lambda *_args: [
                {"status": "completed", "conclusion": "success", "required": True}
            ]
        ),
        status=_RunStatus(),
        discovery=SimpleNamespace(is_bot_pr_mode=lambda *_args: False),
        auto=SimpleNamespace(
            pr_has_implementation_go=lambda _pr: True,
            enable_auto_merge=lambda *_args, **_kwargs: False,
        ),
    )
    result = coordinator.recheck_and_arm_after_fix(7, 42, 0)
    assert result is not None and result.error is not None
    assert "auto-merge failed" in result.error


def test_post_fix_poll_waits_then_returns_and_times_out() -> None:
    """Post-fix polling returns completion and bounds persistent pending work."""
    responses = iter(
        [
            [{"status": "queued", "required": True}],
            [{"status": "completed", "conclusion": "neutral", "required": True}],
        ]
    )
    status = _RunStatus()
    with patch("hephaestus.automation.ci_run_coordinator.time.sleep"):
        checks = _poll_post_fix_required(7, 42, 0, 5, False, lambda *_args: next(responses), status)
    assert checks and checks[0]["conclusion"] == "neutral"
    assert status.updates

    with patch("hephaestus.automation.ci_run_coordinator.time.sleep"):
        assert (
            _poll_post_fix_required(
                7,
                42,
                0,
                0,
                False,
                lambda *_args: [{"status": "queued", "required": True}],
                _RunStatus(),
            )
            is None
        )


def test_containment_helper_preserves_empty_and_delegates_nonempty() -> None:
    """Final containment is a no-op only when no open PRs remain."""
    auto = SimpleNamespace(arm_all_unarmed_open_prs=lambda rows: [*rows, {"number": 2}])
    assert _contain_remaining_prs(auto, []) == []
    assert _contain_remaining_prs(auto, [{"number": 1}]) == [
        {"number": 1},
        {"number": 2},
    ]


def test_run_drives_discovered_map_cleans_worktrees_and_records_shared_issues() -> None:
    """The run lifecycle discovers, drives, records, and cleans all work."""
    shared: list[dict[int, list[int]]] = []
    cleanup: list[bool] = []
    options = SimpleNamespace(
        dry_run=False,
        issues=[7, 8],
        prs=[],
        include_bot_prs=False,
        max_workers=2,
    )
    coordinator = _run_coordinator(
        options=options,
        worktrees=SimpleNamespace(
            cleanup_all=lambda: cleanup.append(True),
            preserved=[(8, "/tmp/preserved")],
        ),
        discovery=SimpleNamespace(
            discover_workset=lambda _issues: SimpleNamespace(
                pr_map={7: 42, 8: 43}, shared_pr_issues={42: [7], 43: [8]}
            ),
            list_open_prs_remaining=lambda: [],
        ),
        arming=SimpleNamespace(sweep_orphaned_records=lambda: None),
        set_shared=lambda value: shared.append(value),
    )

    def drive(issue: int, pr: int, _slot: int) -> WorkerResult:
        return WorkerResult(
            issue_number=issue,
            pr_number=pr,
            success=issue == 7,
            error=None if issue == 7 else "failed",
        )

    with patch.object(coordinator, "drive_issue", side_effect=drive):
        results = coordinator.run()

    assert results[7].success is True
    assert results[8].success is False
    assert cleanup == [True]
    assert shared == [{42: [7], 43: [8]}]
    assert coordinator.open_prs_remaining == []


def test_drive_map_converts_worker_exception_to_failure_result() -> None:
    """One worker exception is isolated as that issue's failed result."""
    coordinator = _run_coordinator(options=SimpleNamespace(max_workers=1))

    def explode(issue: int, _pr: int, _slot: int) -> WorkerResult:
        if issue == 8:
            raise RuntimeError("worker exploded")
        return WorkerResult(issue_number=issue, success=True)

    with patch.object(coordinator, "drive_issue", side_effect=explode):
        results = coordinator._drive_pr_map({7: 42, 8: 43})

    assert results[7].success is True
    assert results[8].success is False
    assert results[8].error == "worker exploded"


def test_run_cleanup_failure_is_nonfatal_and_dry_final_sweep_is_empty() -> None:
    """Cleanup failure is reported without erasing results or dry-run safety."""
    options = SimpleNamespace(
        dry_run=False,
        issues=[7],
        prs=[],
        include_bot_prs=False,
        max_workers=1,
    )
    coordinator = _run_coordinator(
        options=options,
        worktrees=SimpleNamespace(
            cleanup_all=lambda: (_ for _ in ()).throw(RuntimeError("cleanup")),
            preserved=[],
        ),
        discovery=SimpleNamespace(
            discover_workset=lambda _issues: SimpleNamespace(
                pr_map={7: 42}, shared_pr_issues={42: [7]}
            ),
            list_open_prs_remaining=lambda: [],
        ),
        arming=SimpleNamespace(sweep_orphaned_records=lambda: None),
    )
    with patch.object(
        coordinator,
        "drive_issue",
        side_effect=lambda issue, pr, _slot: WorkerResult(
            issue_number=issue, pr_number=pr, success=True
        ),
    ):
        assert coordinator.run()[7].success is True

    dry = _run_coordinator(options=SimpleNamespace(dry_run=True, issues=[]))
    assert dry._final_open_prs({}) == []


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
    """The retired compatibility entry reports unavailable merge waiting, never success."""
    coordinator = _coordinator(
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        lambda _pr_number: {"state": "OPEN"},
        dry_run=True,
    )

    result = coordinator.arm_and_wait_for_merge(issue_number=7, pr_number=42, acquired_slot=0)

    assert result.success is False
    assert result.error == "merge_wait_unavailable"


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
    assert result.error == f"auto-merge containment failed for PR {pr_ref(42)}"


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
