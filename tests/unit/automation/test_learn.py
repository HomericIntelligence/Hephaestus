"""Tests for the learn module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from hephaestus.automation.learn import (
    build_learn_prompt,
    learn_needs_rerun,
    mnemosyne_update_evidence,
    run_learn,
)


class TestLearnNeedsRerun:
    """Tests for learn_needs_rerun."""

    def test_missing_log_returns_true(self, tmp_path: Path) -> None:
        """Returns True when log file doesn't exist."""
        assert learn_needs_rerun(42, tmp_path) is True

    def test_failed_log_returns_true(self, tmp_path: Path) -> None:
        """Returns True when log file starts with FAILED:."""
        log_file = tmp_path / "learn-42.log"
        log_file.write_text("FAILED: something went wrong\nmore output")
        assert learn_needs_rerun(42, tmp_path) is True

    def test_successful_log_returns_false(self, tmp_path: Path) -> None:
        """Returns False when log file has successful content."""
        log_file = tmp_path / "learn-42.log"
        log_file.write_text("Learn completed successfully.")
        assert learn_needs_rerun(42, tmp_path) is False

    def test_empty_log_returns_false(self, tmp_path: Path) -> None:
        """Returns False for an empty log file (not failed)."""
        log_file = tmp_path / "learn-42.log"
        log_file.write_text("")
        assert learn_needs_rerun(42, tmp_path) is False

    def test_unreadable_log_returns_true(self, tmp_path: Path) -> None:
        """Returns True when log file cannot be read."""
        log_file = tmp_path / "learn-42.log"
        log_file.write_text("content")
        log_file.chmod(0o000)
        try:
            assert learn_needs_rerun(42, tmp_path) is True
        finally:
            log_file.chmod(0o644)


class TestRunLearn:
    """Tests for run_learn."""

    def test_mnemosyne_update_evidence_requires_project_mnemosyne_signal(self) -> None:
        evidence = mnemosyne_update_evidence("Learn complete. PR created.")

        assert evidence == {
            "mnemosyne_update_status": "unverified",
            "mnemosyne_update_urls": [],
            "mnemosyne_update_pr_numbers": [],
        }

    def test_mnemosyne_update_evidence_extracts_pr_url_and_ref(self) -> None:
        evidence = mnemosyne_update_evidence(
            "Opened https://github.com/HomericIntelligence/ProjectMnemosyne/pull/45 "
            "and referenced HomericIntelligence/ProjectMnemosyne#46"
        )

        assert evidence["mnemosyne_update_status"] == "confirmed"
        assert evidence["mnemosyne_update_urls"] == [
            "https://github.com/HomericIntelligence/ProjectMnemosyne/pull/45"
        ]
        assert evidence["mnemosyne_update_pr_numbers"] == [46]

    def test_build_learn_prompt_uses_user_facing_command(self) -> None:
        prompt = build_learn_prompt("Capture what happened.")

        assert prompt.startswith("/learn Capture what happened.")
        assert "/skills-registry-commands:learn" not in prompt
        assert "Only push skills to ProjectMnemosyne" in prompt

    def test_success_writes_log_and_returns_true(self, tmp_path: Path) -> None:
        """Returns True and writes log on successful claude run."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "Learn complete. PR created."

        with patch("hephaestus.automation.learn.run", return_value=mock_result) as mock_run:
            result = run_learn("session-abc", worktree_path, 42, tmp_path)

        assert result is True
        cmd_args = mock_run.call_args.args[0]
        prompt = cmd_args[cmd_args.index("session-abc") + 1]
        assert prompt.startswith("/learn")
        assert "/skills-registry-commands:learn" not in prompt
        log_file = tmp_path / "learn-42.log"
        assert log_file.exists()
        assert log_file.read_text() == "Learn complete. PR created."
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["issue_number"] == 42
        assert record["learn_status"] == "succeeded"
        assert record["learn_attempted_at"]
        assert record["learn_succeeded_at"] == record["learn_attempted_at"]
        assert record["log_path"] == str(log_file)
        assert record["mnemosyne_update_status"] == "unverified"
        assert record["mnemosyne_update_urls"] == []
        assert record["mnemosyne_update_pr_numbers"] == []

    def test_failure_writes_failed_log_and_returns_false(self, tmp_path: Path) -> None:
        """Returns False and writes FAILED: log on exception."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with patch("hephaestus.automation.learn.run", side_effect=RuntimeError("claude crashed")):
            result = run_learn("session-abc", worktree_path, 42, tmp_path)

        assert result is False
        log_file = tmp_path / "learn-42.log"
        assert log_file.exists()
        assert log_file.read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["issue_number"] == 42
        assert record["learn_status"] == "failed"
        assert record["learn_attempted_at"]
        assert record["learn_succeeded_at"] is None
        assert record["error"] == "claude crashed"
        assert record["mnemosyne_update_status"] == "failed"

    def test_codex_skips_legacy_claude_session(self, tmp_path: Path) -> None:
        """Legacy sessions must not be resumed through Codex."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with patch("hephaestus.automation.learn.resume_codex_session") as mock_resume:
            result = run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                agent="codex",
            )

        assert result is False
        mock_resume.assert_not_called()
        assert (tmp_path / "learn-42.log").read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "failed"
        assert "selected agent is codex" in record["error"]

    def test_codex_resumes_matching_codex_session(self, tmp_path: Path) -> None:
        """Codex sessions with provider metadata should resume through Codex."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "learned"

        with patch(
            "hephaestus.automation.learn.resume_codex_session", return_value=mock_result
        ) as mock_resume:
            result = run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                agent="codex",
                session_agent="codex",
            )

        assert result is True
        prompt = mock_resume.call_args.args[1]
        assert prompt.startswith("/learn")
        assert "/skills-registry-commands:learn" not in prompt
        assert (tmp_path / "learn-42.log").read_text() == "learned"
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "succeeded"
        assert record["mnemosyne_update_status"] == "unverified"

    def test_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        """Creates state_dir if it does not exist."""
        state_dir = tmp_path / "nested" / "state"
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        with patch("hephaestus.automation.learn.run", return_value=mock_result):
            run_learn("session-abc", worktree_path, 42, state_dir)

        assert state_dir.exists()

    def test_slot_id_accepted(self, tmp_path: Path) -> None:
        """slot_id parameter is accepted without error."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        with patch("hephaestus.automation.learn.run", return_value=mock_result):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, slot_id=3)

        assert result is True

    def test_uses_learn_model_not_hardcoded_sonnet(self, tmp_path: Path) -> None:
        """run_learn passes learn_model() to --model instead of hardcoding 'sonnet' (A5-12)."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        with (
            patch("hephaestus.automation.learn.run", return_value=mock_result) as mock_run,
            patch(
                "hephaestus.automation.learn.learn_model", return_value="claude-haiku-4-5"
            ) as mock_learn_model,
        ):
            run_learn("session-abc", worktree_path, 42, tmp_path)

        mock_learn_model.assert_called_once()
        # Verify "--model" "claude-haiku-4-5" appears in the command args
        cmd_args = mock_run.call_args[0][0]
        assert "--model" in cmd_args
        model_idx = cmd_args.index("--model")
        assert cmd_args[model_idx + 1] == "claude-haiku-4-5"

    def test_learn_model_env_override_respected(self, tmp_path: Path) -> None:
        """HEPH_LEARN_MODEL env override is used by run_learn (A5-12)."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        import os

        with (
            patch.dict(os.environ, {"HEPH_LEARN_MODEL": "claude-opus-4-7"}),
            patch("hephaestus.automation.learn.run", return_value=mock_result) as mock_run,
        ):
            run_learn("session-abc", worktree_path, 42, tmp_path)

        cmd_args = mock_run.call_args[0][0]
        model_idx = cmd_args.index("--model")
        assert cmd_args[model_idx + 1] == "claude-opus-4-7"
