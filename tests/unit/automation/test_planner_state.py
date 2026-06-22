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
# filter — drops state:plan-go issues via batched labels (no per-issue gh view)
# ---------------------------------------------------------------------------


class TestFilterDropsPlanGoIssues:
    """``filter()`` skips already-GO issues using one batched label fetch.

    Previously the planner re-evaluated every open issue every loop with a
    per-issue ``gh issue view`` (via ``is_plan_review_go``); ``state:plan-go``
    issues are now dropped up front from one aliased GraphQL call.
    """

    def test_plan_go_issue_dropped_no_per_issue_gh_view(self) -> None:
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10, 11, 12])
        opts.skip_closed = False
        mgr = PlannerStateManager(opts)

        labels = {
            10: [STATE_PLAN_GO],  # already planned → dropped
            11: ["state:plan-no-go"],  # not GO → kept for re-plan
            12: [],  # unlabeled → kept for re-plan
        }
        with (
            patch(
                "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
                return_value=labels,
            ),
            patch("hephaestus.automation.planner_state._gh_call") as mock_gh,
        ):
            kept = mgr.filter()

        assert kept == [11, 12]
        # No per-issue gh issue view: only the batched label fetch was used.
        mock_gh.assert_not_called()

    def test_force_replans_even_plan_go_issues(self) -> None:
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10])
        opts.skip_closed = False
        opts.force = True
        mgr = PlannerStateManager(opts)

        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
            return_value={10: [STATE_PLAN_GO]},
        ):
            kept = mgr.filter()

        assert kept == [10]  # force overrides the plan-go skip

    def test_cached_labels_served_to_callers(self) -> None:
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10, 11])
        opts.skip_closed = False
        mgr = PlannerStateManager(opts)

        with patch(
            "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
            return_value={10: [STATE_PLAN_GO], 11: ["bug"]},
        ):
            mgr.filter()

        assert mgr.get_cached_labels(11) == ["bug"]
        # Unfetched issue → empty list (cache populated but issue absent).
        assert mgr.get_cached_labels(99) == []

    def test_labels_cache_none_before_filter(self) -> None:
        mgr = PlannerStateManager(_make_options())
        assert mgr.get_cached_labels(1) is None


class TestFilterAllFilteredWarning:
    """``filter()`` warns when an explicit ``--issues`` set fully filters out.

    A run scoped to closed / already-planned issues otherwise no-ops with only
    INFO-level per-issue ``... skipping`` lines, making a mis-scoped run easy to
    miss (the 2026-06-21 ``--issues 123,456,789,101`` all-closed run). The
    warning only fires for explicit sets — auto-discovery legitimately yields an
    empty work set on a converged repo and must stay quiet.
    """

    def test_warns_when_explicit_set_fully_filtered(self, caplog: Any) -> None:
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10, 11])
        opts.skip_closed = False
        opts.issues_explicit = True
        mgr = PlannerStateManager(opts)

        with (
            patch(
                "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
                return_value={10: [STATE_PLAN_GO], 11: [STATE_PLAN_GO]},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.planner_state"),
        ):
            kept = mgr.filter()

        assert kept == []
        assert any("filtered out" in r.message and r.levelname == "WARNING" for r in caplog.records)

    def test_no_warning_when_explicit_set_keeps_issues(self, caplog: Any) -> None:
        opts = _make_options(issues=[10, 11])
        opts.skip_closed = False
        opts.issues_explicit = True
        mgr = PlannerStateManager(opts)

        with (
            patch(
                "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
                return_value={10: [], 11: []},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.planner_state"),
        ):
            kept = mgr.filter()

        assert kept == [10, 11]
        assert not any("filtered out" in r.message for r in caplog.records)

    def test_no_warning_when_discovered_set_fully_filtered(self, caplog: Any) -> None:
        """Auto-discovery (issues_explicit=False) empties quietly — no warning."""
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10, 11])
        opts.skip_closed = False
        opts.issues_explicit = False
        mgr = PlannerStateManager(opts)

        with (
            patch(
                "hephaestus.automation.planner_state.fetch_all_issue_labels_graphql",
                return_value={10: [STATE_PLAN_GO], 11: [STATE_PLAN_GO]},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.planner_state"),
        ):
            kept = mgr.filter()

        assert kept == []
        assert not any("filtered out" in r.message for r in caplog.records)


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
