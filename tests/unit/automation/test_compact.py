"""Tests for compact_session helper (#842)."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.learn import compact_agent_session, compact_session
from hephaestus.automation.session_naming import AGENT_CI_DRIVER, session_uuid


class TestCompactSession:
    """Test suite for compact_session helper."""

    def test_compact_session_issues_resume_and_print(self, tmp_path: Path) -> None:
        """Verify compact_session invokes claude with --resume and --print /compact."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            # Verify the subprocess was called
            assert mock_run.call_count == 1
            call_args = mock_run.call_args

            # Verify the command structure
            cmd = call_args[0][0]
            assert "claude" in cmd
            assert "--resume" in cmd
            assert "--print" in cmd
            assert "/compact" in cmd

            # Verify the order: --resume <uuid> comes before --print /compact
            resume_idx = cmd.index("--resume")
            print_idx = cmd.index("--print")
            assert resume_idx < print_idx

    def test_compact_session_uses_deterministic_uuid(self, tmp_path: Path) -> None:
        """Verify compact_session uses the deterministic session_uuid."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            repo = "Hephaestus"
            issue = 842
            agent = AGENT_CI_DRIVER

            compact_session(repo, issue, agent, tmp_path)

            # Get the actual UUID that was passed
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            resume_idx = cmd.index("--resume")
            actual_uuid = cmd[resume_idx + 1]

            # Compare to the real session_uuid function
            expected_uuid = session_uuid(repo, issue, agent)
            assert actual_uuid == expected_uuid

    def test_compact_session_forwards_cwd(self, tmp_path: Path) -> None:
        """Verify compact_session passes cwd to subprocess.run."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            test_cwd = tmp_path / "test_workdir"
            compact_session("test-repo", 42, AGENT_CI_DRIVER, test_cwd)

            # Verify cwd is passed as a string
            call_kwargs = mock_run.call_args[1]
            assert "cwd" in call_kwargs
            assert call_kwargs["cwd"] == str(test_cwd)

    def test_compact_session_omits_dangerously_skip_permissions_and_uses_text_output(
        self, tmp_path: Path
    ) -> None:
        """Verify compact keeps text output without bypassing permissions."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in cmd
            assert "--output-format" in cmd
            output_fmt_idx = cmd.index("--output-format")
            assert cmd[output_fmt_idx + 1] == "text"

    def test_compact_session_default_timeout_is_1200(self, tmp_path: Path) -> None:
        """Verify compact_session uses a 1200s default subprocess timeout.

        Slow sessions should be allowed to finish because throughput matters
        more than minimizing per-attempt latency.
        """
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert mock_run.call_args[1]["timeout"] == 1200

    def test_compact_session_uses_env_configured_learn_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default compact timeout is read from HEPH_AGENT_LEARN_TIMEOUT per call."""
        monkeypatch.setenv("HEPH_AGENT_LEARN_TIMEOUT", "333")
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert mock_run.call_args[1]["timeout"] == 333

    def test_compact_failure_returns_false_on_timeout(self, tmp_path: Path) -> None:
        """Verify compact_session returns False on timeout (non-fatal)."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 60)

            result = compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert result is False

    def test_compact_failure_returns_false_on_oserror(self, tmp_path: Path) -> None:
        """Verify compact_session returns False on OSError (e.g., missing binary)."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("claude binary not found")

            result = compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert result is False

    def test_compact_returns_false_on_nonzero_exit(self, tmp_path: Path) -> None:
        """Verify compact_session returns False when subprocess exits non-zero."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="error: unknown command: /compact")

            result = compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert result is False

    def test_compact_returns_true_on_zero_exit(self, tmp_path: Path) -> None:
        """Verify compact_session returns True on successful zero-exit."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            result = compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

            assert result is True


class TestCompactAgentSession:
    """Provider-neutral compaction preserves direct-runner context."""

    def test_codex_compact_resumes_the_persisted_session(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.learn.resume_agent_session") as resume:
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="codex",
                session_agent="pr-reviewer",
                session_id="codex-session",
                cwd=tmp_path,
                timeout=60,
                model="gpt-5.6",
                sandbox="read-only",
            )

        assert compacted is True
        resume.assert_called_once_with(
            agent="codex",
            session_id="codex-session",
            prompt="/compact",
            cwd=tmp_path,
            timeout=60,
            model="gpt-5.6",
            sandbox="read-only",
            approval="never",
        )

    def test_direct_compact_without_a_session_is_a_safe_noop(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.learn.resume_agent_session") as resume:
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="codex",
                session_agent="pr-reviewer",
                cwd=tmp_path,
            )

        assert compacted is False
        resume.assert_not_called()
