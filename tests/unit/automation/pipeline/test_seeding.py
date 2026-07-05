"""GitHub-journal seeding: classification of issues into stage queues.

Tests the classifier that maps GitHub state (labels, PR, epic) → entry queue.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hephaestus.automation.pipeline.seeding import (
    IssueFacts,
    classify_issue,
    seed_issue,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
)


class TestClassifyIssue:
    """Classifier routing matrix: GitHub state → entry queue."""

    def test_skip_label_excluded(self) -> None:
        """Issue tagged state:skip is excluded (finished)."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_SKIP},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "finished"
        assert "state:skip" in reason

    def test_epic_excluded(self) -> None:
        """Issues marked as epics are excluded (finished)."""
        facts = IssueFacts(
            number=1,
            is_epic=True,
            labels=set(),
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "finished"
        assert "epic" in reason

    def test_pr_merged_finished(self) -> None:
        """Merged PR is idempotent (finished)."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_IMPLEMENTATION_GO},
            pr_number=42,
            pr_is_open=False,
            pr_is_merged=True,
        )
        queue, reason = classify_issue(facts)
        assert queue == "finished"
        assert "merged" in reason

    def test_open_pr_with_impl_go_routes_to_ci(self) -> None:
        """Open PR + state:implementation-go → ready for CI."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_IMPLEMENTATION_GO},
            pr_number=42,
            pr_is_open=True,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "ci"
        assert "implementation-go" in reason or "impl" in reason

    def test_open_pr_without_impl_go_routes_to_pr_review(self) -> None:
        """Open PR without implementation-go awaits PR review."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_PLAN_GO},
            pr_number=42,
            pr_is_open=True,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "pr_review"
        assert "review" in reason

    def test_no_pr_at_plan_go_routes_to_implementation(self) -> None:
        """No PR, at-or-past state:plan-go → ready for implementation."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_PLAN_GO},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "implementation"
        assert "at-or-past" in reason or "plan-go" in reason

    def test_no_pr_past_plan_go_routes_to_implementation(self) -> None:
        """No PR, past state:plan-go (e.g., impl-no-go) → implementation (at-or-past)."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_IMPLEMENTATION_NO_GO},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, _reason = classify_issue(facts)
        assert queue == "implementation"

    def test_no_pr_plan_no_go_routes_to_planning(self) -> None:
        """No PR, state:plan-no-go → planning (amend path)."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_PLAN_NO_GO},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, reason = classify_issue(facts)
        assert queue == "planning"
        assert "no-go" in reason or "amend" in reason

    def test_needs_plan_routes_to_planning(self) -> None:
        """state:needs-plan → planning."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_NEEDS_PLAN},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, _reason = classify_issue(facts)
        assert queue == "planning"

    def test_no_label_defaults_to_planning(self) -> None:
        """No state label → planning (needs-plan by default)."""
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels=set(),
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, _reason = classify_issue(facts)
        assert queue == "planning"

    def test_closed_pr_normalized_to_none(self) -> None:
        """Closed PR (neither open nor merged) with plan-no-go → planning (no PR path)."""
        # This tests the normalization: if PR is closed, seed_issue should have set
        # pr_number = None. But in the test, we construct IssueFacts directly.
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_PLAN_NO_GO},
            pr_number=None,  # Normalized by seed_issue
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, _reason = classify_issue(facts)
        assert queue == "planning"


class TestSeedIssue:
    """Fetch and normalize GitHub state for a single issue."""

    @patch("hephaestus.automation.pipeline.seeding.fetch_issue_info")
    @patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue")
    def test_seed_issue_basic(self, mock_find_pr, mock_fetch) -> None:
        """seed_issue fetches and normalizes GitHub state."""
        mock_fetch.return_value = MagicMock(
            number=101,
            labels=[STATE_PLAN_GO, "other-label"],
        )
        mock_find_pr.return_value = None

        facts = seed_issue(101)

        assert facts.number == 101
        assert STATE_PLAN_GO in facts.labels
        assert facts.pr_number is None
        assert facts.pr_is_open is False
        assert facts.pr_is_merged is False

    @patch("hephaestus.automation.pipeline.seeding.fetch_issue_info")
    @patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue")
    def test_seed_issue_with_open_pr(self, mock_find_pr, mock_fetch) -> None:
        """seed_issue finds and records open PR."""
        mock_fetch.return_value = MagicMock(
            number=102,
            labels=[STATE_PLAN_GO],
        )
        mock_find_pr.return_value = 42

        facts = seed_issue(102)

        assert facts.pr_number == 42
        assert facts.pr_is_open is True
        assert facts.pr_is_merged is False

    @patch("hephaestus.automation.pipeline.seeding.fetch_issue_info")
    @patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue")
    def test_seed_issue_epic_detection(self, mock_find_pr, mock_fetch) -> None:
        """seed_issue detects epic label."""
        mock_fetch.return_value = MagicMock(
            number=103,
            labels=["epic", STATE_NEEDS_PLAN],
        )
        mock_find_pr.return_value = None

        facts = seed_issue(103)

        assert facts.is_epic is True

    @patch("hephaestus.automation.pipeline.seeding.fetch_issue_info")
    @patch("hephaestus.automation.pipeline.seeding.find_pr_for_issue")
    def test_seed_issue_pr_fetch_failure_handled(self, mock_find_pr, mock_fetch) -> None:
        """seed_issue handles PR lookup failures gracefully (fail-open)."""
        mock_fetch.return_value = MagicMock(
            number=104,
            labels=[STATE_PLAN_GO],
        )
        mock_find_pr.side_effect = RuntimeError("API error")

        facts = seed_issue(104)

        assert facts.pr_number is None  # Fail-open
        assert facts.pr_is_open is False


class TestLabelRank:
    """At-or-past label rank comparisons prevent re-queueing."""

    def test_issue_already_past_plan_go_not_requeued_to_planning(self) -> None:
        """AC: issue with state:implementation-go is NOT re-routed to planning."""
        # This is the critical AC from the plan: "==` strands items already past the target"
        # Our fix: use at-or-past (>=) not equality (==).
        facts = IssueFacts(
            number=1,
            is_epic=False,
            labels={STATE_IMPLEMENTATION_GO},
            pr_number=None,
            pr_is_open=False,
            pr_is_merged=False,
        )
        queue, _ = classify_issue(facts)
        # If we mistakenly routed to planning, this would fail.
        assert queue == "implementation"
