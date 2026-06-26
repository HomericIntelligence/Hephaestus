"""Tests for shared review utilities (_review_utils.py)."""

import argparse
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation._review_utils import (
    _discover_prs_simple,
    add_max_workers_arg,
    close_issue_as_covered,
    find_merged_closing_pr,
    find_pr_for_issue,
    get_pr_head_branch,
    parse_json_block,
)

# ---------------------------------------------------------------------------
# parse_json_block
# ---------------------------------------------------------------------------


class TestParseJsonBlock:
    """Tests for the shared parse_json_block helper."""

    def test_extracts_last_json_block(self) -> None:
        """Multiple ```json blocks → returns the last one parsed."""
        text = (
            '```json\n{"comments": ["first"], "summary": "first"}\n```\n'
            '```json\n{"comments": ["second"], "summary": "second"}\n```'
        )
        result = parse_json_block(text)
        assert result["summary"] == "second"

    def test_no_block_returns_defaults(self) -> None:
        """No json block → returns defaults with empty comments list."""
        result = parse_json_block("No json here.")
        assert result["comments"] == []
        assert "No structured output" in result["summary"]

    def test_invalid_json_returns_defaults(self) -> None:
        """Malformed json → returns defaults."""
        text = "```json\n{invalid!!}\n```"
        result = parse_json_block(text)
        assert result["comments"] == []
        assert "Failed to parse" in result["summary"]

    def test_valid_block(self) -> None:
        """Single valid block → parsed correctly."""
        payload = {"comments": [{"path": "a.py", "line": 1, "body": "x"}], "summary": "ok"}
        text = "```json\n" + json.dumps(payload) + "\n```"
        result = parse_json_block(text)
        assert result["summary"] == "ok"
        assert len(result["comments"]) == 1


# ---------------------------------------------------------------------------
# _discover_prs_simple
# ---------------------------------------------------------------------------


class TestDiscoverPrsSimple:
    """Tests for the shared issue-to-PR discovery helper."""

    def test_empty_input_returns_empty_without_calling_find(self) -> None:
        """Empty issue list returns an empty map without lookup calls."""
        find_fn = MagicMock(return_value=123)

        result = _discover_prs_simple([], find_fn)

        assert result == {}
        find_fn.assert_not_called()

    def test_discovers_prs_and_reports_missing_issues_in_order(self) -> None:
        """Found PRs are mapped while missing issues invoke the callback."""
        calls: list[int] = []
        missing: list[int] = []

        def find_fn(issue_number: int) -> int | None:
            calls.append(issue_number)
            return {1: 101, 3: 103}.get(issue_number)

        result = _discover_prs_simple([1, 2, 3], find_fn, on_missing=missing.append)

        assert result == {1: 101, 3: 103}
        assert calls == [1, 2, 3]
        assert missing == [2]


# ---------------------------------------------------------------------------
# find_pr_for_issue
# ---------------------------------------------------------------------------


def _make_gh_result(payload: Any) -> MagicMock:
    mock = MagicMock()
    mock.stdout = json.dumps(payload)
    return mock


class TestFindPrForIssue:
    """Tests for find_pr_for_issue helper."""

    def test_finds_via_branch_name(self) -> None:
        """Branch-name lookup succeeds → returns PR number immediately."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([{"number": 42}]),
        ):
            result = find_pr_for_issue(123)

        assert result == 42

    def test_falls_back_to_body_search(self) -> None:
        """Branch-name returns empty → falls back to body search."""
        call_count = 0

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "--head" in args:
                return _make_gh_result([])
            # Body search candidate — body must contain ``Closes #N`` on its
            # own line, matching pr-policy's exact-line gate.
            return _make_gh_result([{"number": 99, "body": "Summary.\n\nCloses #123\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(123)

        assert result == 99
        assert call_count == 2

    def test_body_search_rejects_substring_match(self) -> None:
        """Body containing ``Closes #1234`` must NOT match a query for issue #12."""

        # Regression for #826: GitHub's full-text search returns substring
        # matches, so a PR whose body says ``Closes #1234`` would be returned
        # for ``Closes #12 in:body`` queries. Without the regex post-filter
        # the driver would resolve issue #12 to the wrong PR.
        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            if "--head" in args:
                return _make_gh_result([])
            return _make_gh_result([{"number": 9999, "body": "Closes #1234\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(12)

        assert result is None

    def test_body_search_rejects_grouped_closes(self) -> None:
        """``Closes #12, #18, #28`` (one-line grouped list) does not match."""

        # The grouped form is what the strict-audit tracking PRs use. They
        # mention many issue numbers on one line, but pr-policy requires
        # each Closes on its own line, so we should not resolve any of those
        # issues to such a PR via Strategy 3.
        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            if "--head" in args:
                return _make_gh_result([])
            return _make_gh_result([{"number": 5000, "body": "Closes #12, #18, #28, #29\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(28)

        assert result is None

    def test_body_search_picks_exact_match_among_candidates(self) -> None:
        """Among multiple candidates, the first one with a real Closes line wins."""

        # The first candidate has only a substring match; the second one is
        # the real PR. The fix must skip the bogus first candidate.
        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            if "--head" in args:
                return _make_gh_result([])
            return _make_gh_result(
                [
                    {"number": 9999, "body": "Closes #1234\n"},  # substring
                    {"number": 71, "body": "Closes #12\n"},  # exact
                ]
            )

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(12)

        assert result == 71

    def test_returns_none_when_nothing_found(self) -> None:
        """All strategies return empty → None."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([]),
        ):
            result = find_pr_for_issue(123)

        assert result is None

    def test_extra_strategies_uses_review_state(self) -> None:
        """extra_strategies=True checks review state when branch-name fails."""
        # Branch-name returns empty; review-state lookup succeeds
        call_results = [
            _make_gh_result([]),  # branch-name: no match
            _make_gh_result({"state": "OPEN", "number": 55}),  # gh pr view
        ]
        call_iter = iter(call_results)

        review_state = MagicMock()
        review_state.pr_number = 55

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=lambda *a, **kw: next(call_iter),
        ):
            result = find_pr_for_issue(
                123,
                extra_strategies=True,
                _load_review_state_fn=lambda: review_state,
            )

        assert result == 55

    def test_extra_strategies_skips_closed_pr(self) -> None:
        """extra_strategies=True: review-state PR is closed → fall through to body search."""
        call_results = [
            _make_gh_result([]),  # branch-name: empty
            _make_gh_result({"state": "CLOSED", "number": 55}),  # gh pr view: closed
            _make_gh_result([{"number": 77, "body": "Closes #123\n"}]),  # body search: match
        ]
        call_iter = iter(call_results)

        review_state = MagicMock()
        review_state.pr_number = 55

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=lambda *a, **kw: next(call_iter),
        ):
            result = find_pr_for_issue(
                123,
                extra_strategies=True,
                _load_review_state_fn=lambda: review_state,
            )

        assert result == 77

    def test_branch_name_gh_error_falls_back(self) -> None:
        """Branch-name lookup raises → falls back gracefully."""
        import subprocess

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            if "--head" in args:
                raise subprocess.CalledProcessError(1, "gh")
            return _make_gh_result([{"number": 10, "body": "Closes #123\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_pr_for_issue(123)

        assert result == 10


class TestGetPrHeadBranch:
    """get_pr_head_branch returns the PR's REAL head branch, not an assumption.

    Regression for the wrong-branch bug: the loop assumed ``{issue}-auto-impl``
    and ran ``git fetch origin {issue}-auto-impl`` which fails (exit 128) when
    the existing PR was opened from a differently-named branch (e.g. found via
    body ``Closes #N`` search, not the branch-name strategy).
    """

    def test_returns_real_head_ref_name(self) -> None:
        """Reads headRefName from `gh pr view` and returns it verbatim."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result({"headRefName": "708-auto-impl"}),
        ):
            assert get_pr_head_branch(996) == "708-auto-impl"

    def test_returns_none_on_missing_field(self) -> None:
        """Empty/absent headRefName → None so the caller can fall back safely."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result({}),
        ):
            assert get_pr_head_branch(996) is None

    def test_returns_none_on_gh_failure(self) -> None:
        """A gh/parse failure degrades to None, never raises."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=RuntimeError("gh boom"),
        ):
            assert get_pr_head_branch(996) is None


# ---------------------------------------------------------------------------
# add_max_workers_arg
# ---------------------------------------------------------------------------


class TestAddMaxWorkersArg:
    """Tests for the shared add_max_workers_arg helper."""

    def test_default_help_and_value(self) -> None:
        """Default case: uses standard help text and default=3."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser)
        args = parser.parse_args([])
        assert args.max_workers == 3

    def test_custom_help_text(self) -> None:
        """Custom help_text is used in the parser."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser, help_text="custom help")
        assert "custom help" in parser.format_help()

    def test_accepts_valid_values(self) -> None:
        """Valid range 1-32 accepted."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser)
        args = parser.parse_args(["--max-workers", "16"])
        assert args.max_workers == 16

    @pytest.mark.parametrize("bad", ["0", "-1", "33", "100"])
    def test_rejects_out_of_range(self, bad: str) -> None:
        """Out-of-range values 0, -1, 33+ are rejected with exit code 2."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser)
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--max-workers", bad])
        assert excinfo.value.code == 2

    def test_custom_default(self) -> None:
        """Custom default value is honored."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser, default=8)
        args = parser.parse_args([])
        assert args.max_workers == 8


# ---------------------------------------------------------------------------
# find_merged_closing_pr
# ---------------------------------------------------------------------------


class TestFindMergedClosingPr:
    """Tests for find_merged_closing_pr — the merged-PR coverage gate (FM1)."""

    def test_finds_merged_pr_with_exact_closes_line(self) -> None:
        """A merged PR whose body has ``Closes #N`` on its own line is returned."""
        captured: dict[str, list[str]] = {}

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            captured["args"] = args
            return _make_gh_result([{"number": 1358, "body": "Summary.\n\nCloses #1357\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_closing_pr(1357)

        assert result == 1358
        # Must search the MERGED state, not open.
        assert "--state" in captured["args"]
        assert "merged" in captured["args"]

    def test_rejects_substring_match(self) -> None:
        """``Closes #1234`` must NOT match a query for issue #12."""

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            return _make_gh_result([{"number": 9999, "body": "Closes #1234\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_closing_pr(12)

        assert result is None

    def test_rejects_grouped_closes(self) -> None:
        """A grouped one-line ``Closes #12, #18, #28`` does not match #28."""

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            return _make_gh_result([{"number": 5000, "body": "Closes #12, #18, #28, #29\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_closing_pr(28)

        assert result is None

    def test_returns_none_when_no_merged_pr(self) -> None:
        """No merged PR found → None."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([]),
        ):
            result = find_merged_closing_pr(1357)

        assert result is None

    def test_gh_failure_returns_none(self) -> None:
        """A gh failure is swallowed and returns None (no crash)."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=RuntimeError("gh boom"),
        ):
            result = find_merged_closing_pr(1357)

        assert result is None


# ---------------------------------------------------------------------------
# close_issue_as_covered
# ---------------------------------------------------------------------------


class TestCloseIssueAsCovered:
    """Tests for close_issue_as_covered."""

    def test_runs_gh_issue_close_with_comment(self) -> None:
        """Closes the issue with a comment citing the merged PR."""
        captured: dict[str, list[str]] = {}

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            captured["args"] = args
            return MagicMock()

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = close_issue_as_covered(1357, 1358)

        assert result is True
        assert captured["args"][:3] == ["issue", "close", "1357"]
        assert "--comment" in captured["args"]
        comment = captured["args"][captured["args"].index("--comment") + 1]
        assert "PR #1358" in comment
        assert "Closes #1357" in comment

    def test_gh_failure_returns_false(self) -> None:
        """A gh failure is swallowed and returns False (no crash)."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=RuntimeError("gh boom"),
        ):
            result = close_issue_as_covered(1357, 1358)

        assert result is False
