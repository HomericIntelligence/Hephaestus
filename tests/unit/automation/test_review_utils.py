"""Tests for shared review utilities (_review_utils.py)."""

import argparse
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import _review_utils as review_utils, models
from hephaestus.automation._review_utils import (
    _discover_prs_simple,
    add_max_workers_arg,
    close_issue_as_covered,
    find_merged_closing_pr,
    find_pr_for_issue,
    get_pr_head_branch,
    load_impl_session_id,
    log_file_path,
    parse_json_block,
    print_worker_summary,
)
from hephaestus.automation.models import DEFAULT_WORKER_COUNT, WorkerResult

# ---------------------------------------------------------------------------
# log_file_path
# ---------------------------------------------------------------------------


class TestLogFilePath:
    """Tests for standard per-issue automation log paths."""

    def test_without_iteration(self, tmp_path: Path) -> None:
        assert log_file_path(tmp_path, "learn", 42) == tmp_path / "learn-42.log"

    def test_with_iteration(self, tmp_path: Path) -> None:
        assert log_file_path(tmp_path, "review", 42, iteration=3) == tmp_path / "review-42-r3.log"

    def test_prefix_can_include_hyphen(self, tmp_path: Path) -> None:
        assert (
            log_file_path(tmp_path, "pr-review-analysis", 42)
            == tmp_path / "pr-review-analysis-42.log"
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

    def test_invalid_json_with_custom_default_writes_trace(self, tmp_path: Path) -> None:
        """Malformed JSON with trace_dir writes the diagnostic payload."""
        default: dict[str, Any] = {"addressed": [], "replies": {}}
        text = "before\n```json\n{broken!!}\n```\nafter"

        result = parse_json_block(
            text,
            default=default,
            trace_dir=tmp_path,
            trace_name="address-123.parse-error.log",
        )

        assert result == default
        trace = tmp_path / "address-123.parse-error.log"
        assert trace.exists()
        payload = trace.read_text()
        assert "reason: json.JSONDecodeError:" in payload
        assert "=== last fenced block (if any) ===\n{broken!!}" in payload
        assert "=== full response ===\n" in payload
        assert text in payload

    def test_raw_json_fallback_invalid_returns_custom_default(self) -> None:
        """Invalid raw JSON fallback returns the caller's shape."""
        assert parse_json_block("{bad", default={}, raw_json_fallback=True) == {}

    def test_first_block_invalid_with_raw_fallback_does_not_use_later_block(self) -> None:
        """First-block mode preserves CI-driver semantics on invalid first blocks."""
        text = '```json\n{bad}\n```\n```json\n{"fixed": true}\n```'
        assert (
            parse_json_block(text, default={}, raw_json_fallback=True, use_last_block=False) == {}
        )

    def test_scalar_json_returns_parse_error_default(self) -> None:
        """Scalar JSON is not a dict result and uses the parse-error default."""
        result = parse_json_block("```json\n42\n```")
        assert result["summary"].startswith("Failed to parse")


class TestPrintWorkerSummary:
    """Tests for the shared worker summary logger."""

    @staticmethod
    def _worker_result(issue_number: int, success: bool, error: str | None = None) -> WorkerResult:
        return WorkerResult(issue_number=issue_number, success=success, error=error)

    def test_empty_results_logs_zero_counts_without_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty result sets log zero totals and omit the failure block."""
        with caplog.at_level(logging.INFO):
            print_worker_summary("PR Review Summary", {})

        text = "\n".join(caplog.messages)
        assert "PR Review Summary" in text
        assert "Total issues: 0" in text
        assert "Successful: 0" in text
        assert "Failed: 0" in text
        assert "Failed issues:" not in text

    def test_all_successful_results_log_success_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Successful result sets log the expected success tally."""
        results = {
            1: self._worker_result(1, True),
            2: self._worker_result(2, True),
        }

        with caplog.at_level(logging.INFO):
            print_worker_summary("Plan Review Summary", results)

        text = "\n".join(caplog.messages)
        assert "Plan Review Summary" in text
        assert "Total issues: 2" in text
        assert "Successful: 2" in text
        assert "Failed: 0" in text

    def test_mixed_results_log_failed_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        """Failed results are listed with their issue numbers and error text."""
        results = {
            1: self._worker_result(1, True),
            2: self._worker_result(2, False, "boom"),
        }

        with caplog.at_level(logging.INFO):
            print_worker_summary("CI Driver Summary", results)

        text = "\n".join(caplog.messages)
        assert "CI Driver Summary" in text
        assert "Successful: 1" in text
        assert "Failed: 1" in text
        assert "Failed issues:" in text
        assert "  #2: boom" in text

    def test_custom_count_noun_preserves_pr_summary_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Callers can preserve the existing PR-specific total label."""
        results = {1: self._worker_result(1, True)}

        with caplog.at_level(logging.INFO):
            print_worker_summary("PR Review Summary", results, count_noun="PRs")

        assert "Total PRs: 1" in caplog.messages

    def test_custom_failed_header_preserves_leading_newline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Callers can preserve summary methods that logged a blank line first."""
        results = {1: self._worker_result(1, False, "x")}

        with caplog.at_level(logging.INFO):
            print_worker_summary(
                "Address Review Summary",
                results,
                failed_header="\nFailed issues:",
            )

        assert "\nFailed issues:" in caplog.messages


class TestEnsureStateDir:
    """Tests for the canonical automation state-dir helper."""

    def test_ensure_state_dir_creates_default_state_dir_under_repo_root(
        self, tmp_path: Path
    ) -> None:
        """Default state dir is created under the provided repo root."""
        state_dir = review_utils.ensure_state_dir(tmp_path)

        assert models.DEFAULT_STATE_DIR == "build/.issue_implementer"
        assert state_dir == tmp_path / Path(models.DEFAULT_STATE_DIR)
        assert state_dir.is_dir()

    def test_ensure_state_dir_accepts_custom_subdir(self, tmp_path: Path) -> None:
        """Callers can override the subdir while reusing mkdir behavior."""
        state_dir = review_utils.ensure_state_dir(tmp_path, subdir="custom/state")

        assert state_dir == tmp_path / "custom" / "state"
        assert state_dir.is_dir()


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
# load_impl_session_id
# ---------------------------------------------------------------------------


class TestLoadImplSessionId:
    """Tests for the shared load_impl_session_id helper."""

    def test_returns_session_id_for_matching_claude(self, tmp_path: Path) -> None:
        """Legacy state without session_agent belongs to Claude."""
        (tmp_path / "issue-123.json").write_text(json.dumps({"session_id": "abc"}))

        assert load_impl_session_id(tmp_path, 123, "claude") == "abc"

    def test_skips_legacy_claude_session_for_codex(self, tmp_path: Path) -> None:
        """Legacy Claude session must not resume as Codex."""
        (tmp_path / "issue-123.json").write_text(json.dumps({"session_id": "abc"}))

        assert load_impl_session_id(tmp_path, 123, "codex") is None

    def test_returns_matching_codex_session(self, tmp_path: Path) -> None:
        """Codex sessions resume when the selected agent is Codex."""
        (tmp_path / "issue-123.json").write_text(
            json.dumps({"session_id": "codex-sess", "session_agent": "codex"})
        )

        assert load_impl_session_id(tmp_path, 123, "codex") == "codex-sess"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Missing implementer state returns None."""
        assert load_impl_session_id(tmp_path, 123, "claude") is None

    def test_null_session_id_returns_none(self, tmp_path: Path) -> None:
        """State with a null session_id returns None."""
        (tmp_path / "issue-123.json").write_text(json.dumps({"session_id": None}))

        assert load_impl_session_id(tmp_path, 123, "claude") is None

    def test_no_session_id_key_returns_none(self, tmp_path: Path) -> None:
        """State without session_id returns None."""
        (tmp_path / "issue-123.json").write_text(json.dumps({"phase": "completed"}))

        assert load_impl_session_id(tmp_path, 123, "claude") is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        """Unreadable JSON state returns None."""
        (tmp_path / "issue-123.json").write_text("{not json")

        assert load_impl_session_id(tmp_path, 123, "claude") is None

    def test_reads_issue_filename_not_legacy_state_name(self, tmp_path: Path) -> None:
        """Only the implementer-written issue filename is read."""
        (tmp_path / "state-123.json").write_text(json.dumps({"session_id": "legacy"}))

        assert load_impl_session_id(tmp_path, 123, "claude") is None

        (tmp_path / "issue-123.json").write_text(json.dumps({"session_id": "real"}))

        assert load_impl_session_id(tmp_path, 123, "claude") == "real"


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

    def test_body_search_query_uses_closes_in_body(self) -> None:
        """The body-search strategy queries exact Closes lines in PR bodies."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([]),
        ) as mock_gh:
            result = find_pr_for_issue(42)

        assert result is None
        search_calls = [call for call in mock_gh.call_args_list if "--search" in call.args[0]]
        assert search_calls
        assert "Closes #42 in:body" in search_calls[0].args[0]

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
        """Default case: uses the shared worker default."""
        parser = argparse.ArgumentParser()
        add_max_workers_arg(parser)
        args = parser.parse_args([])

        assert args.max_workers == DEFAULT_WORKER_COUNT
        assert f"default: {DEFAULT_WORKER_COUNT}" in parser.format_help()

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
