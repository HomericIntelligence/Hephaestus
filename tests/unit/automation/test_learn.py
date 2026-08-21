"""Tests for the learn module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.learn import (
    _LEARN_RATE_LIMIT_MAX_RETRIES,
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

    def test_mnemosyne_update_evidence_ignores_pr_like_model_text(self) -> None:
        evidence = mnemosyne_update_evidence(
            "Opened https://github.com/HomericIntelligence/Mnemosyne/pull/45 "
            "and referenced HomericIntelligence/Mnemosyne#46"
        )

        assert evidence["mnemosyne_update_status"] == "unverified"
        assert evidence["mnemosyne_update_urls"] == []
        assert evidence["mnemosyne_update_pr_numbers"] == []

    def test_mnemosyne_update_evidence_confirms_delivery_receipt(self) -> None:
        evidence = mnemosyne_update_evidence(
            {
                "commit_sha": "a" * 40,
                "readback_head_sha": "a" * 40,
                "pr_url": "https://github.com/acme/Mnemosyne/pull/7",
                "pr_number": 7,
                "local_only": False,
            }
        )

        assert evidence["mnemosyne_update_status"] == "confirmed"
        assert evidence["mnemosyne_update_urls"] == ["https://github.com/acme/Mnemosyne/pull/7"]
        assert evidence["mnemosyne_update_pr_numbers"] == [7]

    def test_build_learn_prompt_uses_user_facing_command(self) -> None:
        prompt = build_learn_prompt("Capture what happened.")

        assert prompt.startswith("/learn EXECUTE")
        assert "Capture what happened." in prompt
        assert "/skills-registry-commands:learn" not in prompt
        assert "Only push skills to the resolved Mnemosyne" in prompt
        # Directives must appear before the context detail
        assert prompt.index("Do NOT return a plan") < prompt.index("Capture what happened.")

    def test_success_writes_log_and_returns_true(self, tmp_path: Path) -> None:
        """Returns True and writes log on successful claude run."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "Learn complete. PR created."

        with patch("hephaestus.automation.learn.run", return_value=mock_result) as mock_run:
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

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
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

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

    @pytest.mark.parametrize(("agent", "session_agent"), [("claude", None), ("codex", "claude")])
    def test_invalid_provider_metadata_never_resumes(
        self, tmp_path: Path, agent: str, session_agent: str | None
    ) -> None:
        """Absent and cross-provider metadata never resume."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with (
            patch("hephaestus.automation.learn.run") as mock_run,
            patch("hephaestus.automation.learn.resume_agent_session") as mock_resume,
        ):
            result = run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                agent=agent,
                session_agent=session_agent,
            )

        assert result is False
        mock_run.assert_not_called()
        mock_resume.assert_not_called()
        assert (tmp_path / "learn-42.log").read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "failed"
        assert "skipping" in record["error"]

    def test_codex_resumes_matching_codex_session(self, tmp_path: Path) -> None:
        """Codex sessions with provider metadata should resume through Codex."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "learned"

        with patch(
            "hephaestus.automation.learn.resume_agent_session", return_value=mock_result
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
        assert mock_resume.call_args.kwargs["agent"] == "codex"
        prompt = mock_resume.call_args.kwargs["prompt"]
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
            run_learn("session-abc", worktree_path, 42, state_dir, session_agent="claude")

        assert state_dir.exists()

    def test_slot_id_accepted(self, tmp_path: Path) -> None:
        """slot_id parameter is accepted without error."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        with patch("hephaestus.automation.learn.run", return_value=mock_result):
            result = run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                slot_id=3,
                session_agent="claude",
            )

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
            run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        mock_learn_model.assert_called_once()
        # Verify "--model" "claude-haiku-4-5" appears in the command args
        cmd_args = mock_run.call_args[0][0]
        assert "--model" in cmd_args
        model_idx = cmd_args.index("--model")
        assert cmd_args[model_idx + 1] == "claude-haiku-4-5"

    def test_explicit_learn_model_is_respected(self, tmp_path: Path) -> None:
        """An explicit learn model wins while the removed environment is inert."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_result = MagicMock()
        mock_result.stdout = "done"

        import os

        with (
            patch.dict(os.environ, {"HEPH_LEARN_MODEL": "claude-opus-4-7"}),
            patch("hephaestus.automation.learn.run", return_value=mock_result) as mock_run,
        ):
            run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                session_agent="claude",
                model="claude-opus-4-7",
            )

        cmd_args = mock_run.call_args[0][0]
        model_idx = cmd_args.index("--model")
        assert cmd_args[model_idx + 1] == "claude-opus-4-7"


class TestRunLearnRateLimitRetry:
    """Tests for the wait-and-retry behavior on a rate-limited /learn (#1331)."""

    # A Claude session-limit 429 message carrying a parseable reset time; the
    # common resolver (resolve_quota_reset_epoch) parses this to a future epoch.
    _RATE_LIMIT_MSG = "You've hit your session limit · resets 11:59pm (America/Los_Angeles)"

    def test_rate_limited_stdout_waits_then_retries_and_succeeds(self, tmp_path: Path) -> None:
        """A rate-limit message in stdout triggers wait_until + a second invocation."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        first = MagicMock()
        first.stdout = self._RATE_LIMIT_MSG
        second = MagicMock()
        second.stdout = "Learn complete. PR created."

        with (
            patch("hephaestus.automation.learn.run", side_effect=[first, second]) as mock_run,
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
        ):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        assert result is True
        # Two invocations: the rate-limited one and the successful retry.
        assert mock_run.call_count == 2
        mock_wait.assert_called_once()
        # The retry re-issues the SAME command.
        assert mock_run.call_args_list[0].args[0] == mock_run.call_args_list[1].args[0]
        log_file = tmp_path / "learn-42.log"
        assert not log_file.read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "succeeded"

    def test_rate_limited_exception_waits_then_retries_and_succeeds(self, tmp_path: Path) -> None:
        """A rate-limit message in a raised CalledProcessError triggers wait + retry."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        err = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], output="", stderr=self._RATE_LIMIT_MSG
        )
        success = MagicMock()
        success.stdout = "Learn complete."

        with (
            patch("hephaestus.automation.learn.run", side_effect=[err, success]) as mock_run,
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
        ):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        assert result is True
        assert mock_run.call_count == 2
        mock_wait.assert_called_once()
        log_file = tmp_path / "learn-42.log"
        assert not log_file.read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "succeeded"

    def test_unknown_reset_sentinel_backs_off_fixed_interval(self, tmp_path: Path) -> None:
        """A rate-limit message with no reset time (epoch 0) backs off a fixed interval."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        # Session-limit phrasing WITHOUT a "resets ..." clause -> resolver yields 0.
        first = MagicMock()
        first.stdout = "You've hit your session limit"
        second = MagicMock()
        second.stdout = "Learn complete."

        with (
            patch("hephaestus.automation.learn.run", side_effect=[first, second]),
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
            patch("hephaestus.automation.learn.time.time", return_value=1_000_000),
        ):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        assert result is True
        # Backed off a fixed interval (now + backoff), not epoch 0.
        mock_wait.assert_called_once_with(1_000_000 + 300)

    def test_persistent_rate_limit_respects_retry_bound(self, tmp_path: Path) -> None:
        """A message that always parses as rate-limited stops after the retry bound."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        always = MagicMock()
        always.stdout = self._RATE_LIMIT_MSG

        with (
            patch("hephaestus.automation.learn.run", return_value=always) as mock_run,
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
        ):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        assert result is False
        # Bounded: max_retries + 1 attempts, then give up (no infinite loop).
        assert mock_run.call_count == _LEARN_RATE_LIMIT_MAX_RETRIES + 1
        assert mock_wait.call_count == _LEARN_RATE_LIMIT_MAX_RETRIES + 1
        log_file = tmp_path / "learn-42.log"
        assert log_file.read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "failed"
        assert "rate-limited after" in record["error"]

    def test_genuine_failure_records_failed_without_retry(self, tmp_path: Path) -> None:
        """A non-rate-limit failure records FAILED: and returns False without retrying."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        with (
            patch(
                "hephaestus.automation.learn.run",
                side_effect=RuntimeError("disk full"),
            ) as mock_run,
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
        ):
            result = run_learn("session-abc", worktree_path, 42, tmp_path, session_agent="claude")

        assert result is False
        # Genuine failure: a single attempt, no wait, no retry.
        assert mock_run.call_count == 1
        mock_wait.assert_not_called()
        log_file = tmp_path / "learn-42.log"
        assert log_file.read_text().startswith("FAILED:")
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "failed"
        assert record["error"] == "disk full"

    def test_codex_rate_limited_waits_then_retries(self, tmp_path: Path) -> None:
        """Codex /learn also waits and retries on a rate-limit message (#1331)."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        first = MagicMock()
        first.stdout = self._RATE_LIMIT_MSG
        second = MagicMock()
        second.stdout = "learned"

        with (
            patch(
                "hephaestus.automation.learn.resume_agent_session",
                side_effect=[first, second],
            ) as mock_resume,
            patch("hephaestus.automation.learn.wait_until") as mock_wait,
        ):
            result = run_learn(
                "session-abc",
                worktree_path,
                42,
                tmp_path,
                agent="codex",
                session_agent="codex",
            )

        assert result is True
        assert mock_resume.call_count == 2
        mock_wait.assert_called_once()
        record = json.loads((tmp_path / "learn-42.json").read_text())
        assert record["learn_status"] == "succeeded"
