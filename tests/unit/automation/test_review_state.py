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
    PLAN_REVIEW_PREFIX,
    VERDICT_APPROVED,
    VERDICT_BLOCK,
    VERDICT_REVISE,
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
# is_plan_review_approved (with pre-supplied comments)
# ---------------------------------------------------------------------------


def _review_comment(verdict: str | None) -> dict[str, Any]:
    if verdict is None:
        body = f"{PLAN_REVIEW_PREFIX}\n\nMalformed review with no verdict line.\n"
    else:
        body = f"{PLAN_REVIEW_PREFIX}\n\nBody.\n\n**Verdict: {verdict}**\n"
    return {"body": body}


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
