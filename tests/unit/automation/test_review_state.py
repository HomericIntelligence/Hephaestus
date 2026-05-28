"""Unit tests for ``hephaestus.automation.review_state``.

The shared APPROVED-plan-review gate is load-bearing — both
``plan_reviewer._latest_review_is_final`` (skip-on-approved) and
``implementer._implement_issue`` (gate-on-approved) read from it, so it
needs explicit coverage independent of either caller. See #551.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.review_state import (
    MAX_UNPARSEABLE_VERDICT_PASSES,
    PLAN_REVIEW_PREFIX,
    VERDICT_APPROVED,
    VERDICT_BLOCK,
    VERDICT_REVISE,
    _extract_verdict_context,
    count_unparseable_verdict_passes,
    exceeds_unparseable_verdict_cap,
    fetch_all_issue_comments_graphql,
    is_plan_review_approved,
    latest_verdict,
)

# ---------------------------------------------------------------------------
# latest_verdict
# ---------------------------------------------------------------------------


class TestLatestVerdict:
    """The regex must take the LAST well-formed verdict line, not a substring."""

    def test_returns_approved_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nLooks great.\n\n**Verdict: APPROVED**\n"
        assert latest_verdict(body) == VERDICT_APPROVED

    def test_returns_revise_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNeeds work.\n\n**Verdict: REVISE**\n"
        assert latest_verdict(body) == VERDICT_REVISE

    def test_returns_block_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nFundamental flaw.\n\n**Verdict: BLOCK**\n"
        assert latest_verdict(body) == VERDICT_BLOCK

    def test_returns_none_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNo verdict line at all.\n"
        assert latest_verdict(body) is None

    def test_picks_last_verdict_when_multiple(self) -> None:
        # Per the prompt contract, only the LAST verdict line counts —
        # Claude may discuss APPROVED then settle on BLOCK.
        body = (
            f"{PLAN_REVIEW_PREFIX}\n"
            "I initially thought:\n"
            "**Verdict: APPROVED**\n"
            "But on reflection:\n"
            "**Verdict: BLOCK**\n"
        )
        assert latest_verdict(body) == VERDICT_BLOCK

    def test_ignores_inline_marker_in_prose(self) -> None:
        # The regex is anchored to line boundaries with MULTILINE, so an
        # inline mention like "we did not pick **Verdict: APPROVED**" does
        # NOT count.
        body = (
            f"{PLAN_REVIEW_PREFIX}\n"
            "We did not pick **Verdict: APPROVED** because of issues.\n"
            "**Verdict: REVISE**\n"
        )
        assert latest_verdict(body) == VERDICT_REVISE


# ---------------------------------------------------------------------------
# _extract_verdict_context
# ---------------------------------------------------------------------------


class TestExtractVerdictContext:
    """Context extraction for not-APPROVED logs."""

    def test_extracts_verdict_line_when_present(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nReview text.\n\n**Verdict: REVISE**\n"
        context = _extract_verdict_context(body)
        assert "Verdict: REVISE" in context

    def test_returns_first_non_prefix_line_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nThis is the main content.\nMore details.\n"
        context = _extract_verdict_context(body)
        assert context == "This is the main content."

    def test_prefers_verdict_line_over_first_content_line(self) -> None:
        body = (
            f"{PLAN_REVIEW_PREFIX}\n\nFirst line of content.\nMore details.\n**Verdict: BLOCK**\n"
        )
        context = _extract_verdict_context(body)
        assert "Verdict: BLOCK" in context

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
# is_plan_review_approved (with pre-supplied comments)
# ---------------------------------------------------------------------------


def _review_comment(verdict: str | None, url: str | None = None) -> dict[str, Any]:
    if verdict is None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nMalformed review with no verdict line.\n"
    else:
        body = f"{PLAN_REVIEW_PREFIX}\n\nBody.\n\n**Verdict: {verdict}**\n"
    comment = {"body": body}
    if url is not None:
        comment["url"] = url
    return comment


def _plan_comment() -> dict[str, Any]:
    return {"body": "# Implementation Plan\n\nSteps...\n"}


class TestIsPlanReviewApprovedWithComments:
    """Caller passes ``comments`` explicitly; no GraphQL fetch."""

    def test_approved_returns_true(self) -> None:
        comments = [_plan_comment(), _review_comment(VERDICT_APPROVED)]
        assert is_plan_review_approved(123, comments=comments) is True

    def test_revise_returns_false(self) -> None:
        comments = [_plan_comment(), _review_comment(VERDICT_REVISE)]
        assert is_plan_review_approved(123, comments=comments) is False

    def test_block_returns_false(self) -> None:
        comments = [_plan_comment(), _review_comment(VERDICT_BLOCK)]
        assert is_plan_review_approved(123, comments=comments) is False

    def test_no_review_returns_false(self) -> None:
        # Plan exists but no plan-review comment yet.
        comments = [_plan_comment()]
        assert is_plan_review_approved(123, comments=comments) is False

    def test_empty_comments_returns_false(self) -> None:
        assert is_plan_review_approved(123, comments=[]) is False

    def test_takes_latest_review_when_multiple(self) -> None:
        # Older APPROVED, newer BLOCK → newer wins.
        comments = [
            _plan_comment(),
            _review_comment(VERDICT_APPROVED),
            _review_comment(VERDICT_BLOCK),
        ]
        assert is_plan_review_approved(123, comments=comments) is False

    def test_takes_latest_review_when_multiple_newer_approved(self) -> None:
        # Older REVISE, planner amended, newer APPROVED → APPROVED wins.
        comments = [
            _plan_comment(),
            _review_comment(VERDICT_REVISE),
            _review_comment(VERDICT_APPROVED),
        ]
        assert is_plan_review_approved(123, comments=comments) is True

    def test_malformed_review_returns_false(self) -> None:
        # Review comment exists with the right prefix but no verdict line.
        comments = [_plan_comment(), _review_comment(None)]
        assert is_plan_review_approved(123, comments=comments) is False

    def test_enriched_logging_includes_verdict_context_and_url(self, caplog: Any) -> None:
        """Not-APPROVED logs should include verdict context and URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment(VERDICT_BLOCK, url="https://github.com/o/r/issues/123#comment-1"),
        ]
        is_plan_review_approved(123, comments=comments)
        # Logs should include verdict context and URL when not-APPROVED
        log_text = caplog.text
        assert "BLOCK" in log_text
        assert "https://github.com/o/r/issues/123#comment-1" in log_text

    def test_enriched_logging_fallback_no_url(self, caplog: Any) -> None:
        """Not-APPROVED logs should show <no url> when URL is missing."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [_plan_comment(), _review_comment(VERDICT_REVISE)]
        is_plan_review_approved(123, comments=comments)
        log_text = caplog.text
        assert "REVISE" in log_text
        assert "<no url>" in log_text

    def test_enriched_logging_missing_verdict(self, caplog: Any) -> None:
        """Malformed-verdict logs at WARNING with first line of comment + URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment(None, url="https://github.com/o/r/issues/123#comment-2"),
        ]
        is_plan_review_approved(123, comments=comments)
        log_text = caplog.text
        # #615: malformed verdict now emits WARNING-level log with first line + URL
        assert "VERDICT_LINE_RE did not match" in log_text
        assert "https://github.com/o/r/issues/123#comment-2" in log_text


# ---------------------------------------------------------------------------
# is_plan_review_approved (fetches comments itself via GraphQL)
# ---------------------------------------------------------------------------


def _graphql_payload(comment_bodies: list[str]) -> str:
    # GraphQL returns newest-first; production code reverses to chronological.
    # So we hand it newest-first (i.e. reversed input).
    nodes = [{"body": b, "updatedAt": "2025-01-01T00:00:00Z"} for b in reversed(comment_bodies)]
    return json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": nodes}}}}})


class TestIsPlanReviewApprovedWithFetch:
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
        ):
            yield

    def test_fetches_and_returns_true_for_approved(self) -> None:
        approved_body = _review_comment(VERDICT_APPROVED)["body"]
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload(["# Implementation Plan\n", approved_body])
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            assert is_plan_review_approved(123) is True

    def test_fetches_and_returns_false_for_block(self) -> None:
        block_body = _review_comment(VERDICT_BLOCK)["body"]
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload(["# Implementation Plan\n", block_body])
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            assert is_plan_review_approved(123) is False

    def test_returns_false_when_gh_raises(self) -> None:
        with patch(
            "hephaestus.automation.review_state._gh_call",
            side_effect=RuntimeError("network down"),
        ):
            assert is_plan_review_approved(123) is False

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
            is_plan_review_approved(1928)

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
            _review_comment(VERDICT_APPROVED),
        ]
        assert count_unparseable_verdict_passes(comments) == 0

    def test_counts_malformed_review_comments(self) -> None:
        # Two plan-review comments with no parseable verdict + one with REVISE.
        comments = [
            _plan_comment(),
            _review_comment(None),  # malformed pass 1
            _review_comment(None),  # malformed pass 2
            _review_comment(VERDICT_REVISE),  # well-formed — should NOT be counted
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
            _review_comment(VERDICT_REVISE),
        ]
        assert exceeds_unparseable_verdict_cap(comments) is False

    def test_custom_cap_respected(self) -> None:
        comments = [_plan_comment(), _review_comment(None)]
        assert exceeds_unparseable_verdict_cap(comments, cap=1) is True
        assert exceeds_unparseable_verdict_cap(comments, cap=2) is False

    def test_malformed_verdict_logs_at_warning_level(self, caplog: Any) -> None:
        """#615: missing verdict should produce a WARNING with first line + URL."""
        import logging

        caplog.set_level(logging.WARNING)
        comments = [
            _plan_comment(),
            _review_comment(None, url="https://github.com/o/r/issues/615#comment-99"),
        ]
        is_plan_review_approved(615, comments=comments)
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected at least one WARNING log for malformed verdict"
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "VERDICT_LINE_RE did not match" in combined
        assert "https://github.com/o/r/issues/615#comment-99" in combined


# ---------------------------------------------------------------------------
# fetch_all_issue_comments_graphql (#616)
# ---------------------------------------------------------------------------


def _batch_graphql_payload(issues: dict[int, list[str]]) -> str:
    """Build a GraphQL batch response for alias-indexed issues.

    ``issues`` maps issue_number -> list of comment bodies (chronological).
    The aliasing uses the position in ``sorted(issues.keys())`` so tests can
    be deterministic.
    """
    repo: dict[str, Any] = {}
    for idx, (num, bodies) in enumerate(sorted(issues.items())):
        # GraphQL returns newest-first; production code reverses to chrono.
        nodes = [
            {"body": b, "updatedAt": "2025-01-01T00:00:00Z", "url": f"https://gh/{num}/{i}"}
            for i, b in enumerate(reversed(bodies))
        ]
        repo[f"issue{idx}"] = {"comments": {"nodes": nodes}}
    return json.dumps({"data": {"repository": repo}})


class TestFetchAllIssueCommentsGraphql:
    """Batch comment fetch for plan-detection + review-gate (#616)."""

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

    def test_returns_empty_dict_for_empty_input(self) -> None:
        assert fetch_all_issue_comments_graphql([]) == {}

    def test_single_issue_comments_in_chrono_order(self) -> None:
        bodies = ["first comment", "second comment"]
        payload = _batch_graphql_payload({101: bodies})
        mock_result = MagicMock()
        mock_result.stdout = payload
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            result = fetch_all_issue_comments_graphql([101])
        assert 101 in result
        assert [c["body"] for c in result[101]] == bodies

    def test_multiple_issues_returned_correctly(self) -> None:
        payload = _batch_graphql_payload(
            {
                201: ["plan A"],
                202: ["plan B", "review B"],
            }
        )
        mock_result = MagicMock()
        mock_result.stdout = payload
        with patch("hephaestus.automation.review_state._gh_call", return_value=mock_result):
            result = fetch_all_issue_comments_graphql([201, 202])
        assert len(result[201]) == 1
        assert result[201][0]["body"] == "plan A"
        assert len(result[202]) == 2
        assert result[202][0]["body"] == "plan B"
        assert result[202][1]["body"] == "review B"

    def test_makes_single_gh_call(self) -> None:
        payload = _batch_graphql_payload({301: [], 302: []})
        mock_result = MagicMock()
        mock_result.stdout = payload
        with patch(
            "hephaestus.automation.review_state._gh_call", return_value=mock_result
        ) as mock_gh:
            fetch_all_issue_comments_graphql([301, 302])
        mock_gh.assert_called_once()

    def test_returns_empty_lists_on_gh_failure(self) -> None:
        with patch(
            "hephaestus.automation.review_state._gh_call",
            side_effect=RuntimeError("network error"),
        ):
            result = fetch_all_issue_comments_graphql([401, 402])
        assert result == {401: [], 402: []}
