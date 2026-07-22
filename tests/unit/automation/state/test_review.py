"""Unit tests for ``hephaestus.automation.state.review``.

The shared GO-plan-review gate is load-bearing: the standalone reviewer and
implementation admission both use it. GitHub labels are the sole durable
authority; comment state tokens are diagnostic data only.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, strategies as st

from hephaestus.automation.state.review import (
    MAX_UNPARSEABLE_VERDICT_PASSES,
    PLAN_REVIEW_PREFIX,
    _extract_verdict_context,
    _fetch_issue_comments_graphql,
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
    """latest_verdict recognizes state tokens only."""

    def test_returns_go_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nLooks great.\n\nstate:plan-go\n"
        assert latest_verdict(body) == "GO"

    def test_returns_nogo_when_only_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNeeds work.\n\nstate:plan-no-go\n"
        assert latest_verdict(body) == "NOGO"

    def test_rejects_bold_legacy_verdict_line(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nLooks great.\n\n**Verdict: GO**\n"
        assert latest_verdict(body) is None

    def test_returns_none_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nNo verdict line at all.\n"
        assert latest_verdict(body) is None

    def test_picks_last_verdict_go_then_nogo(self) -> None:
        # A posted review may discuss an earlier verdict before settling; the
        # reviewer's FINAL word wins. GO then NOGO → NOGO (fail safe: re-review).
        body = (
            f"{PLAN_REVIEW_PREFIX}\n\nInitial impression: sound.\n\nstate:plan-go\n\n"
            "On reflection a fatal bug surfaced.\n\nstate:plan-no-go\n"
        )
        assert latest_verdict(body) == "NOGO"

    def test_picks_last_verdict_nogo_then_go(self) -> None:
        # NOGO then GO → GO (the reviewer withdrew the concern).
        body = (
            f"{PLAN_REVIEW_PREFIX}\n\nFirst-pass concern.\n\nstate:plan-no-go\n\n"
            "After re-reading, concern unfounded.\n\nstate:plan-go\n"
        )
        assert latest_verdict(body) == "GO"

    def test_ignores_inline_marker_in_prose(self) -> None:
        # Inline prose is not an exact state-token line.
        body = (
            f"{PLAN_REVIEW_PREFIX}\nWe did not pick state:plan-go because of issues.\n"
            "state:plan-no-go\n"
        )
        assert latest_verdict(body) == "NOGO"

    def test_legacy_nogo_forms_are_not_recognized(self) -> None:
        assert latest_verdict(f"{PLAN_REVIEW_PREFIX}\n\nVerdict: NO-GO\n") is None
        assert latest_verdict(f"{PLAN_REVIEW_PREFIX}\n\nVerdict: NO GO\n") is None


# ---------------------------------------------------------------------------
# _extract_verdict_context
# ---------------------------------------------------------------------------


class TestExtractVerdictContext:
    """Context extraction for not-GO logs."""

    def test_extracts_verdict_line_when_present(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nReview text.\n\nstate:plan-no-go\n"
        context = _extract_verdict_context(body)
        assert "state:plan-no-go" in context

    def test_returns_first_non_prefix_line_when_no_verdict(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nThis is the main content.\nMore details.\n"
        context = _extract_verdict_context(body)
        assert context == "This is the main content."

    def test_prefers_verdict_line_over_first_content_line(self) -> None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nFirst line of content.\nMore details.\nstate:plan-no-go\n"
        context = _extract_verdict_context(body)
        assert "state:plan-no-go" in context

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
        token = {
            "GO": "state:plan-go",
            "NOGO": "state:plan-no-go",
            "BLOCKED": "state:plan-blocked",
        }[verdict]
        body = f"{PLAN_REVIEW_PREFIX}\n\nBody.\n\n{token}\n"
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
        assert is_plan_review_go(123, comments=comments, issue_labels=["state:plan-go"]) is True

    def test_nogo_returns_false(self) -> None:
        comments = [_plan_comment(), _review_comment("NOGO")]
        assert is_plan_review_go(123, comments=comments, issue_labels=["state:plan-no-go"]) is False

    def test_no_review_returns_false(self) -> None:
        # Plan exists but no plan-review comment yet.
        comments = [_plan_comment()]
        assert is_plan_review_go(123, comments=comments, issue_labels=[]) is False

    def test_empty_comments_returns_false(self) -> None:
        assert is_plan_review_go(123, comments=[], issue_labels=[]) is False

    def test_takes_latest_review_when_multiple_newer_nogo(self) -> None:
        # Older GO, newer NOGO → newer wins (gate is False).
        comments = [
            _plan_comment(),
            _review_comment("GO"),
            _review_comment("NOGO"),
        ]
        assert is_plan_review_go(123, comments=comments, issue_labels=["state:plan-no-go"]) is False

    def test_takes_latest_review_when_multiple_newer_go(self) -> None:
        # Older NOGO, planner amended, newer GO → GO wins (gate is True).
        comments = [
            _plan_comment(),
            _review_comment("NOGO"),
            _review_comment("GO"),
        ]
        assert is_plan_review_go(123, comments=comments, issue_labels=["state:plan-go"]) is True

    def test_malformed_review_returns_false(self) -> None:
        # Review comment exists with the right prefix but no verdict line.
        comments = [_plan_comment(), _review_comment(None)]
        assert is_plan_review_go(123, comments=comments, issue_labels=[]) is False

    def test_enriched_logging_includes_verdict_context_and_url(self, caplog: Any) -> None:
        """Not-GO logs should include verdict context and URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment("NOGO", url="https://github.com/o/r/issues/123#comment-1"),
        ]
        is_plan_review_go(123, comments=comments, issue_labels=["state:plan-no-go"])
        log_text = caplog.text
        assert "not GO" in log_text
        assert "https://github.com/o/r/issues/123#comment-1" not in log_text

    def test_enriched_logging_fallback_no_url(self, caplog: Any) -> None:
        """Not-GO logs should show <no url> when URL is missing."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [_plan_comment(), _review_comment("NOGO")]
        is_plan_review_go(123, comments=comments, issue_labels=["state:plan-no-go"])
        log_text = caplog.text
        assert "not GO" in log_text
        assert "<no url>" not in log_text

    def test_enriched_logging_missing_verdict(self, caplog: Any) -> None:
        """Malformed-verdict logs at WARNING with first line of comment + URL."""
        import logging

        caplog.set_level(logging.DEBUG)
        comments = [
            _plan_comment(),
            _review_comment(None, url="https://github.com/o/r/issues/123#comment-2"),
        ]
        is_plan_review_go(123, comments=comments, issue_labels=[])
        log_text = caplog.text
        assert "not GO" in log_text
        assert "https://github.com/o/r/issues/123#comment-2" not in log_text


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
                "hephaestus.automation.state.review.get_repo_root",
                return_value="/tmp/repo",
            ),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "name"),
            ),
            # #704: is_plan_review_go also lazy-fetches labels via
            # gh_issue_json when neither labels nor comments were supplied.
            # Return an empty-labels issue so the comment-scan path is
            # exercised (which is what these legacy tests target).
            patch(
                "hephaestus.automation.state.review.gh_issue_json",
                return_value={"labels": []},
            ),
        ):
            yield

    def test_fetches_and_returns_true_for_go(self) -> None:
        with patch(
            "hephaestus.automation.state.review.gh_issue_json",
            return_value={"labels": [{"name": "state:plan-go"}]},
        ):
            assert is_plan_review_go(123) is True

    def test_fetches_and_returns_false_for_nogo(self) -> None:
        nogo_body = _review_comment("NOGO")["body"]
        mock_result = MagicMock()
        mock_result.stdout = _graphql_payload(["# Implementation Plan\n", nogo_body])
        with patch("hephaestus.automation.state.review._gh_call", return_value=mock_result):
            assert is_plan_review_go(123) is False

    def test_returns_false_when_gh_raises(self) -> None:
        with patch(
            "hephaestus.automation.state.review._gh_call",
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
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("HomericIntelligence", "Mnemosyne"),
            ) as mock_info,
            patch(
                "hephaestus.automation.state.review._gh_call",
                return_value=mock_result,
            ) as mock_gh,
        ):
            _fetch_issue_comments_graphql(1928)

        mock_info.assert_called_once()
        gh_args = mock_gh.call_args[0][0]
        joined = " ".join(gh_args)
        assert "owner=HomericIntelligence" in joined
        assert "name=Mnemosyne" in joined

    def test_fetch_issue_comments_returns_empty_on_gh_failure(self) -> None:
        """#1426: a ``_gh_call`` failure is logged and yields an empty list.

        Covers the formerly ``# pragma: no cover`` fallback handler in
        ``_fetch_issue_comments_graphql`` — callers treat ``[]`` as "no review".
        """
        with (
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("HomericIntelligence", "Mnemosyne"),
            ),
            patch(
                "hephaestus.automation.state.review._gh_call",
                side_effect=RuntimeError("gh down"),
            ),
        ):
            assert _fetch_issue_comments_graphql(1928) == []


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


def test_fetch_all_comments_returns_empty_map_on_gh_failure() -> None:
    """#1426: a batch comment-fetch failure → every issue maps to ``[]``.

    Covers the formerly ``# pragma: no cover`` fallback handler in
    ``fetch_all_issue_comments_graphql``; callers get empty lists.
    """
    with (
        patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
        patch(
            "hephaestus.automation.state.review.get_repo_info",
            return_value=("HomericIntelligence", "Mnemosyne"),
        ),
        patch(
            "hephaestus.automation.state.review._gh_call",
            side_effect=RuntimeError("gh down"),
        ),
    ):
        assert fetch_all_issue_comments_graphql([1, 2]) == {1: [], 2: []}


# ---------------------------------------------------------------------------
# fetch_all_issue_labels_graphql
# ---------------------------------------------------------------------------


class TestFetchAllIssueLabelsGraphql:
    """Batched label fetch used by the planner to drop already-GO issues."""

    def test_empty_input_returns_empty(self) -> None:
        from hephaestus.automation.state.review import fetch_all_issue_labels_graphql

        assert fetch_all_issue_labels_graphql([]) == {}

    def test_parses_aliased_labels(self) -> None:
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.state.review import fetch_all_issue_labels_graphql

        payload = {
            "data": {
                "repository": {
                    "issue0": {"labels": {"nodes": [{"name": "state:plan-go"}, {"name": "bug"}]}},
                    "issue1": {"labels": {"nodes": []}},
                }
            }
        }
        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch("hephaestus.automation.state.review._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout=json.dumps(payload))
            result = fetch_all_issue_labels_graphql([10, 11])

        assert result == {10: ["state:plan-go", "bug"], 11: []}

    def test_fetch_failure_returns_empty_lists(self) -> None:
        from unittest.mock import patch

        from hephaestus.automation.state.review import fetch_all_issue_labels_graphql

        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.state.review._gh_call",
                side_effect=RuntimeError("gh down"),
            ),
        ):
            result = fetch_all_issue_labels_graphql([10, 11])

        assert result == {10: [], 11: []}

    def test_owner_name_passed_as_graphql_variables_not_interpolated(self) -> None:
        """Regression: owner/name must be GraphQL variables, not interpolated.

        The original implementation interpolated ``{owner!r}`` into the query
        body, producing single-quoted GraphQL string literals
        (``owner:'HomericIntelligence'``). GraphQL only accepts double-quoted
        strings, so ``gh`` rejected every batch label fetch with
        ``Expected VALUE, actual: UNKNOWN_CHAR``. Pass owner/name as ``-F``
        variables instead, matching ``fetch_issue_review_comments``.
        """
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.state.review import fetch_all_issue_labels_graphql

        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("HomericIntelligence", "Hephaestus"),
            ),
            patch("hephaestus.automation.state.review._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout=json.dumps({"data": {"repository": {}}}))
            fetch_all_issue_labels_graphql([10, 11])

        argv = mock_gh.call_args.args[0]
        query_arg = next(a for a in argv if a.startswith("query="))
        # No single-quoted GraphQL string literals (invalid GraphQL).
        assert "'HomericIntelligence'" not in query_arg
        assert "'Hephaestus'" not in query_arg
        # owner/name supplied as declared variables, passed via -F.
        assert query_arg.startswith("query=query($owner:String!,$name:String!")
        assert "$owner" in query_arg and "$name" in query_arg
        assert "owner=HomericIntelligence" in argv
        assert "name=Hephaestus" in argv
        # Issue numbers are GraphQL variables too, not interpolated inline.
        assert "owner:'" not in query_arg
        assert "n0=10" in argv and "n1=11" in argv
        assert "issue(number:$n0)" in query_arg


class TestFetchAllIssueTitlesGraphql:
    """Batched title fetch used by the planner to detect epic/roadmap issues (#1669)."""

    def test_empty_input_returns_empty(self) -> None:
        from hephaestus.automation.state.review import fetch_all_issue_titles_graphql

        assert fetch_all_issue_titles_graphql([]) == {}

    def test_parses_aliased_titles(self) -> None:
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.state.review import fetch_all_issue_titles_graphql

        payload = {
            "data": {
                "repository": {
                    "issue0": {"title": "Epic: pipeline overhaul"},
                    "issue1": {"title": "Fix crash"},
                }
            }
        }
        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch("hephaestus.automation.state.review._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout=json.dumps(payload))
            result = fetch_all_issue_titles_graphql([10, 11])

        assert result == {10: "Epic: pipeline overhaul", 11: "Fix crash"}

    def test_fetch_failure_returns_empty_strings(self) -> None:
        from unittest.mock import patch

        from hephaestus.automation.state.review import fetch_all_issue_titles_graphql

        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.state.review._gh_call",
                side_effect=RuntimeError("gh down"),
            ),
        ):
            result = fetch_all_issue_titles_graphql([10, 11])

        assert result == {10: "", 11: ""}

    def test_owner_name_passed_as_graphql_variables_not_interpolated(self) -> None:
        """Title fetch must parameterize owner/name/numbers (no injection)."""
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.state.review import fetch_all_issue_titles_graphql

        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("HomericIntelligence", "Hephaestus"),
            ),
            patch("hephaestus.automation.state.review._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout=json.dumps({"data": {"repository": {}}}))
            fetch_all_issue_titles_graphql([10, 11])

        argv = mock_gh.call_args.args[0]
        query_arg = next(a for a in argv if a.startswith("query="))
        assert "'HomericIntelligence'" not in query_arg
        assert query_arg.startswith("query=query($owner:String!,$name:String!")
        assert "owner=HomericIntelligence" in argv
        assert "name=Hephaestus" in argv
        assert "n0=10" in argv and "n1=11" in argv
        assert "issue(number:$n0)" in query_arg


class TestFetchAllIssueCommentsGraphqlVars:
    """Regression guard for the batched comments fetch's GraphQL parameterization."""

    def test_comments_owner_name_passed_as_graphql_variables(self) -> None:
        """Same regression guard for the batched comments fetch."""
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.state.review import fetch_all_issue_comments_graphql

        with (
            patch("hephaestus.automation.state.review.get_repo_root", return_value="/tmp/repo"),
            patch(
                "hephaestus.automation.state.review.get_repo_info",
                return_value=("HomericIntelligence", "Hephaestus"),
            ),
            patch("hephaestus.automation.state.review._gh_call") as mock_gh,
        ):
            mock_gh.return_value = MagicMock(stdout=json.dumps({"data": {"repository": {}}}))
            fetch_all_issue_comments_graphql([10])

        argv = mock_gh.call_args.args[0]
        query_arg = next(a for a in argv if a.startswith("query="))
        assert "'HomericIntelligence'" not in query_arg
        assert query_arg.startswith("query=query($owner:String!,$name:String!")
        assert "$owner" in query_arg and "$name" in query_arg
        assert "owner=HomericIntelligence" in argv
        assert "name=Hephaestus" in argv
        # Issue number passed as a variable, not interpolated inline.
        assert "owner:'" not in query_arg
        assert "n0=10" in argv
        assert "issue(number:$n0)" in query_arg


class TestLatestVerdictProperties:
    """Property-based fuzz coverage for latest_verdict (#1470)."""

    @given(st.text())
    def test_never_raises_returns_go_nogo_or_none(self, body: str) -> None:
        assert latest_verdict(body) in {"GO", "NOGO", "BLOCKED", None}

    @given(st.text())
    def test_no_verdict_line_is_none(self, body: str) -> None:
        if "state:plan-" not in body.lower():
            assert latest_verdict(body) is None

    @given(st.lists(st.sampled_from(["go", "no-go", "blocked"]), min_size=1, max_size=6))
    def test_last_matching_line_wins(self, tokens: list[str]) -> None:
        body = "\n".join(f"state:plan-{token}" for token in tokens) + "\n"
        expected = {"go": "GO", "no-go": "NOGO", "blocked": "BLOCKED"}[tokens[-1]]
        assert latest_verdict(body) == expected
