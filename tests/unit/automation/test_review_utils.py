"""Test current queue parsing, state directories, and GitHub query helpers."""

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import models
from hephaestus.automation._review_utils import (
    add_max_workers_arg,
    close_issue_as_covered,
    ensure_state_dir,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    find_pr_for_issue,
    get_pr_head_branch,
    parse_json_block,
)
from hephaestus.automation.models import DEFAULT_WORKER_COUNT


class TestEnsureStateDir:
    """Tests for the shared automation state directory helper."""

    def test_default_state_dir_literal(self) -> None:
        """Default state directory path remains the existing on-disk layout."""
        assert models.DEFAULT_STATE_DIR == "build/.issue_implementer"

    def test_creates_default_state_dir(self, tmp_path: Path) -> None:
        """Default helper creates and returns the repo-local state directory."""
        state_dir = ensure_state_dir(tmp_path)

        assert state_dir == tmp_path / models.DEFAULT_STATE_DIR
        assert state_dir.is_dir()

    def test_creates_custom_subdir(self, tmp_path: Path) -> None:
        """A custom subdir keeps the helper reusable for alternate state roots."""
        state_dir = ensure_state_dir(tmp_path, "custom/state")

        assert state_dir == tmp_path / "custom/state"
        assert state_dir.is_dir()


def test_issue_implementer_state_dir_literal_is_centralized() -> None:
    """Only the canonical helper and literal-pin test may spell issue_implementer."""
    import ast

    expected_paths = {
        Path("hephaestus/automation/models.py"),
        Path("tests/unit/automation/test_review_utils.py"),
    }
    hits: list[tuple[Path, int, str]] = []
    for base in (Path("hephaestus/automation"), Path("tests/unit/automation")):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "issue_implementer" in node.value
                ):
                    hits.append((path, node.lineno, node.value))

    bad = [hit for hit in hits if hit[0] not in expected_paths]
    assert not bad
    assert {path for path, _, _ in hits} == expected_paths


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

    def test_json_string_containing_fence_marker_does_not_close_block(self) -> None:
        """Fence markers inside JSON strings are content, not closing fences."""
        payload = {"comments": [], "summary": "contains ``` marker"}
        text = "```json\n" + json.dumps(payload) + "\n```"
        result = parse_json_block(text)
        assert result["summary"] == payload["summary"]

    def test_scalar_json_returns_parse_error_default(self) -> None:
        """Scalar JSON is not a dict result and uses the parse-error default."""
        result = parse_json_block("```json\n42\n```")
        assert result["summary"].startswith("Failed to parse")


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


class TestFindMergedClosingPr:
    """Tests for find_merged_closing_pr — the merged-PR coverage gate (FM1)."""

    def test_finds_merged_pr_with_exact_closes_line(self) -> None:
        """A merged PR whose body has ``Closes #N`` on its own line is returned."""
        captured: dict[str, list[str]] = {}

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            captured["args"] = args
            return _make_gh_result([{"number": 1358, "body": "Summary.\r\n\r\nCloses #1357\r\n"}])

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

    @pytest.mark.parametrize(
        "body",
        [
            "Closes #12, #18\n",
            "Closes #12-extra\n",
            "Closes #12: reason\n",
            "Closes #12 trailing\n",
        ],
    )
    def test_rejects_trailing_text_after_target_issue(self, body: str) -> None:
        """The target number must occupy the complete policy line."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([{"number": 5000, "body": body}]),
        ):
            assert find_merged_closing_pr(12) is None

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


class TestFindMergedPrForIssue:
    """Tests for find_merged_pr_for_issue — the tri-state fetch's merged lookup."""

    def test_finds_via_exact_merged_closing_line(self) -> None:
        """Only an exact closing line can associate merged work to an issue."""
        captured: dict[str, list[str]] = {}

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            captured["args"] = args
            return _make_gh_result([{"number": 43, "body": "Closes #7\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_pr_for_issue(7)

        assert result == 43
        assert "--search" in captured["args"]
        assert "--head" not in captured["args"]
        assert "merged" in captured["args"]

    def test_uses_exact_merged_body_search(self) -> None:
        """The sole merged lookup delegates to exact body search."""

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            return _make_gh_result([{"number": 44, "body": "Fix.\n\nCloses #7\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_pr_for_issue(7)

        assert result == 44

    @pytest.mark.parametrize(
        "body",
        ["Closes #7, #8\n", "Closes #7: reason\n", "Closes #7 trailing\n"],
    )
    def test_rejects_trailing_text_after_target_issue(self, body: str) -> None:
        """Merged lookup follows the same whole-line PR policy contract."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([{"number": 44, "body": body}]),
        ):
            assert find_merged_pr_for_issue(7) is None

    def test_returns_none_when_no_merged_pr(self) -> None:
        """Neither strategy finds a merged PR → None (closed PRs stay invisible)."""
        with patch(
            "hephaestus.automation._review_utils._gh_call",
            return_value=_make_gh_result([]),
        ):
            result = find_merged_pr_for_issue(7)

        assert result is None

    def test_exact_body_search_does_not_probe_branch_name(self) -> None:
        """Branch names cannot turn a merged PR into completion evidence."""
        calls: list[list[str]] = []

        def _side_effect(args: list[str], **kw: Any) -> MagicMock:
            calls.append(args)
            return _make_gh_result([{"number": 45, "body": "Closes #7\n"}])

        with patch(
            "hephaestus.automation._review_utils._gh_call",
            side_effect=_side_effect,
        ):
            result = find_merged_pr_for_issue(7)

        assert result == 45
        assert len(calls) == 1
        assert "--head" not in calls[0]


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
