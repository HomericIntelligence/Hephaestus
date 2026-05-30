"""Unit tests for ``hephaestus.automation.review_state``.

The shared GO-plan-review gate is load-bearing — both
``plan_reviewer._latest_review_is_final`` (skip-on-GO) and
``implementer._implement_issue`` (gate-on-GO) read from it, so it needs
explicit coverage independent of either caller. See #551.

Planning (and PR review) use a single binary ``Verdict: GO | NOGO`` vocabulary
parsed by ``claude_invoke.parse_review_verdict``; this module's helpers delegate
to it so the gate and the in-loop reviewer never diverge.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.review_state import (
    MAX_UNPARSEABLE_VERDICT_PASSES,
    PLAN_REVIEW_PREFIX,
    _extract_verdict_context,
    count_unparseable_verdict_passes,
    exceeds_unparseable_verdict_cap,
    fetch_all_issue_comments_graphql,
    is_plan_review_go,
    latest_verdict,
)

# ---------------------------------------------------------------------------
# latest_verdict
# ---------------------------------------------------------------------------


class TestLatestVerdict:
    """latest_verdict: GO/NOGO/None, LAST verdict line wins."""

    def test_returns_go_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nLooks great.\n\nVerdict: GO\n"
        assert latest_verdict(body) == "GO"

    def test_returns_nogo_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNeeds work.\n\nVerdict: NOGO\n"
        assert latest_verdict(body) == "NOGO"

    def test_accepts_bold_verdict_line(self) -> None:
        # The matcher tolerates the optional bold form too.
        body = f"{PLAN_REVIEW_PREFIX}\n\nLooks great.\n\n**Verdict: GO**\n"
        assert latest_verdict(body) == "GO"

    def test_returns_none_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNo verdict line at all.\n"
        assert latest_verdict(body) is None

    def test_picks_last_verdict_go_then_nogo(self) -> None:
        # A posted review may discuss an earlier verdict before settling; the
        # reviewer's FINAL word wins. GO then NOGO → NOGO (fail safe: re-review).
        body = (
            f"{PLAN_REVIEW_PREFIX}\n\nInitial impression: sound.\n\nVerdict: GO\n\n"
            "On reflection a fatal bug surfaced.\n\nVerdict: NOGO\n"
        )
        assert latest_verdict(body) == "NOGO"

    def test_picks_last_verdict_nogo_then_go(self) -> None:
        # NOGO then GO → GO (the reviewer withdrew the concern).
        body = (
            f"{PLAN_REVIEW_PREFIX}\n\nFirst-pass concern.\n\nVerdict: NOGO\n\n"
            "After re-reading, concern unfounded.\n\nVerdict: GO\n"
        )
        assert latest_verdict(body) == "GO"

    def test_ignores_inline_marker_in_prose(self) -> None:
        # The verdict regex anchors to the start of a line (optional bold), so a
        # mid-sentence mention like "we did not pick Verdict: GO" does NOT match;
        # only the real trailing verdict line counts.
        body = (
            f"{PLAN_REVIEW_PREFIX}\nWe did not pick Verdict: GO because of issues.\nVerdict: NOGO\n"
        )
        assert latest_verdict(body) == "NOGO"

    def test_nogo_dash_and_space_forms_normalize(self) -> None:
        # NO-GO / NO GO normalize to NOGO.
        assert latest_verdict(f"{PLAN_REVIEW_PREFIX}\n\nVerdict: NO-GO\n") == "NOGO"
        assert latest_verdict(f"{PLAN_REVIEW_PREFIX}\n\nVerdict: NO GO\n") == "NOGO"


# ---------------------------------------------------------------------------
# _extract_verdict_context
# ---------------------------------------------------------------------------


class TestExtractVerdictContext:
    """Context extraction for not-GO logs."""

    def test_extracts_verdict_line_when_present(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nReview text.\n\nVerdict: NOGO\n"
        context = _extract_verdict_context(body)
        assert "Verdict: NOGO" in context

    def test_returns_first_non_prefix_line_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nThis is the main content.\nMore details.\n"
        context = _extract_verdict_context(body)
        assert context == "This is the main content."

    def test_prefers_verdict_line_over_first_content_line(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nFirst line of content.\nMore details.\nVerdict: NOGO\n"
        context = _extract_verdict_context(body)
        assert "Verdict: NOGO" in context

    def test_returns_empty_string_when_body_is_empty(self) -> None:
        body = ""
        context = _extract_verdict_context(body)
        assert context == ""

    def test_returns_empty_string_when_only_prefix_lines(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n{PLAN_REVIEW_PREFIX}\n"
        context = _extract_verdict_context(body)
        assert context == ""

    def test_truncates_to_verdict_log_preview_chars(self) -> None:
        long_line = "x" * 500
        body = f"{PLAN_REVIEW_PREFIX}\n\n{long_line}\n"
        context = _extract_verdict_context(body)
        assert len(context) <= 200


# ---------------------------------------------------------------------------
# is_plan_review_go (with pre-supplied comments)
# ---------------------------------------------------------------------------


def _review_comment(verdict: str | None, url: str | None = None) -> dict[str, Any]:
    """Build a plan-review comment. ``verdict`` is "GO"/"NOGO" or None (malformed)."""
    if verdict is None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nMalformed review with no verdict line.\n"
    else:
        body = f"{PLAN_REVIEW_PREFIX}\n\nBody.\n\nVerdict: {verdict}\n"
    comment = {"body": body}
    if url is not None:
        comment["url"] = url
    return comment


def _plan_comment() -> dict[str, Any]:
    return {"body": "# Implementation Plan\n\nSteps...\n"}


class TestIsPlanReviewGoWithComments:
    """Caller passes ``comments`` explicitly; no GraphQL fetch."""

    def test_go_returns_true(self) -> None:
        comments = [_plan_comment(), _review_comment("GO")]
        assert is_plan_review_go(123, comments=comments) is True

    def test_nogo_returns_false(self) -> None:
        comments = [_plan_comment(), _review_comment("NOGO")]
        assert is_plan_review_go(123, comments=comments) is False

    def test_no_review_returns_false(self) -> None:
        # Plan exists but no plan-review comment yet.
        comments = [_plan_comment()]
        assert is_plan_review_go(123, comments=comments) is False

    def test_empty_comments_returns_false(self) -> None:
        assert is_plan_review_go(123, comments=[]) is False

    def test_takes_latest_review_when_multiple_newer_nogo(self) -> None:
        # Older GO, newer NOGO → newer wins (gate is False).
        comments = [
            _plan_comment(),
            _review_comment("GO"),
            _review_comment("NOGO"),
        ]
        assert is_plan_review_go(123, comments=comments) is False

    def test_takes_latest_review_when_multiple_newer_go(self) -> None:
        # Older NOGO, planner amended, newer GO → GO wins (gate is True).
        comments = [
            _plan_comment(),
            _review_comment("NOGO"),
            _review_comment("GO"),
        ]
        assert is_plan_review_go(123, comments=comments) is True

    def test_malformed_review_returns_false(self) -> None:
        # Review comment exists with the right prefix but no verdict line.
        comments = [_plan_comment(), _review_comment(None)]
        assert is_plan_review_go(123, comments=comments) is False

    def test_enriched_logging_includes_verdict_context_and_url(self, caplog: Any) -> None:
        """Not-GO logs should include verdict context and URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment("NOGO", url="https://github.com/o/r/issues/123#comment-1"),
        ]
        is_plan_review_go(123, comments=comments)
        log_text = caplog.text
        assert "NOGO" in log_text
        assert "https://github.com/o/r/issues/123#comment-1" in log_text

    def test_enriched_logging_fallback_no_url(self, caplog: Any) -> None:
        """Not-GO logs should show <no url> when URL is missing."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [_plan_comment(), _review_comment("NOGO")]
        is_plan_review_go(123, comments=comments)
        log_text = caplog.text
        assert "NOGO" in log_text
        assert "<no url>" in log_text

    def test_enriched_logging_missing_verdict(self, caplog: Any) -> None:
        """Malformed-verdict logs at WARNING with first line of comment + URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment(None, url="https://github.com/o/r/issues/123#comment-2"),
        ]
        is_plan_review_go(123, comments=comments)
        log_text = caplog.text
        # #615: malformed verdict emits a WARNING with first line + URL.
        assert "no parseable Verdict: GO/NOGO line" in log_text
        assert "https://github.com/o/r/issues/123#comment-2" in log_text


# ---------------------------------------------------------------------------
# is_plan_review_go (fetches comments itself via GraphQL)
# ---------------------------------------------------------------------------


def _graphql_payload(comment_bodies: list[str]) -> str:
    # GraphQL returns newest-first; production code reverses to chronological.
    # So we hand it newest-first (i.e. reversed input).
    nodes = [{"body": b, "updatedAt": "2025-01-01T00:00:00Z"} for b in reversed(comment_bodies)]
    return json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": nodes}}}}})


class TestIsPlanReviewGoWithFetch:
    """No comments supplied → module fetches via GraphQL."""

    @pytest.fixture(autouse=True)
    def _patch_repo_helpers(self) -> Any:
        with (
            patch(
                "hephaestus.automation.review_state.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.review_state.get_repo_info",
                return_value=("owner", "name"),
            ),
            # #704: is_plan_review_go also lazy-fetches labels via
            # gh_issue_json when neither labels nor comments were supplied.
            # Return an empty-labels issue so the comment-scan path is
            # exercised (which is what these legacy tests target).
            patch(
                "hephaestus.automation.review_state.gh_issue_json",
                return_value={"labels": []},
            ),
        ):
            yield

    def test_fetches_and_returns_true_for_go(self) -> None:
        go_body = _review_comment("GO")["body"]
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload(["# Implementation Plan\n", go_body])
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            assert is_plan_review_go(123) is True

    def test_fetches_and_returns_false_for_nogo(self) -> None:
        nogo_body = _review_comment("NOGO")["body"]
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload(["# Implementation Plan\n", nogo_body])
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            assert is_plan_review_go(123) is False

    def test_returns_false_when_gh_raises(self) -> None:
        with patch(
            "hephaestus.automation.review_state._gh_call",
            side_effect=RuntimeError("network down"),
        ):
            assert is_plan_review_go(123) is False

    def test_uses_owner_repo_tuple_from_get_repo_info(self) -> None:
        """Regression test for #588 — derive owner+name from get_repo_info.

        ``_fetch_issue_comments_graphql`` must obtain ``owner`` and ``name``
        from ``get_repo_info`` (which returns a tuple) rather than calling
        ``get_repo_slug(...).split('/', 1)`` (which crashes with "not enough
        values to unpack" because the slug is just the repo name with no
        owner prefix). This mirrors the fix in PR #575 for the same bug
        pattern in ``plan_reviewer.py``; #588 caught the missed copy here.
        """
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload([])
        with (
            patch(
                "hephaestus.automation.review_state.get_repo_info",
                return_value=("HomericIntelligence", "ProjectMnemosyne"),
            ) as mock_info,
            patch(
                "hephaestus.automation.review_state._gh_call",
                return_value=mock_result,
            ) as mock_gh,
        ):
            is_plan_review_go(1928)

        mock_info.assert_called_once()
        gh_args = mock_gh.call_args[0][0]
        joined = " ".join(gh_args)
        assert "owner=HomericIntelligence" in joined
        assert "name=ProjectMnemosyne" in joined


# ---------------------------------------------------------------------------
# count_unparseable_verdict_passes / exceeds_unparseable_verdict_cap (#615)
# ---------------------------------------------------------------------------


class TestUnparseableVerdictCap:
    """Bounded-retry helpers introduced by #615."""

    def test_zero_when_all_verdicts_parseable(self) -> None:
        comments = [
            _plan_comment(),
            _review_comment("GO"),
        ]
        assert count_unparseable_verdict_passes(comments) == 0

    def test_counts_malformed_review_comments(self) -> None:
        # Two plan-review comments with no parseable verdict + one with NOGO.
        comments = [
            _plan_comment(),
            _review_comment(None),  # malformed pass 1
            _review_comment(None),  # malformed pass 2
            _review_comment("NOGO"),  # well-formed — should NOT be counted
        ]
        assert count_unparseable_verdict_passes(comments) == 2

    def test_only_counts_plan_review_comments(self) -> None:
        # Non-plan-review comments (no PLAN_REVIEW_PREFIX) should not be counted.
        other = {"body": "Some other comment with no verdict"}
        comments = [other, _plan_comment(), other]
        assert count_unparseable_verdict_passes(comments) == 0

    def test_empty_comments_returns_zero(self) -> None:
        assert count_unparseable_verdict_passes([]) == 0

    def test_exceeds_cap_false_when_below_threshold(self) -> None:
        comments = [
            _plan_comment(),
            _review_comment(None),  # 1 malformed pass
        ]
        assert exceeds_unparseable_verdict_cap(comments) is False

    def test_exceeds_cap_true_when_at_threshold(self) -> None:
        comments = [_plan_comment()] + [_review_comment(None)] * MAX_UNPARSEABLE_VERDICT_PASSES
        assert exceeds_unparseable_verdict_cap(comments) is True

    def test_exceeds_cap_false_when_parseable_verdict_resets(self) -> None:
        # Even 2 malformed + 1 parseable: cap not exceeded (count stops at 2).
        comments = [
            _plan_comment(),
            _review_comment(None),
            _review_comment(None),
            _review_comment("NOGO"),
        ]
        assert exceeds_unparseable_verdict_cap(comments) is False

    def test_custom_cap_respected(self) -> None:
        comments = [_plan_comment(), _review_comment(None)]
        assert exceeds_unparseable_verdict_cap(comments, cap=1) is True
        assert exceeds_unparseable_verdict_cap(comments, cap=2) is False


# ---------------------------------------------------------------------------
# fetch_all_issue_comments_graphql (smoke — import surface)
# ---------------------------------------------------------------------------


def test_fetch_all_issue_comments_graphql_is_importable() -> None:
    """Guard the public import surface used by the planner's batch prefetch."""
    assert callable(fetch_all_issue_comments_graphql)
