"""Pure-function classifiers for CI and merge-wait pipeline stages (issue #1816)."""

from __future__ import annotations

from hephaestus.automation.ci_run_coordinator import (
    CiConclusion,
    PrMergeState,
    classify_ci_state,
    classify_pr_merge_state,
)


class TestClassifyCiState:
    """Test classify_ci_state pure classifier."""

    def test_no_checks_empty_list(self) -> None:
        """Empty check list → NO_CHECKS."""
        assert classify_ci_state([]) is CiConclusion.NO_CHECKS

    def test_green_all_success(self) -> None:
        """All checks with success conclusion → GREEN."""
        checks = [
            {"status": "completed", "conclusion": "success", "required": True},
            {"status": "completed", "conclusion": "success", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.GREEN

    def test_green_skipped_neutral(self) -> None:
        """Skipped and neutral conclusions → GREEN."""
        checks = [
            {"status": "completed", "conclusion": "skipped", "required": True},
            {"status": "completed", "conclusion": "neutral", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.GREEN

    def test_failing_any_failure(self) -> None:
        """Any required check with failure conclusion → FAILING."""
        checks = [
            {"status": "completed", "conclusion": "success", "required": True},
            {"status": "completed", "conclusion": "failure", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.FAILING

    def test_pending_not_completed(self) -> None:
        """At least one required check not completed → PENDING."""
        checks = [
            {"status": "in_progress", "conclusion": None, "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.PENDING

    def test_pending_queued(self) -> None:
        """Queued status → PENDING."""
        checks = [
            {"status": "queued", "conclusion": None, "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.PENDING

    def test_required_false_ignored(self) -> None:
        """Checks with required=False are ignored; uses all if no required=True."""
        checks = [
            {"status": "in_progress", "conclusion": None, "required": False},
        ]
        # Falls back to all checks (required=False, so all is treated as required)
        assert classify_ci_state(checks) is CiConclusion.PENDING

    def test_required_true_subset(self) -> None:
        """Only required=True checks count; others are ignored."""
        checks: list[dict[str, object]] = [
            {"status": "in_progress", "conclusion": None, "required": False},
            {"status": "completed", "conclusion": "success", "required": True},
        ]
        # Should look at required=True only and return GREEN
        assert classify_ci_state(checks) is CiConclusion.GREEN

    def test_no_required_field_defaults_to_false(self) -> None:
        """Missing required field defaults to False."""
        checks = [
            {"status": "completed", "conclusion": "success"},  # no required field
        ]
        # Falls back to all checks
        assert classify_ci_state(checks) is CiConclusion.GREEN

    def test_cancelled_is_failing_not_green(self) -> None:
        """GREEN mirrors legacy all_green EXACTLY: cancelled must never arm.

        Legacy ``_drive_issue`` computed ``all_green = conclusion in
        (success, skipped, neutral)``; a cancelled check therefore never
        took the arm path. The classifier's GREEN must match that set, so
        the residual (cancelled/timed_out/action_required) is FAILING.
        """
        checks = [
            {"status": "completed", "conclusion": "cancelled", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.FAILING

    def test_timed_out_is_failing_not_green(self) -> None:
        """timed_out is outside the legacy all_green set → FAILING."""
        checks = [
            {"status": "completed", "conclusion": "timed_out", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.FAILING

    def test_mixed_success_and_cancelled_is_failing(self) -> None:
        """One non-green conclusion breaks all_green even beside successes."""
        checks = [
            {"status": "completed", "conclusion": "success", "required": True},
            {"status": "completed", "conclusion": "cancelled", "required": True},
        ]
        assert classify_ci_state(checks) is CiConclusion.FAILING


class TestClassifyPrMergeState:
    """Test classify_pr_merge_state pure classifier."""

    def test_merged_state(self) -> None:
        """state=MERGED → MERGED."""
        gh_state = {"state": "MERGED"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.MERGED

    def test_merged_state_lowercase(self) -> None:
        """State lowercase → normalized to MERGED."""
        gh_state = {"state": "merged"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.MERGED

    def test_closed_state(self) -> None:
        """state=CLOSED → CLOSED."""
        gh_state = {"state": "CLOSED"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.CLOSED

    def test_failing_fixable_failing(self) -> None:
        """fixable_failing non-empty → FAILING (even if no merge status)."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "BEHIND"}
        assert (
            classify_pr_merge_state(gh_state, ["policy", "other"], ["policy"], [])
            is PrMergeState.FAILING
        )

    def test_dirty_merge_status(self) -> None:
        """mergeStateStatus=DIRTY → DIRTY."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "DIRTY"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.DIRTY

    def test_conflicting_merge_status(self) -> None:
        """mergeStateStatus=CONFLICTING → DIRTY."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "CONFLICTING"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.DIRTY

    def test_blocked_with_no_failing_pending(self) -> None:
        """mergeStateStatus=BLOCKED + no failing + no pending → BLOCKED."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "BLOCKED"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.BLOCKED

    def test_blocked_with_pending_is_pending(self) -> None:
        """mergeStateStatus=BLOCKED + pending → PENDING (not BLOCKED)."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "BLOCKED"}
        assert classify_pr_merge_state(gh_state, [], [], ["check1"]) is PrMergeState.PENDING

    def test_blocked_with_failing_is_pending(self) -> None:
        """mergeStateStatus=BLOCKED + failing (not fixable) → PENDING."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "BLOCKED"}
        assert classify_pr_merge_state(gh_state, ["check1"], [], []) is PrMergeState.PENDING

    def test_pending_default(self) -> None:
        """No terminal state matched → PENDING."""
        gh_state = {"state": "OPEN", "mergeStateStatus": "BEHIND"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.PENDING

    def test_none_gh_state_is_pending(self) -> None:
        """gh_state=None → PENDING (safe default)."""
        assert classify_pr_merge_state(None, [], [], []) is PrMergeState.PENDING

    def test_gh_state_missing_state_field(self) -> None:
        """gh_state without state field → defaults to empty string → PENDING."""
        gh_state: dict[str, object] = {"mergeStateStatus": "BEHIND"}
        assert classify_pr_merge_state(gh_state, [], [], []) is PrMergeState.PENDING

    def test_fixable_failing_takes_priority_over_dirty(self) -> None:
        """Legacy ordering: a red required check wins over the DIRTY status.

        Migrated from the ``_wait_for_pr_terminal`` scenario table: the
        legacy loop checked failing checks BEFORE mergeStateStatus, so a
        red-and-conflicting PR returns FAILING (drive a fix first).
        """
        gh_state = {"state": "OPEN", "mergeStateStatus": "DIRTY"}
        assert classify_pr_merge_state(gh_state, ["lint"], ["lint"], []) is PrMergeState.FAILING

    def test_policy_only_failure_with_blocked_waits(self) -> None:
        """Migrated legacy scenario: BLOCKED + auto-merge-policy-only waits.

        A stale auto-merge-policy failure right after arming is not
        code-fixable and not a branch-protection block: PENDING (the legacy
        loop kept waiting for the policy check to refresh).
        """
        gh_state = {"state": "OPEN", "mergeStateStatus": "BLOCKED"}
        assert (
            classify_pr_merge_state(gh_state, ["auto-merge-policy"], [], []) is PrMergeState.PENDING
        )
