"""Tests for hephaestus.automation.pr_manager.

Covers commit_changes filtering of secret files, ensure_pr_created
fallback paths, and create_pr argument shape — all via mocked subprocess
and GitHub-API calls.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import pr_manager
from hephaestus.automation.session_naming import AGENT_COMMIT_MESSAGE, AGENT_PR_MESSAGE


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
                _status("M\tsrc/foo.py\nM\tsrc/bar.py\n"),  # changed files context
                _status(" src/foo.py | 1 +\n src/bar.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add foo")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
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
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),
                _status(""),
                _status("R\told.py\tnew.py\n"),
                _status(" new.py | 1 +\n"),
                _status(""),
            ]
        )
        issue = MagicMock(title="Rename")
        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(4, Path("/tmp/wt"))
        add_call = run_mock.call_args_list[1].args[0]
        assert "new.py" in add_call

    def test_uses_message_agent_for_commit_subject_and_body(self) -> None:
        porcelain = " M LICENSE\n M NOTICE\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status("M\tLICENSE\nM\tNOTICE\n"),  # changed files context
                _status(" LICENSE | 2 +-\n NOTICE | 2 +-\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Refresh copyright years", body="Update 2024 to 2024-2026.")
        agent_output = (
            '{"subject":"docs: update copyright notices",'
            '"body":"Refresh stale license and notice metadata."}'
        )

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(
                pr_manager, "_invoke_git_message_agent", return_value=agent_output
            ) as invoke,
        ):
            pr_manager.commit_changes(1515, Path("/tmp/wt"), agent="codex")

        prompt = invoke.call_args.kwargs["prompt"]
        assert "LICENSE" in prompt
        assert "NOTICE" in prompt
        commit_msg = run_mock.call_args_list[-1].args[0][-1]
        assert commit_msg.startswith("docs: update copyright notices\n\n")
        assert "Refresh stale license and notice metadata." in commit_msg
        assert "Closes #1515" in commit_msg
        assert "Implemented-By: Codex" in commit_msg
        assert "Co-Authored-By: Codex <noreply@openai.com>" in commit_msg

    def test_commit_message_agent_invalid_output_falls_back(self) -> None:
        porcelain = " M src/feature.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status("M\tsrc/feature.py\n"),  # changed files context
                _status(" src/feature.py | 3 +++\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add feature", body="Implement it.")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(10, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
        assert commit_msg.startswith("feat: Implement #10\n\nAdd feature\n")
        assert "Closes #10" in commit_msg


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
                1,
                "branch",
                auto_merge=False,
                agent="claude",
                base="master",
                worktree_path=Path("/tmp/wt"),
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
                1,
                "branch",
                auto_merge=False,
                agent="codex",
                base="master",
                worktree_path=Path("/tmp/wt"),
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

    def test_uses_message_agent_for_pr_title_and_body(self) -> None:
        issue = MagicMock(title="Refresh copyright years", body="Update stale docs.")
        run_mock = MagicMock(
            side_effect=[
                _status("M\tLICENSE\nM\tNOTICE\n"),  # changed files
                _status(" LICENSE | 2 +-\n NOTICE | 2 +-\n"),  # diff stat
                _status("33f2ea6 docs: update copyright notices\n"),  # commits
            ]
        )
        agent_output = (
            '{"title":"docs: update copyright notices",'
            '"summary":"Refresh stale legal metadata.",'
            '"changes":["Updated LICENSE copyright range","Updated NOTICE copyright range"],'
            '"testing":["pytest tests/unit/automation/test_pr_manager.py"]}'
        )

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(
                pr_manager, "_invoke_git_message_agent", return_value=agent_output
            ) as invoke,
            patch.object(pr_manager, "gh_pr_create", return_value=7) as gh_mock,
        ):
            assert (
                pr_manager.create_pr(
                    1515,
                    "1515-auto-impl",
                    auto_merge=False,
                    agent="codex",
                    base="main",
                    worktree_path=Path("/tmp/wt"),
                )
                == 7
            )

        prompt = invoke.call_args.kwargs["prompt"]
        assert "LICENSE" in prompt
        assert "NOTICE" in prompt
        assert "33f2ea6 docs: update copyright notices" in prompt
        kwargs = gh_mock.call_args.kwargs
        assert kwargs["title"] == "docs: update copyright notices"
        assert "Refresh stale legal metadata." in kwargs["body"]
        assert "- Updated LICENSE copyright range" in kwargs["body"]
        assert "- pytest tests/unit/automation/test_pr_manager.py" in kwargs["body"]
        assert "Closes #1515" in kwargs["body"]
        assert "Generated by Codex via ProjectHephaestus automation." in kwargs["body"]

    def test_pr_message_agent_invalid_output_falls_back(self) -> None:
        issue = MagicMock(title="Add feature X", body="Do it.")
        with (
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
            patch.object(pr_manager, "gh_pr_create", return_value=7) as gh_mock,
        ):
            assert (
                pr_manager.create_pr(
                    5,
                    "branch",
                    auto_merge=True,
                    agent="codex",
                    worktree_path=Path("/tmp/wt"),
                )
                == 7
            )

        kwargs = gh_mock.call_args.kwargs
        assert kwargs["title"] == "feat: Add feature X"
        assert "Implements #5" in kwargs["body"]
        assert "Automated implementation via Codex" in kwargs["body"]


class TestMessageAgentInvocation:
    """Tests for the lightweight git-message agent invocation."""

    def test_claude_message_agent_uses_separate_session(self) -> None:
        with (
            patch.object(pr_manager, "get_repo_slug", return_value="ProjectHephaestus"),
            patch.object(pr_manager, "git_message_model", return_value="claude-haiku-4-5"),
            patch.object(pr_manager, "git_message_agent_timeout", return_value=120),
            patch.object(
                pr_manager,
                "invoke_claude_with_session",
                return_value=("{}", "sid"),
            ) as invoke,
        ):
            assert (
                pr_manager._invoke_git_message_agent(
                    issue_number=9,
                    agent_kind=AGENT_COMMIT_MESSAGE,
                    prompt="prompt",
                    worktree_path=Path("/tmp/wt"),
                    agent="claude",
                )
                == "{}"
            )

        kwargs = invoke.call_args.kwargs
        assert kwargs["agent"] == AGENT_COMMIT_MESSAGE
        assert kwargs["model"] == "claude-haiku-4-5"
        assert kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert kwargs["timeout"] == 120

    def test_codex_message_agent_uses_read_only_codex_exec(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["codex", "exec"], returncode=0, stdout="{}", stderr=""
        )
        with (
            patch.dict("os.environ", {"HEPH_GIT_MESSAGE_MODEL": "gpt-5.4-mini"}),
            patch.object(pr_manager, "git_message_agent_timeout", return_value=120),
            patch.object(pr_manager, "run_agent_text", return_value=completed) as run_agent,
        ):
            assert (
                pr_manager._invoke_git_message_agent(
                    issue_number=9,
                    agent_kind=AGENT_PR_MESSAGE,
                    prompt="prompt",
                    worktree_path=Path("/tmp/wt"),
                    agent="codex",
                )
                == "{}"
            )

        kwargs = run_agent.call_args.kwargs
        assert kwargs["agent"] == "codex"
        assert kwargs["cwd"] == Path("/tmp/wt")
        assert kwargs["sandbox"] == "read-only"
        assert kwargs["model"] == "gpt-5.4-mini"

    def test_pi_message_agent_uses_read_only_pi_exec(self) -> None:
        completed = subprocess.CompletedProcess(args=["pi"], returncode=0, stdout="{}", stderr="")
        with (
            patch.object(pr_manager, "uses_direct_agent_runner", return_value=True),
            patch.object(pr_manager, "git_message_agent_timeout", return_value=120),
            patch.object(pr_manager, "run_agent_text", return_value=completed) as run_agent,
        ):
            assert (
                pr_manager._invoke_git_message_agent(
                    issue_number=9,
                    agent_kind=AGENT_PR_MESSAGE,
                    prompt="prompt",
                    worktree_path=Path("/tmp/wt"),
                    agent="pi",
                )
                == "{}"
            )

        kwargs = run_agent.call_args.kwargs
        assert kwargs["agent"] == "pi"
        assert kwargs["cwd"] == Path("/tmp/wt")
        assert kwargs["sandbox"] == "read-only"


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
                _status("M\tsrc/feature.py\n"),  # changed files context
                _status(" src/feature.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add feature")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model", return_value="claude-test-model-9"),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(10, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
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
                _status("M\tsrc/feature.py\n"),  # changed files context
                _status(" src/feature.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="Add feature")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model", return_value="claude-test-model-9"),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(11, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
        assert "Implemented-By: claude-test-model-9" in commit_msg

    def test_implemented_by_reflects_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "claude-env-override-5")
        porcelain = " M foo.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status("M\tfoo.py\n"),  # changed files context
                _status(" foo.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="env override test")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(20, Path("/tmp/wt"))

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
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
                _status("M\tfoo.py\n"),  # changed files context
                _status(" foo.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="codex fallback commit")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model") as mock_model,
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(30, Path("/tmp/wt"), agent="codex")

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
        coauthor_line = next(
            line for line in commit_msg.splitlines() if line.startswith("Co-Authored-By:")
        )
        assert coauthor_line == "Co-Authored-By: Codex <noreply@openai.com>"
        assert _COAUTHOR_HUMAN_NAME_RE.match(coauthor_line)
        assert "Implemented-By: Codex" in commit_msg
        mock_model.assert_not_called()

    def test_pi_coauthor_and_provenance_are_pi(self) -> None:
        porcelain = " M foo.py\n"
        run_mock = MagicMock(
            side_effect=[
                _status(porcelain),  # git status
                _status(""),  # git add
                _status("M\tfoo.py\n"),  # changed files context
                _status(" foo.py | 1 +\n"),  # stat context
                _status(""),  # git commit
            ]
        )
        issue = MagicMock(title="pi fallback commit")

        with (
            patch.object(pr_manager, "run", run_mock),
            patch.object(pr_manager, "fetch_issue_info", return_value=issue),
            patch.object(pr_manager, "implementer_model") as mock_model,
            patch.object(pr_manager, "_invoke_git_message_agent", return_value="not json"),
        ):
            pr_manager.commit_changes(31, Path("/tmp/wt"), agent="pi")

        commit_msg = run_mock.call_args_list[-1].args[0][-1]
        coauthor_line = next(
            line for line in commit_msg.splitlines() if line.startswith("Co-Authored-By:")
        )
        assert coauthor_line == "Co-Authored-By: Pi <noreply@earendil.works>"
        assert _COAUTHOR_HUMAN_NAME_RE.match(coauthor_line)
        assert "Implemented-By: Pi" in commit_msg
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


class TestPrIsGenuinelyStuck:
    """``pr_is_genuinely_stuck`` distinguishes stuck PRs from pending ones (#1576)."""

    def test_dirty_merge_state_is_stuck(self) -> None:
        gh_mock = _status('{"mergeStateStatus": "DIRTY", "mergeable": "", "statusCheckRollup": []}')
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is True

    def test_conflicting_mergeable_is_stuck(self) -> None:
        gh_mock = _status(
            '{"mergeStateStatus": "BLOCKED", "mergeable": "CONFLICTING", "statusCheckRollup": []}'
        )
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is True

    def test_red_check_is_stuck(self) -> None:
        gh_mock = _status(
            '{"mergeStateStatus": "BLOCKED", "mergeable": "MERGEABLE", '
            '"statusCheckRollup": [{"conclusion": "FAILURE"}]}'
        )
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is True

    def test_blocked_on_review_is_not_stuck(self) -> None:
        # Green CI, BLOCKED only because review hasn't approved → NOT stuck.
        gh_mock = _status(
            '{"mergeStateStatus": "BLOCKED", "mergeable": "MERGEABLE", '
            '"statusCheckRollup": [{"conclusion": "SUCCESS"}]}'
        )
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is False

    def test_clean_green_is_not_stuck(self) -> None:
        gh_mock = _status(
            '{"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "statusCheckRollup": []}'
        )
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is False

    def test_malformed_json_is_not_stuck(self) -> None:
        # Safe default: never misclassify an unknown PR as stuck.
        gh_mock = _status("not json")
        with patch.object(pr_manager, "_gh_call", return_value=gh_mock):
            assert pr_manager.pr_is_genuinely_stuck(7) is False


class TestNormalizeConventionalType:
    """``_normalize_conventional_type`` keeps the commit/PR type pr-policy-legal (#1587)."""

    def test_disallowed_type_normalized_scope_preserved(self) -> None:
        assert (
            pr_manager._normalize_conventional_type("security(audit): add threat model")
            == "chore(audit): add threat model"
        )

    def test_allowed_type_unchanged(self) -> None:
        assert (
            pr_manager._normalize_conventional_type("fix(io): handle EOF") == "fix(io): handle EOF"
        )

    def test_no_prefix_gets_default(self) -> None:
        assert (
            pr_manager._normalize_conventional_type("add threat model") == "chore: add threat model"
        )

    def test_breaking_bang_preserved(self) -> None:
        assert pr_manager._normalize_conventional_type("security!: drop API") == "chore!: drop API"

    def test_disallowed_no_scope(self) -> None:
        assert pr_manager._normalize_conventional_type("wip: stuff") == "chore: stuff"

    def test_allowlist_matches_pr_policy_gate(self) -> None:
        """The mirrored allowlist MUST equal the pr-policy gate's source of truth."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        from check_conventional_commit import ALLOWED_TYPES

        assert set(pr_manager.ALLOWED_CONVENTIONAL_TYPES) == set(ALLOWED_TYPES)
