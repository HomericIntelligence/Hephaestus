"""Tests for the follow_up module (consolidated-issue policy, 2026-05-10)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from hephaestus.automation.follow_up import (
    FollowUpItem,
    FollowUpResponse,
    RejectedItem,
    parse_follow_up_items,
    parse_follow_up_response,
    render_rejected_for_pr_body,
    run_follow_up_issues,
)


class TestParseFollowUpResponse:
    """Tests for parse_follow_up_response (the new sectioned schema)."""

    def test_parses_fenced_json_object(self) -> None:
        text = (
            "```json\n"
            '{"follow_ups": [{"category": "core", "title": "T1", "body": "B1"}],'
            ' "rejected": [{"title": "R1", "reason": "out of scope"}]}\n'
            "```"
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].category == "core"
        assert response.follow_ups[0].title == "T1"
        assert response.follow_ups[0].body == "B1"
        assert len(response.rejected) == 1
        assert response.rejected[0].title == "R1"
        assert response.rejected[0].reason == "out of scope"

    def test_parses_bare_json_object(self) -> None:
        text = (
            "Some prose...\n"
            '{"follow_ups": [{"category": "safety", "title": "T", "body": "B"}],'
            ' "rejected": []}'
            "\nmore prose"
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].category == "safety"

    def test_returns_empty_when_no_json(self) -> None:
        response = parse_follow_up_response("No JSON here.")
        assert response.follow_ups == []
        assert response.rejected == []

    def test_returns_empty_when_root_not_object(self) -> None:
        response = parse_follow_up_response("[1, 2, 3]")
        assert response.follow_ups == []
        assert response.rejected == []

    def test_returns_empty_on_invalid_json(self) -> None:
        response = parse_follow_up_response("{not valid")
        assert response.follow_ups == []

    def test_parses_fenced_json_with_nested_object(self) -> None:
        r"""Regression: the fenced regex must not truncate on inner ``}``.

        Locks in that ``r"```(?:json)?\s*(\{.*?\})\s*```"`` correctly
        spans nested objects because ``.*?`` is anchored by the trailing
        fence literal.
        """
        text = (
            "```json\n"
            '{"follow_ups": [{"category": "core", "title": "T",'
            ' "body": "B", "extra": {"nested": "val"}}],'
            ' "rejected": []}\n'
            "```"
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].title == "T"

    def test_parses_bare_json_with_trailing_text(self) -> None:
        """``raw_decode`` must stop at the closing ``}`` of the first object.

        Mirrors the prior balancer's behavior of returning early once
        the outer brace closes, ignoring any garbage that follows.
        """
        text = (
            '{"follow_ups": [{"category": "core", "title": "T",'
            ' "body": "B"}], "rejected": []}'
            "\nsome trailing log line that is not JSON"
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].title == "T"

    def test_parses_bare_json_with_leading_whitespace_before_brace(self) -> None:
        """Prose followed by blank lines before the JSON must still parse."""
        text = (
            "Some prose explaining the response.\n"
            "\n   \n"
            '{"follow_ups": [{"category": "security", "title": "T",'
            ' "body": "B"}], "rejected": []}'
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].category == "security"

    def test_caps_at_three_items(self) -> None:
        items = [{"category": "core", "title": f"T{i}", "body": f"B{i}"} for i in range(10)]
        text = json.dumps({"follow_ups": items, "rejected": []})
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 3

    def test_invalid_category_demoted_to_rejected(self) -> None:
        text = json.dumps(
            {
                "follow_ups": [
                    {"category": "enhancement", "title": "Bad cat", "body": "..."},
                    {"category": "core", "title": "Good", "body": "..."},
                ],
                "rejected": [],
            }
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].title == "Good"
        assert len(response.rejected) == 1
        assert response.rejected[0].title == "Bad cat"
        assert "enhancement" in response.rejected[0].reason

    def test_skips_items_missing_required_fields(self) -> None:
        text = json.dumps(
            {
                "follow_ups": [
                    {"category": "core", "title": "Good", "body": "Body"},
                    {"category": "core", "title": "No body"},
                    {"category": "core", "body": "No title"},
                    {"category": "core", "title": "", "body": "Empty title"},
                ],
                "rejected": [],
            }
        )
        response = parse_follow_up_response(text)
        assert len(response.follow_ups) == 1
        assert response.follow_ups[0].title == "Good"

    def test_handles_non_list_follow_ups_gracefully(self) -> None:
        text = json.dumps({"follow_ups": "not a list", "rejected": []})
        response = parse_follow_up_response(text)
        assert response.follow_ups == []

    def test_skips_rejected_items_with_missing_title(self) -> None:
        text = json.dumps(
            {
                "follow_ups": [],
                "rejected": [
                    {"title": "Good", "reason": "r"},
                    {"reason": "no title"},
                    {"title": "", "reason": "empty"},
                ],
            }
        )
        response = parse_follow_up_response(text)
        assert len(response.rejected) == 1
        assert response.rejected[0].title == "Good"


class TestParseFollowUpItemsLegacyAdapter:
    """The legacy adapter must keep returning a flat dict list for older callers."""

    def test_projects_to_legacy_shape(self) -> None:
        text = json.dumps(
            {
                "follow_ups": [
                    {"category": "security", "title": "T", "body": "B"},
                ],
                "rejected": [],
            }
        )
        items = parse_follow_up_items(text)
        assert len(items) == 1
        assert items[0]["title"] == "T"
        assert items[0]["body"] == "B"
        assert "follow-up" in items[0]["labels"]
        assert "security" in items[0]["labels"]

    def test_returns_empty_on_no_json(self) -> None:
        assert parse_follow_up_items("No JSON here.") == []


class TestRunFollowUpIssues:
    """Tests for run_follow_up_issues (consolidated-issue policy)."""

    def _make_claude_output(self, payload: dict[str, Any]) -> str:
        return json.dumps({"result": json.dumps(payload)})

    def test_files_one_consolidated_issue(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        payload = {
            "follow_ups": [
                {"category": "core", "title": "C1", "body": "BC1"},
                {"category": "safety", "title": "S1", "body": "BS1"},
            ],
            "rejected": [],
        }
        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output(payload)

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch(
                "hephaestus.automation.follow_up.gh_issue_create", return_value=999
            ) as mock_create,
            patch("hephaestus.automation.follow_up.gh_issue_comment") as mock_comment,
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        assert response is not None
        assert len(response.follow_ups) == 2
        # Exactly ONE issue filed regardless of item count
        mock_create.assert_called_once()
        title = mock_create.call_args.kwargs["title"]
        body = mock_create.call_args.kwargs["body"]
        labels = mock_create.call_args.kwargs["labels"]
        assert "Follow-up from #42" in title
        assert "## Core library" in body
        assert "## Safety" in body
        assert "follow-up" in labels
        assert "core" in labels
        assert "safety" in labels
        # Summary comment posted on parent
        mock_comment.assert_called_once()
        assert "#999" in mock_comment.call_args.args[1]

    def test_codex_skips_legacy_claude_session(self, tmp_path: Path) -> None:
        """Legacy sessions must not be resumed through Codex."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with patch("hephaestus.automation.follow_up.resume_codex_session") as mock_resume:
            response = run_follow_up_issues(
                "sess",
                worktree_path,
                42,
                tmp_path,
                agent="codex",
            )

        assert response is None
        mock_resume.assert_not_called()
        assert (tmp_path / "follow-up-42.log").read_text().startswith("FAILED:")

    def test_no_items_skips_issue_creation(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output({"follow_ups": [], "rejected": []})

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create") as mock_create,
            patch("hephaestus.automation.follow_up.gh_issue_comment") as mock_comment,
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        assert response is not None
        mock_create.assert_not_called()
        mock_comment.assert_not_called()

    def test_persists_rejected_list(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        payload = {
            "follow_ups": [
                {"category": "core", "title": "Real", "body": "..."},
            ],
            "rejected": [
                {"title": "Web dashboard", "reason": "Feature expansion."},
                {"title": "README polish", "reason": "Doc polish."},
            ],
        }
        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output(payload)

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create", return_value=1234),
            patch("hephaestus.automation.follow_up.gh_issue_comment"),
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        rejected_path = tmp_path / "follow-up-rejected-42.json"
        assert rejected_path.exists()
        persisted = json.loads(rejected_path.read_text())
        assert len(persisted) == 2
        assert persisted[0]["title"] == "Web dashboard"
        # Returned response carries the rejected list too
        assert response is not None
        assert len(response.rejected) == 2

    def test_dry_run_suppresses_github_calls(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        payload = {
            "follow_ups": [
                {"category": "core", "title": "T", "body": "B"},
            ],
            "rejected": [],
        }
        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output(payload)

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create") as mock_create,
            patch("hephaestus.automation.follow_up.gh_issue_comment") as mock_comment,
        ):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path, dry_run=True)

        mock_create.assert_not_called()
        mock_comment.assert_not_called()

    def test_failure_writes_log_and_does_not_raise(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with patch(
            "hephaestus.automation.follow_up.run",
            side_effect=RuntimeError("claude failed"),
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        assert response is None
        log_file = tmp_path / "follow-up-42.log"
        assert log_file.exists()
        assert log_file.read_text().startswith("FAILED:")

    def test_cleans_up_prompt_file_on_success(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output({"follow_ups": [], "rejected": []})

        with patch("hephaestus.automation.follow_up.run", return_value=mock_result):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        prompt_file = worktree_path / ".claude-followup-42.md"
        assert not prompt_file.exists()

    def test_cleans_up_prompt_file_on_failure(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with patch(
            "hephaestus.automation.follow_up.run",
            side_effect=RuntimeError("fail"),
        ):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        prompt_file = worktree_path / ".claude-followup-42.md"
        assert not prompt_file.exists()

    def test_failure_log_includes_exception_type(self, tmp_path: Path) -> None:
        """Acceptance: error log records exception class name for observability."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        with patch(
            "hephaestus.automation.follow_up.run",
            side_effect=RuntimeError("claude failed"),
        ):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path)
        log_text = (tmp_path / "follow-up-42.log").read_text()
        assert log_text.startswith("FAILED: [RuntimeError]")
        assert "TRACEBACK:" in log_text

    def test_unexpected_exception_logged_at_error(self, tmp_path: Path, caplog: Any) -> None:
        """Acceptance: programmer bugs (AttributeError etc.) surface at ERROR with exc_info."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        with (
            caplog.at_level("WARNING", logger="hephaestus.automation.follow_up"),
            patch(
                "hephaestus.automation.follow_up.run",
                side_effect=AttributeError("bug"),
            ),
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)
        assert response is None  # safety contract still holds
        error_records = [
            r
            for r in caplog.records
            if r.levelname == "ERROR" and r.name == "hephaestus.automation.follow_up"
        ]
        assert error_records, "expected an ERROR-level record for unexpected exception"
        assert any("AttributeError" in r.getMessage() for r in error_records)
        assert any(r.exc_info is not None for r in error_records)

    def test_expected_exception_logged_at_warning(self, tmp_path: Path, caplog: Any) -> None:
        """Acceptance: known pipeline failures (CalledProcessError) stay at WARNING."""
        import subprocess

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        with (
            caplog.at_level("WARNING", logger="hephaestus.automation.follow_up"),
            patch(
                "hephaestus.automation.follow_up.run",
                side_effect=subprocess.CalledProcessError(1, "claude"),
            ),
        ):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path)
        follow_up_records = [
            r for r in caplog.records if r.name == "hephaestus.automation.follow_up"
        ]
        warning_records = [r for r in follow_up_records if r.levelname == "WARNING"]
        error_records = [r for r in follow_up_records if r.levelname == "ERROR"]
        assert warning_records, "expected at least one WARNING record"
        assert not error_records, "expected pipeline failure must not escalate to ERROR"
        assert any(r.exc_info is not None for r in warning_records)

    def test_status_tracker_updated_once_for_consolidated_issue(self, tmp_path: Path) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        payload = {
            "follow_ups": [
                {"category": "core", "title": "T1", "body": "B1"},
                {"category": "core", "title": "T2", "body": "B2"},
            ],
            "rejected": [],
        }
        mock_result = MagicMock()
        mock_result.stdout = self._make_claude_output(payload)
        mock_tracker = MagicMock()

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create", return_value=201),
            patch("hephaestus.automation.follow_up.gh_issue_comment"),
        ):
            run_follow_up_issues("sess", worktree_path, 42, tmp_path, mock_tracker, slot_id=1)

        # New policy: tracker is updated ONCE for the consolidated filing,
        # not once per item.
        mock_tracker.update_slot.assert_called_once()
        assert "consolidated" in mock_tracker.update_slot.call_args.args[1]


class TestRenderRejectedForPRBody:
    """Tests for render_rejected_for_pr_body."""

    def test_returns_empty_when_none_rejected(self) -> None:
        assert render_rejected_for_pr_body([]) == ""

    def test_renders_markdown_section(self) -> None:
        rejected = [
            RejectedItem(title="Add web dashboard", reason="Feature expansion."),
            RejectedItem(title="README polish", reason="Doc polish."),
        ]
        rendered = render_rejected_for_pr_body(rejected)
        assert "## Considered & rejected follow-ups" in rendered
        assert "Add web dashboard" in rendered
        assert "Feature expansion." in rendered
        assert "README polish" in rendered


class TestDataclasses:
    """Tests for the frozen dataclasses exposed by follow_up."""

    def test_follow_up_item_is_frozen(self) -> None:
        item = FollowUpItem(category="core", title="T", body="B")
        try:
            item.title = "other"  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("FollowUpItem should be frozen")

    def test_follow_up_response_defaults(self) -> None:
        r = FollowUpResponse()
        assert r.follow_ups == []
        assert r.rejected == []


class TestRunFollowUpIsErrorHandling:
    """A2-006: run_follow_up_issues must detect is_error=True and handle it without crashing."""

    def test_is_error_true_returns_none(self, tmp_path: Path) -> None:
        """Claude returning is_error=true must cause run_follow_up_issues to return None."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        # Simulate Claude JSON output with is_error=True (no usage-cap reset epoch)
        error_output = json.dumps({"is_error": True, "result": "Something went wrong"})
        mock_result = MagicMock()
        mock_result.stdout = error_output

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create") as mock_create,
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        assert response is None
        mock_create.assert_not_called()

    def test_is_error_with_usage_cap_waits(self, tmp_path: Path) -> None:
        """is_error=True with a quota-reset epoch must call wait_until before returning None."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        # Simulate a usage-cap message that detect_claude_usage_cap would parse.
        # We patch detect_claude_usage_cap to return a future epoch.
        import time

        future_epoch = int(time.time()) + 3600
        error_output = json.dumps(
            {"is_error": True, "result": "out of extra usage · resets at ..."}
        )
        mock_result = MagicMock()
        mock_result.stdout = error_output

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch(
                "hephaestus.automation.follow_up.detect_claude_usage_cap",
                return_value=future_epoch,
            ),
            patch("hephaestus.automation.follow_up.wait_until") as mock_wait,
            patch("hephaestus.automation.follow_up.gh_issue_create") as mock_create,
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        assert response is None
        mock_wait.assert_called_once_with(future_epoch)
        mock_create.assert_not_called()

    def test_is_error_false_proceeds_normally(self, tmp_path: Path) -> None:
        """is_error=False (or absent) must not trigger the error path."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        payload: dict[str, list[Any]] = {"follow_ups": [], "rejected": []}
        normal_output = json.dumps({"is_error": False, "result": json.dumps(payload)})
        mock_result = MagicMock()
        mock_result.stdout = normal_output

        with (
            patch("hephaestus.automation.follow_up.run", return_value=mock_result),
            patch("hephaestus.automation.follow_up.gh_issue_create") as mock_create,
        ):
            response = run_follow_up_issues("sess", worktree_path, 42, tmp_path)

        # No error — response parsed normally
        assert response is not None
        mock_create.assert_not_called()  # no items means no issue created
