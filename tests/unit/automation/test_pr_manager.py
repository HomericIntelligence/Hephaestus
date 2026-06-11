"""Tests for hephaestus.automation.pr_manager.

Covers commit_changes filtering of secret files, ensure_pr_created
fallback paths, and create_pr argument shape — all via mocked subprocess
and GitHub-API calls.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import pr_manager


def _status(stdout: str = "", returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, returncode=returncode)


class TestCommitChanges:
    """Tests for commit changes."""

    def test_no_changes_raises(self) -> None:
        with patch.object(pr_manager, "run", return_value=_status("")):
            with pytest.raises(RuntimeError, match="No changes to commit"):
                pr_manager.commit_changes(1, Path("/tmp/wt"))

    def test_only_secret_files_raises(self) -> None:
        porcelain = "?? .env\n?? id_rsa\n M secrets/foo.key\n"
        with patch.object(pr_manager, "run", return_value=_status(porcelain)):
            with pytest.raises(RuntimeError, match="All changes appear to be secret"):
                pr_manager.commit_changes(2, Path("/tmp/wt"))

    def test_filters_secrets_and_commits(self) -> None:
        porcelain = " M src/foo.py\n?? .env\n?? data.key\n M src/bar.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add foo")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
        ):
            pr_manager.commit_changes(3, Path("/tmp/wt"))

        # git add must include the .py files but not .env or .key
        add_call = run_mock.call_args_list[1].args[0]
        assert "src/foo.py" in add_call
        assert "src/bar.py" in add_call
        assert ".env" not in add_call
        assert "data.key" not in add_call

    def test_handles_renamed_files(self) -> None:
        porcelain = "R  old.py -> new.py\n"
        run_mock = MagicMock(side_effect=[_status(porcelain), _status(""), _status("")])
        issue = MagicMock(title="Rename")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
        ):
            pr_manager.commit_changes(4, Path("/tmp/wt"))
        add_call = run_mock.call_args_list[1].args[0]
        assert "new.py" in add_call


class TestEnsurePRCreated:
    """Tests for ensure p r created."""

    def test_no_commit_raises(self) -> None:
        with patch.object(pr_manager, "run", return_value=_status("")):
            with pytest.raises(RuntimeError, match="No commit found"):
                pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt"))

    def test_empty_diff_vs_base_raises_before_pr_create(self) -> None:
        """A commit exists but the branch has no commits vs base → no PR created.

        Regression for the opaque, retried "No commits between main and
        <branch>" failure: detect the empty-diff branch up front and raise a
        clear message instead of letting `gh pr create` fail.
        """
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log (a commit exists)
                _status("origin/master"),  # default base branch (guard)
                _status("0"),  # rev-list count vs base → no net change
            ]
        )
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "create_pr") as create_mock,
        ):
            with pytest.raises(RuntimeError, match="No changes produced"):
                pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt"))
        create_mock.assert_not_called()

    def test_returns_existing_pr(self) -> None:
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log
                _status("origin/master"),  # default base branch (guard)
                _status("2"),  # rev-list count vs base (has commits)
                _status("refs/heads/branch"),  # ls-remote (already pushed)
            ]
        )
        gh_mock = _status('[{"number": 99}]')
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "_gh_call", return_value=gh_mock),
        ):
            assert pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt")) == 99

    def test_creates_pr_when_missing(self) -> None:
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log
                _status("origin/master"),  # default base branch (guard)
                _status("3"),  # rev-list count vs base (has commits)
                _status(""),  # ls-remote (not pushed)
                _status(""),  # git push
            ]
        )
        gh_mock = _status("[]")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "_gh_call", return_value=gh_mock),
            patch.object(pr_manager, "create_pr", return_value=42) as create_mock,
        ):
            assert pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt")) == 42
            create_mock.assert_called_once_with(
                1, "branch", auto_merge=False, agent="claude", base="master"
            )

    def test_creates_pr_with_selected_agent_metadata(self) -> None:
        run_mock = MagicMock(
            side_effect=[
                _status("abc1234 commit msg"),  # git log
                _status("origin/master"),  # default base branch (guard)
                _status("1"),  # rev-list count vs base (has commits)
                _status("refs/heads/branch"),  # ls-remote (already pushed)
            ]
        )
        gh_mock = _status("[]")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "_gh_call", return_value=gh_mock),
            patch.object(pr_manager, "create_pr", return_value=42) as create_mock,
        ):
            assert pr_manager.ensure_pr_created(1, "branch", Path("/tmp/wt"), agent="codex") == 42
            create_mock.assert_called_once_with(
                1, "branch", auto_merge=False, agent="codex", base="master"
            )


class TestCreatePR:
    """Tests for create p r."""

    def test_invokes_gh_pr_create(self) -> None:
        issue = MagicMock(title="Add feature X")
        with (
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "gh_pr_create", return_value=7) as gh_mock,
        ):
            assert pr_manager.create_pr(5, "branch", auto_merge=True, agent="codex") == 7
        kwargs = gh_mock.call_args.kwargs
        assert kwargs["branch"] == "branch"
        assert "Add feature X" in kwargs["title"]
        assert kwargs["auto_merge"] is True
        assert kwargs["base"] == "main"
        assert "Closes #5" in kwargs["body"]
        assert "Automated implementation via Codex" in kwargs["body"]
        assert "Claude Code" not in kwargs["body"]


# ---------------------------------------------------------------------------
# #717: Co-Authored-By uses a human-shaped name; model id moves to Implemented-By
# ---------------------------------------------------------------------------


_COAUTHOR_HUMAN_NAME_RE = re.compile(r"^Co-Authored-By: [A-Za-z].* <.*@.*>$")


class TestCoAuthorLine:
    """commit_changes emits a human Co-Authored-By and a separate Implemented-By trailer (#717)."""

    def test_claude_coauthor_is_human_name_not_model_id(self) -> None:
        porcelain = " M src/feature.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add feature")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model", return_value="claude-test-model-9"),
        ):
            pr_manager.commit_changes(10, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[2].args[0][-1]
        coauthor_line = next(
            line for line in commit_msg.splitlines() if line.startswith("Co-Authored-By:")
        )
        assert coauthor_line == "Co-Authored-By: Claude Code <noreply@anthropic.com>"
        assert _COAUTHOR_HUMAN_NAME_RE.match(coauthor_line)
        # Model id must NOT appear in the name slot of Co-Authored-By (#717).
        assert "claude-test-model-9" not in coauthor_line

    def test_claude_implemented_by_carries_model_id(self) -> None:
        porcelain = " M src/feature.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add feature")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model", return_value="claude-test-model-9"),
        ):
            pr_manager.commit_changes(11, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[2].args[0][-1]
        assert "Implemented-By: claude-test-model-9" in commit_msg

    def test_implemented_by_reflects_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "claude-env-override-5")
        porcelain = " M foo.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="env override test")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
        ):
            pr_manager.commit_changes(20, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[2].args[0][-1]
        # Env override flows into Implemented-By, not Co-Authored-By.
        assert "Implemented-By: claude-env-override-5" in commit_msg
        coauthor_line = next(
            line for line in commit_msg.splitlines() if line.startswith("Co-Authored-By:")
        )
        assert "claude-env-override-5" not in coauthor_line
        assert coauthor_line == "Co-Authored-By: Claude Code <noreply@anthropic.com>"

    def test_codex_coauthor_is_codex_human_name(self) -> None:
        porcelain = " M foo.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="codex fallback commit")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model") as mock_model,
        ):
            pr_manager.commit_changes(30, Path("/tmp/wt"), agent="codex")

        commit_msg = run_mock.call_args_list[2].args[0][-1]
        coauthor_line = next(
            line for line in commit_msg.splitlines() if line.startswith("Co-Authored-By:")
        )
        assert coauthor_line == "Co-Authored-By: Codex <noreply@openai.com>"
        assert _COAUTHOR_HUMAN_NAME_RE.match(coauthor_line)
        assert "Implemented-By: Codex" in commit_msg
        mock_model.assert_not_called()


class TestImplementationStateLabel:
    """Tests for pr_has_implementation_state_label (existing-PR idempotency gate)."""

    def test_go_label(self) -> None:
        gh_mock = _status('{"labels": [{"name": "state:implementation-go"}]}')
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_has_implementation_state_label(7) == (True, False)

    def test_no_go_label(self) -> None:
        gh_mock = _status('{"labels": [{"name": "state:implementation-no-go"}]}')
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_has_implementation_state_label(7) == (False, True)

    def test_no_label(self) -> None:
        gh_mock = _status('{"labels": [{"name": "bug"}]}')
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_has_implementation_state_label(7) == (False, False)

    def test_empty_labels(self) -> None:
        gh_mock = _status('{"labels": []}')
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_has_implementation_state_label(7) == (False, False)

    def test_malformed_json_returns_false_false(self) -> None:
        gh_mock = _status("not json")
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_has_implementation_state_label(7) == (False, False)
