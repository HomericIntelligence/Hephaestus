"""Unit tests for ``hephaestus.automation.planner_state``.

Covers the batched comment-prefetch path introduced by #616 and the
``has_existing_plan`` fallback behaviour.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import PLAN_COMMENT_MARKER, PlannerOptions
from hephaestus.automation.planner_state import PlannerStateManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_options(issues: list[int] | None = None) -> PlannerOptions:
    return PlannerOptions(
        issues=issues or [1, 2, 3],
        dry_run=False,
        force=False,
        parallel=1,
        system_prompt_file=None,
        skip_closed=False,
        enable_advise=False,
    )


def _plan_body() -> str:
    return f"{PLAN_COMMENT_MARKER}\n\nStep 1: do the thing.\n"


def _other_body() -> str:
    return "Just a regular comment with no plan marker.\n"


def _review_body(verdict: str = "GO") -> str:
    """Build a parseable plan-review body carrying a verdict line."""
    return f"## 🔍 Plan Review\n\nLooks good.\n\nGrade: A\nVerdict: {verdict}\n"


def _unparseable_review_body() -> str:
    """Build a plan-review-prefixed body with no parseable ``Verdict:`` line.

    Mirrors the pre-verdict-contract review comments observed across the org
    (320 ``no parseable Verdict`` warnings during the 2026-05-29 org run).
    """
    return "## 🔍 Plan Review\n\nThe plan looks fine — implement it.\n"


# ---------------------------------------------------------------------------
# prefetch_comments / get_cached_comments
# ---------------------------------------------------------------------------


class TestPrefetchComments:
    """Batch comment cache wiring (#616)."""

    @pytest.fixture(autouse=True)
    def _patch_repo(self) -> Any:
        with (
            patch(
                "hephaestus.automation.review_state.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.review_state.get_repo_info",
                return_value=("owner", "repo"),
            ),
        ):
            yield

    def test_cache_starts_as_none(self) -> None:
        mgr = PlannerStateManager(_make_options())
        assert mgr._comments_cache is None

    def test_get_cached_returns_none_before_prefetch(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[10]))
        assert mgr.get_cached_comments(10) is None

    def test_prefetch_empty_list_sets_empty_cache(self) -> None:
        mgr = PlannerStateManager(_make_options())
        mgr.prefetch_comments([])
        assert mgr._comments_cache == {}

    def test_prefetch_populates_cache(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[11, 12]))
        expected = {
            11: [{"body": _plan_body(), "updatedAt": "2025-01-01", "url": "u1"}],
            12: [{"body": _other_body(), "updatedAt": "2025-01-01", "url": "u2"}],
        }
        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_comments_graphql",
            return_value=expected,
        ):
            mgr.prefetch_comments([11, 12])
        assert mgr._comments_cache == expected
        assert mgr.get_cached_comments(11) == expected[11]
        assert mgr.get_cached_comments(12) == expected[12]

    def test_get_cached_returns_empty_list_for_missing_key(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[20]))
        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_comments_graphql",
            return_value={},
        ):
            mgr.prefetch_comments([20])
        # Issue 20 not in the returned map → get_cached returns []
        assert mgr.get_cached_comments(20) == []


# ---------------------------------------------------------------------------
# has_existing_plan — cached path
# ---------------------------------------------------------------------------


class TestHasExistingPlanCached:
    """has_existing_plan uses the cache when prefetch_comments was called."""

    @pytest.fixture(autouse=True)
    def _patch_repo(self) -> Any:
        with (
            patch(
                "hephaestus.automation.review_state.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.review_state.get_repo_info",
                return_value=("owner", "repo"),
            ),
        ):
            yield

    def _mgr_with_cache(self, cache: dict[int, list[dict[str, Any]]]) -> PlannerStateManager:
        mgr = PlannerStateManager(_make_options(issues=list(cache.keys())))
        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_comments_graphql",
            return_value=cache,
        ):
            mgr.prefetch_comments(list(cache.keys()))
        return mgr

    def test_returns_true_when_plan_marker_in_cache(self) -> None:
        mgr = self._mgr_with_cache({31: [{"body": _plan_body()}, {"body": _other_body()}]})
        assert mgr.has_existing_plan(31) is True

    def test_returns_false_when_no_plan_marker_in_cache(self) -> None:
        mgr = self._mgr_with_cache({32: [{"body": _other_body()}]})
        assert mgr.has_existing_plan(32) is False

    def test_returns_false_when_cache_empty_for_issue(self) -> None:
        mgr = self._mgr_with_cache({33: []})
        assert mgr.has_existing_plan(33) is False

    def test_does_not_call_gh_cli_when_cache_hit(self) -> None:
        mgr = self._mgr_with_cache({34: [{"body": _plan_body()}]})
        with patch("hephaestus.automation.planner_state._gh_call") as mock_gh:
            mgr.has_existing_plan(34)
        mock_gh.assert_not_called()


# ---------------------------------------------------------------------------
# has_existing_plan — fallback (no cache)
# ---------------------------------------------------------------------------


class TestHasExistingPlanFallback:
    """has_existing_plan falls back to individual gh CLI call when no cache."""

    def _gh_comments_payload(self, bodies: list[str]) -> MagicMock:
        mock = MagicMock()
        mock.stdout = json.dumps({"comments": [{"body": b} for b in bodies]})
        return mock

    def test_returns_true_via_gh_cli_when_plan_present(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[41]))
        mock_result = self._gh_comments_payload([_other_body(), _plan_body()])
        with patch("hephaestus.automation.planner_state._gh_call", return_value=mock_result):
            assert mgr.has_existing_plan(41) is True

    def test_returns_false_via_gh_cli_when_no_plan(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[42]))
        mock_result = self._gh_comments_payload([_other_body()])
        with patch("hephaestus.automation.planner_state._gh_call", return_value=mock_result):
            assert mgr.has_existing_plan(42) is False

    def test_returns_false_on_gh_call_exception(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[43]))
        with patch(
            "hephaestus.automation.planner_state._gh_call",
            side_effect=RuntimeError("network error"),
        ):
            assert mgr.has_existing_plan(43) is False


# ---------------------------------------------------------------------------
# has_usable_plan — self-heals stale unparseable plan-review comments (#702)
# ---------------------------------------------------------------------------


class TestHasUsablePlan:
    """has_usable_plan = plan comment present AND latest plan-review parseable.

    Background (#702): the org-wide 2026-05-29 loop run logged 320
    "no parseable Verdict" warnings. Those issues had a plan comment AND a
    plan-review comment, but the review predated the Verdict: GO/NOGO contract
    so the implementer's GO-gate stayed False forever — the loop never
    re-planned them because ``has_existing_plan`` only checked for the plan
    comment, not whether its review was usable.

    ``has_usable_plan`` returns True iff BOTH the plan comment exists AND the
    latest plan-review comment carries a parseable verdict. When the review
    is unparseable, the next loop will re-plan (and re-review) the issue,
    self-healing without manual cleanup.
    """

    @pytest.fixture(autouse=True)
    def _patch_repo(self) -> Any:
        with (
            patch(
                "hephaestus.automation.review_state.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.review_state.get_repo_info",
                return_value=("owner", "repo"),
            ),
        ):
            yield

    def _mgr_with_cache(self, cache: dict[int, list[dict[str, Any]]]) -> PlannerStateManager:
        mgr = PlannerStateManager(_make_options(issues=list(cache.keys())))
        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_comments_graphql",
            return_value=cache,
        ):
            mgr.prefetch_comments(list(cache.keys()))
        return mgr

    def test_returns_true_when_plan_and_parseable_go_review_present(self) -> None:
        """Healthy state: plan + GO-verdict review → usable."""
        mgr = self._mgr_with_cache({51: [{"body": _plan_body()}, {"body": _review_body("GO")}]})
        assert mgr.has_usable_plan(51) is True

    def test_returns_true_when_plan_and_parseable_nogo_review_present(self) -> None:
        """A parseable NOGO is still 'usable' for the gate — the implementer reads it.

        The intent of has_usable_plan is purely "is the review parseable?" — a
        NOGO is a real reviewer signal, not a stale-comment defect. The
        implementer's existing NOGO-defer logic handles it downstream.
        """
        mgr = self._mgr_with_cache({52: [{"body": _plan_body()}, {"body": _review_body("NOGO")}]})
        assert mgr.has_usable_plan(52) is True

    def test_returns_false_when_plan_present_but_review_unparseable(self) -> None:
        """The #702 case: plan + verdict-less review → NOT usable → re-plan next loop."""
        mgr = self._mgr_with_cache(
            {53: [{"body": _plan_body()}, {"body": _unparseable_review_body()}]}
        )
        assert mgr.has_usable_plan(53) is False

    def test_returns_false_when_no_plan_comment(self) -> None:
        """No plan at all → not usable (delegates to existing existence semantics)."""
        mgr = self._mgr_with_cache({54: [{"body": _other_body()}]})
        assert mgr.has_usable_plan(54) is False

    def test_returns_true_when_plan_present_and_no_review_yet(self) -> None:
        """A plan with no review yet is 'usable' — the review will run this loop.

        This preserves the prior has_existing_plan semantics for the
        no-review-yet case so the loop's normal "plan posted, awaiting review"
        flow is unaffected.
        """
        mgr = self._mgr_with_cache({55: [{"body": _plan_body()}]})
        assert mgr.has_usable_plan(55) is True

    def test_latest_review_wins_when_multiple_reviews_present(self) -> None:
        """Most-recent review's parseability decides — older unparseable review is ignored."""
        mgr = self._mgr_with_cache(
            {
                56: [
                    {"body": _plan_body()},
                    {"body": _unparseable_review_body()},  # older, stale
                    {"body": _review_body("GO")},  # newer, valid
                ]
            }
        )
        assert mgr.has_usable_plan(56) is True
