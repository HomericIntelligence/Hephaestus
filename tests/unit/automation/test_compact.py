"""Tests for compact_session helper (#842)."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.learn import compact_agent_session, compact_session
from hephaestus.automation.session_naming import AGENT_CI_DRIVER, session_uuid


class TestCompactSession:
    """Test suite for compact_session helper."""

    @pytest.fixture(autouse=True)
    def _stable_checkout_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hephaestus.automation.agent_config._checkout_identity",
            lambda _cwd: "test-checkout",
        )

    def test_compact_session_sends_command_via_stdin(self, tmp_path: Path) -> None:
        """Verify /compact is sent via stdin rather than process arguments."""
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            result = compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path)

        assert result is True
        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert cmd[-1] == "--print"
        assert all("/compact" not in argument for argument in cmd)
        assert mock_run.call_args.kwargs["input"] == "/compact"
        assert mock_run.call_args.kwargs["text"] is True

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
            expected_uuid = session_uuid(repo, issue, agent, cwd=tmp_path)
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

    def test_compact_session_uses_explicit_learn_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit compact timeout wins while the removed environment is inert."""
        monkeypatch.setenv("HEPH_AGENT_LEARN_TIMEOUT", "333")
        with patch("hephaestus.automation.learn.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="")

            compact_session("test-repo", 42, AGENT_CI_DRIVER, tmp_path, timeout=333)

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
            disable_pi_automation=False,
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
