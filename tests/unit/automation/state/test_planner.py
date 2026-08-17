"""Unit tests for ``hephaestus.automation.state.planner``.

Covers the batched comment-prefetch path introduced by #616 and the
tri-state plan-discovery fallback behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.models import PLAN_COMMENT_MARKER, PlannerOptions
from hephaestus.automation.review_journal import (
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryStatus,
)
from hephaestus.automation.state.planner import PlannerStateManager
from hephaestus.github.client import GitHubRateLimitError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_title_and_skip_patches() -> Any:
    """Default the epic-detection helpers to no-ops (#1669).

    ``PlannerStateManager.filter`` now also batch-fetches titles and may tag
    excluded epics ``state:skip``. Tests that don't care about epics get a
    safe default: no titles (so nothing reads as an epic by title) and a
    no-op skip-tagging call. Tests that DO exercise epic exclusion override
    these with their own ``patch`` context.
    """
    with (
        patch(
            "hephaestus.automation.state.planner.fetch_all_issue_titles_graphql",
            return_value={},
        ),
        patch("hephaestus.automation.state.planner.skip_epics"),
        patch("hephaestus.automation.state.planner.gh_current_login", return_value="bot"),
    ):
        yield


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
                "hephaestus.automation.state.review.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
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
        with patch(
            "hephaestus.automation.state.planner.fetch_issue_comments_metadata",
            side_effect=lambda issue: [
                {
                    "body": _plan_body() if issue == 11 else _other_body(),
                    "user": {"login": "bot"},
                }
            ],
        ):
            mgr.prefetch_comments([11, 12])
        assert mgr.discover_plan(11).status is PlanDiscoveryStatus.FOUND
        assert mgr.discover_plan(12).status is PlanDiscoveryStatus.ABSENT

    def test_missing_cache_key_falls_back_instead_of_inventing_absence(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[20]))
        mgr._comments_cache = {}
        with patch.object(
            mgr,
            "_read_comments",
            return_value=[
                IssueComment(
                    body=_plan_body(),
                    author_login="bot",
                    viewer_did_author=True,
                )
            ],
        ) as read:
            result = mgr.discover_plan(20)

        assert result.status is PlanDiscoveryStatus.FOUND
        read.assert_called_once_with(20)


# ---------------------------------------------------------------------------
# discover_plan — cached path
# ---------------------------------------------------------------------------


class TestHasExistingPlanCached:
    """discover_plan uses the cache when prefetch_comments was called."""

    @pytest.fixture(autouse=True)
    def _patch_repo(self) -> Any:
        with (
            patch(
                "hephaestus.automation.state.review.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "repo"),
            ),
        ):
            yield

    def _mgr_with_cache(self, cache: dict[int, list[IssueComment]]) -> PlannerStateManager:
        mgr = PlannerStateManager(_make_options(issues=list(cache.keys())))
        mgr._comments_cache = cache
        return mgr

    def test_returns_true_when_plan_marker_in_cache(self) -> None:
        mgr = self._mgr_with_cache(
            {
                31: [
                    IssueComment(body=_plan_body(), author_login="bot", viewer_did_author=True),
                    IssueComment(body=_other_body(), author_login="bot", viewer_did_author=True),
                ]
            }
        )
        assert mgr.discover_plan(31).status is PlanDiscoveryStatus.FOUND

    def test_returns_false_when_no_plan_marker_in_cache(self) -> None:
        mgr = self._mgr_with_cache(
            {32: [IssueComment(body=_other_body(), author_login="bot", viewer_did_author=True)]}
        )
        assert mgr.discover_plan(32).status is PlanDiscoveryStatus.ABSENT

    def test_returns_false_when_cache_empty_for_issue(self) -> None:
        mgr = self._mgr_with_cache({33: []})
        assert mgr.discover_plan(33).status is PlanDiscoveryStatus.ABSENT

    def test_does_not_call_gh_cli_when_cache_hit(self) -> None:
        mgr = self._mgr_with_cache(
            {34: [IssueComment(body=_plan_body(), author_login="bot", viewer_did_author=True)]}
        )
        with patch.object(mgr, "_read_comments") as read:
            assert mgr.discover_plan(34).status is PlanDiscoveryStatus.FOUND
        read.assert_not_called()


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
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value=labels,
            ),
        ):
            kept = mgr.filter()

        assert kept == [11, 12]

    def test_force_replans_even_plan_go_issues(self) -> None:
        from hephaestus.automation.state_labels import STATE_PLAN_GO

        opts = _make_options(issues=[10])
        opts.skip_closed = False
        opts.force = True
        mgr = PlannerStateManager(opts)

        with patch(
            "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
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
            "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
            return_value={10: [STATE_PLAN_GO], 11: ["bug"]},
        ):
            mgr.filter()

        assert mgr.get_cached_labels(11) == ["bug"]
        # Unfetched issue → empty list (cache populated but issue absent).
        assert mgr.get_cached_labels(99) == []

    def test_labels_cache_none_before_filter(self) -> None:
        mgr = PlannerStateManager(_make_options())
        assert mgr.get_cached_labels(1) is None


class TestFilterEpicExclusion:
    """``filter()`` drops epic/roadmap issues and tags them state:skip (#1669)."""

    def test_drops_epic_by_label_and_title_and_tags_skip(self) -> None:
        opts = _make_options(issues=[10, 11, 12])
        opts.skip_closed = False
        mgr = PlannerStateManager(opts)

        labels = {10: ["bug"], 11: ["epic"], 12: ["feature"]}
        titles = {10: "Fix crash", 11: "Umbrella", 12: "Q3 Roadmap rollup"}

        with (
            patch(
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value=labels,
            ),
            patch(
                "hephaestus.automation.state.planner.fetch_all_issue_titles_graphql",
                return_value=titles,
            ),
            patch("hephaestus.automation.state.planner.skip_epics") as mock_skip,
        ):
            kept = mgr.filter()

        assert kept == [10]  # 11 (epic label) and 12 (roadmap title) excluded
        mock_skip.assert_called_once()
        tagged = mock_skip.call_args[0][0]
        assert set(tagged.keys()) == {11, 12}

    def test_no_epics_does_not_tag(self) -> None:
        opts = _make_options(issues=[10, 11])
        opts.skip_closed = False
        mgr = PlannerStateManager(opts)

        with (
            patch(
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value={10: ["bug"], 11: []},
            ),
            patch(
                "hephaestus.automation.state.planner.fetch_all_issue_titles_graphql",
                return_value={10: "a", 11: "b"},
            ),
            patch("hephaestus.automation.state.planner.skip_epics") as mock_skip,
        ):
            kept = mgr.filter()

        assert kept == [10, 11]
        mock_skip.assert_not_called()


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
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value={10: [STATE_PLAN_GO], 11: [STATE_PLAN_GO]},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.state.planner"),
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
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value={10: [], 11: []},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.state.planner"),
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
                "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
                return_value={10: [STATE_PLAN_GO], 11: [STATE_PLAN_GO]},
            ),
            caplog.at_level("WARNING", logger="hephaestus.automation.state.planner"),
        ):
            kept = mgr.filter()

        assert kept == []
        assert not any("filtered out" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# discover_plan — fallback (no cache)
# ---------------------------------------------------------------------------


class TestHasExistingPlanFallback:
    """Plan discovery falls back to a complete REST read when no cache exists."""

    def test_returns_true_via_gh_cli_when_plan_present(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[41]))
        with patch(
            "hephaestus.automation.state.planner.fetch_issue_comments_metadata",
            return_value=[{"body": _plan_body(), "user": {"login": "bot"}}],
        ):
            assert mgr.discover_plan(41).status is PlanDiscoveryStatus.FOUND

    def test_returns_false_via_gh_cli_when_no_plan(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[42]))
        with patch(
            "hephaestus.automation.state.planner.fetch_issue_comments_metadata",
            return_value=[{"body": _other_body(), "user": {"login": "bot"}}],
        ):
            assert mgr.discover_plan(42).status is PlanDiscoveryStatus.ABSENT

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("network error"), GitHubRateLimitError("rate limited", reset_epoch=123)],
    )
    def test_read_failure_is_explicit(self, failure: Exception) -> None:
        mgr = PlannerStateManager(_make_options(issues=[43]))
        with patch(
            "hephaestus.automation.state.planner.fetch_issue_comments_metadata",
            side_effect=failure,
        ):
            result = mgr.discover_plan(43)

        assert result.status is PlanDiscoveryStatus.READ_ERROR

    def test_prefetch_failure_is_read_error(self) -> None:
        mgr = PlannerStateManager(_make_options(issues=[44]))
        with patch.object(
            mgr,
            "_read_comments",
            side_effect=CommentJournalReadError("rate limited"),
        ):
            mgr.prefetch_comments([44])

        assert mgr.discover_plan(44).status is PlanDiscoveryStatus.READ_ERROR
