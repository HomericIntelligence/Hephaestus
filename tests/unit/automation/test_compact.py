"""Tests for compact_session helper (#842)."""

import subprocess
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.pi_session import create_pi_binding
from hephaestus.automation.agent_config import (
    AGENT_PLAN_REVIEWER,
    AGENT_PR_REVIEWER,
    session_uuid,
)
from hephaestus.automation.learn import compact_agent_session, compact_session
from hephaestus.utils import subprocess_registry


class TestCompactSession:
    """Test suite for compact_session helper."""

    @pytest.fixture(autouse=True)
    def _stable_checkout_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hephaestus.automation.agent_config._checkout_identity",
            lambda _cwd, *, remaining_timeout=None: "test-checkout",
        )

    def test_compact_session_sends_command_via_stdin(self, tmp_path: Path) -> None:
        """Verify /compact is sent via stdin rather than process arguments."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            result = compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

        assert result is True
        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert cmd[-1] == "--print"
        assert all("/compact" not in argument for argument in cmd)
        assert mock_run.call_args.kwargs["input_text"] == "/compact"
        assert mock_run.call_args.kwargs["track_process_group"] is True
        assert mock_run.call_args.kwargs["check"] is True
        assert mock_run.call_args.kwargs["shutdown"] is None

    def test_compact_session_stops_before_process_start_when_budget_expires(
        self, tmp_path: Path
    ) -> None:
        """An expired operation budget prevents the provider process."""
        budget = MagicMock(
            side_effect=[60, 60, subprocess.TimeoutExpired("compact operation deadline", 0)]
        )
        with (
            patch("hephaestus.automation.learn.session_uuid", return_value="session"),
            patch("subprocess.Popen") as popen,
        ):
            result = compact_session(
                "test-repo",
                42,
                AGENT_PLAN_REVIEWER,
                tmp_path,
                remaining_timeout=budget,
            )

        assert result is False
        popen.assert_not_called()

    def test_compact_session_reaps_process_when_budget_expires_during_start(
        self, tmp_path: Path
    ) -> None:
        """A deadline race after process start stops and reaps the provider."""
        budget = MagicMock(
            side_effect=[
                60,
                60,
                60,
                subprocess.TimeoutExpired("compact operation deadline", 0),
            ]
        )
        process = MagicMock(pid=123)
        with (
            patch("hephaestus.automation.learn.session_uuid", return_value="session"),
            patch("hephaestus.utils.subprocess_registry.supported", return_value=True),
            patch("subprocess.Popen", return_value=process),
            patch("hephaestus.utils.subprocess_registry.track_process_group"),
            patch("hephaestus.utils.helpers._stop_process_group", return_value=("", "")) as stop,
        ):
            result = compact_session(
                "test-repo",
                42,
                AGENT_PLAN_REVIEWER,
                tmp_path,
                remaining_timeout=budget,
            )

        assert result is False
        stop.assert_called_once_with(process)
        process.communicate.assert_not_called()

    def test_compact_session_uses_deterministic_uuid(self, tmp_path: Path) -> None:
        """Verify compact_session uses the deterministic session_uuid."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            repo = "Hephaestus"
            issue = 842
            agent = AGENT_PLAN_REVIEWER

            compact_session(repo, issue, agent, tmp_path)

            # Get the actual UUID that was passed
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            resume_idx = cmd.index("--resume")
            actual_uuid = cmd[resume_idx + 1]

            # Compare to the real session_uuid function
            expected_uuid = session_uuid(repo, issue, agent, cwd=tmp_path)
            assert actual_uuid == expected_uuid

    def test_inline_effort_uses_the_base_claude_session(self, tmp_path: Path) -> None:
        """Claude compaction strips the effort before it resolves the session key."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            compact_session(
                "Hephaestus",
                842,
                AGENT_PLAN_REVIEWER,
                tmp_path,
                model="claude-sonnet-5:future-effort",
            )

        command = mock_run.call_args.args[0]
        assert command[command.index("--resume") + 1] == session_uuid(
            "Hephaestus",
            842,
            AGENT_PLAN_REVIEWER,
            "claude-sonnet-5",
            cwd=tmp_path,
        )

    def test_compact_session_forwards_cwd(self, tmp_path: Path) -> None:
        """Verify compact_session passes cwd to the tracked runner."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            test_cwd = tmp_path / "test_workdir"
            compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, test_cwd)

            # Verify cwd is passed as a string
            call_kwargs = mock_run.call_args[1]
            assert "cwd" in call_kwargs
            assert call_kwargs["cwd"] == str(test_cwd)

    def test_compact_session_omits_dangerously_skip_permissions_and_uses_text_output(
        self, tmp_path: Path
    ) -> None:
        """Verify compact keeps text output without bypassing permissions."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

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
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

            assert mock_run.call_args[1]["timeout"] == 1200

    def test_compact_session_uses_explicit_learn_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit compact timeout wins while the removed environment is inert."""
        monkeypatch.setenv("HEPH_AGENT_LEARN_TIMEOUT", "333")
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path, timeout=333)

            assert mock_run.call_args[1]["timeout"] == 333

    def test_compact_failure_returns_false_on_timeout(self, tmp_path: Path) -> None:
        """Verify compact_session returns False on timeout (non-fatal)."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 60)

            result = compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

            assert result is False

    def test_compact_failure_returns_false_on_oserror(self, tmp_path: Path) -> None:
        """Verify compact_session returns False on OSError (e.g., missing binary)."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.side_effect = FileNotFoundError("claude binary not found")

            result = compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

            assert result is False

    def test_compact_returns_false_on_nonzero_exit(self, tmp_path: Path) -> None:
        """Verify compact_session returns False when subprocess exits non-zero."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["claude"], stderr="error: unknown command: /compact"
            )

            result = compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

            assert result is False

    def test_compact_returns_true_on_zero_exit(self, tmp_path: Path) -> None:
        """Verify compact_session returns True on successful zero-exit."""
        with patch("hephaestus.automation.learn.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

            result = compact_session("test-repo", 42, AGENT_PLAN_REVIEWER, tmp_path)

            assert result is True


class TestCompactSessionDiscovery:
    """Real session lookup stops at the checked Git discovery boundary.

    These cases keep the real ``session_uuid`` and ``_checkout_identity``
    execution and mock Git only at its single external ``git rev-parse``
    command. The controlled clock or shutdown event changes inside the Git
    seam, so the post-command operation check is the boundary under test.
    """

    def test_shutdown_inside_git_discovery_prevents_provider_launch(self, tmp_path: Path) -> None:
        """A shutdown before the Git answer blocks the session lookup and launch."""
        shutdown = threading.Event()
        git_calls: list[list[str]] = []

        def remaining_timeout() -> int:
            if shutdown.is_set():
                raise InterruptedError("compact operation cancelled")
            return 60

        def git_seam(
            cmd: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            git_calls.append(cmd)
            shutdown.set()  # cancel before the "git rev-parse" answer returns
            return subprocess.CompletedProcess(cmd, 0, stdout="/checkout/.git\n", stderr="")

        with (
            patch("hephaestus.automation.agent_config.subprocess.run", side_effect=git_seam),
            patch("hephaestus.automation.learn.run_subprocess") as provider,
        ):
            result = compact_session(
                "test-repo",
                42,
                AGENT_PR_REVIEWER,
                tmp_path,
                remaining_timeout=remaining_timeout,
                shutdown=shutdown,
            )

        assert result is False
        provider.assert_not_called()
        assert len(git_calls) == 1

    def test_deadline_inside_git_discovery_prevents_provider_launch(self, tmp_path: Path) -> None:
        """An exhausted budget before the Git answer blocks the provider launch."""
        clock = [100.0]
        deadline_s = 130.0

        def remaining_timeout() -> int:
            remaining = deadline_s - clock[0]
            if remaining <= 0:
                raise subprocess.TimeoutExpired("compact operation deadline", 0)
            return min(60, int(remaining))

        def git_seam(
            cmd: list[str], *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            clock[0] = 200.0  # consume the operation budget inside the Git boundary
            return subprocess.CompletedProcess(cmd, 0, stdout="/checkout/.git\n", stderr="")

        with (
            patch("hephaestus.automation.agent_config.subprocess.run", side_effect=git_seam),
            patch("hephaestus.automation.learn.run_subprocess") as provider,
        ):
            result = compact_session(
                "test-repo",
                42,
                AGENT_PR_REVIEWER,
                tmp_path,
                remaining_timeout=remaining_timeout,
            )

        assert result is False
        provider.assert_not_called()


class TestCompactAgentSession:
    """Provider-neutral compaction preserves direct-runner context."""

    def test_codex_compact_resumes_the_persisted_session(self, tmp_path: Path) -> None:
        remaining_timeout = MagicMock(return_value=60)
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value="codex"),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
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
                remaining_timeout=remaining_timeout,
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
            pi_dir=None,
            process_tracker=subprocess_registry.track_process_group,
            remaining_timeout=remaining_timeout,
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

    def test_pi_compact_uses_the_selected_pi_configuration(self, tmp_path: Path) -> None:
        """Pi compaction must reuse the admitted directory and adapter."""
        pi_dir = tmp_path / "pi-agent"
        remaining_timeout = MagicMock(return_value=1200)
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value="pi") as resolve,
            patch(
                "hephaestus.automation.learn.agent_compaction_resume",
                return_value=("pi-session", {}),
            ),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="pi",
                session_agent="pr-reviewer",
                session_id="pi-session",
                cwd=tmp_path,
                pi_dir=pi_dir,
                pi_isolation_adapter="package:factory",
                remaining_timeout=remaining_timeout,
            )

        assert compacted is True
        resolve.assert_called_once_with(
            "pi",
            cwd=tmp_path,
            disable_pi_automation=False,
            auth_status_timeout=10,
            pi_isolation_adapter="package:factory",
            pi_dir=pi_dir,
            model_references=("",),
            remaining_timeout=remaining_timeout,
            shutdown=None,
        )
        assert resume.call_args.kwargs["pi_dir"] == pi_dir
        assert resume.call_args.kwargs["remaining_timeout"] is remaining_timeout

    def test_compact_bounds_authentication_to_the_remaining_operation_time(
        self, tmp_path: Path
    ) -> None:
        """Keep provider authentication in the current operation budget."""
        remaining_timeout = MagicMock(return_value=3)
        shutdown = threading.Event()
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value="codex") as resolve,
            patch(
                "hephaestus.automation.learn.agent_compaction_resume",
                return_value=("codex-session", {}),
            ),
            patch("hephaestus.automation.learn.resume_agent_session"),
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="codex",
                session_agent="pr-reviewer",
                session_id="codex-session",
                cwd=tmp_path,
                auth_status_timeout=10,
                remaining_timeout=remaining_timeout,
                shutdown=shutdown,
            )

        assert compacted is True
        assert resolve.call_args.kwargs["auth_status_timeout"] == 3
        assert resolve.call_args.kwargs["remaining_timeout"] is remaining_timeout
        assert resolve.call_args.kwargs["shutdown"] is shutdown

    def test_opencode_compact_resumes_the_persisted_session(self, tmp_path: Path) -> None:
        """OpenCode compaction resumes the stored session through the shared route."""
        remaining_timeout = MagicMock(return_value=60)
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value="opencode"),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="opencode",
                session_agent="pr-reviewer",
                session_id="opencode-session",
                cwd=tmp_path,
                timeout=60,
                model="comet/kimi-k3",
                sandbox="read-only",
                remaining_timeout=remaining_timeout,
            )

        assert compacted is True
        resume.assert_called_once_with(
            agent="opencode",
            session_id="opencode-session",
            prompt="/compact",
            cwd=tmp_path,
            timeout=60,
            model="comet/kimi-k3",
            sandbox="read-only",
            approval="never",
            disable_pi_automation=False,
            pi_dir=None,
            process_tracker=subprocess_registry.track_process_group,
            remaining_timeout=remaining_timeout,
        )

    @pytest.mark.parametrize("provider", ["codex", "opencode"])
    @pytest.mark.parametrize(
        "boundary",
        [
            InterruptedError("compact operation cancelled"),
            subprocess.TimeoutExpired("compact operation deadline", 0),
        ],
        ids=("cancellation", "deadline"),
    )
    def test_compact_stops_before_provider_authentication(
        self, tmp_path: Path, provider: str, boundary: BaseException
    ) -> None:
        """An exhausted bound prevents provider authentication and any launch."""
        remaining_timeout = MagicMock(side_effect=boundary)
        with (
            patch("hephaestus.automation.learn.resolve_agent") as resolve,
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider=provider,
                session_agent="pr-reviewer",
                session_id=f"{provider}-session",
                cwd=tmp_path,
                timeout=60,
                remaining_timeout=remaining_timeout,
            )

        assert compacted is False
        resolve.assert_not_called()
        resume.assert_not_called()

    @pytest.mark.parametrize("provider", ["codex", "opencode"])
    @pytest.mark.parametrize(
        "boundary",
        [
            InterruptedError("compact operation cancelled"),
            subprocess.TimeoutExpired("compact operation deadline", 0),
        ],
        ids=("cancellation", "deadline"),
    )
    def test_compact_authentication_boundary_is_nonfatal(
        self, tmp_path: Path, provider: str, boundary: BaseException
    ) -> None:
        """A cancelled or expired authentication keeps compaction best-effort."""
        remaining_timeout = MagicMock(return_value=60)
        with (
            patch("hephaestus.automation.learn.resolve_agent", side_effect=boundary),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider=provider,
                session_agent="pr-reviewer",
                session_id=f"{provider}-session",
                cwd=tmp_path,
                timeout=60,
                remaining_timeout=remaining_timeout,
            )

        assert compacted is False
        resume.assert_not_called()

    @pytest.mark.parametrize("provider", ["codex", "opencode"])
    @pytest.mark.parametrize(
        "boundary",
        [
            InterruptedError("compact operation cancelled"),
            subprocess.TimeoutExpired("compact operation deadline", 0),
        ],
        ids=("cancellation", "deadline"),
    )
    def test_compact_stops_after_resume_preparation_before_launch(
        self, tmp_path: Path, provider: str, boundary: BaseException
    ) -> None:
        """A bound check after resume preparation prevents provider process start."""
        remaining_timeout = MagicMock(side_effect=[60, boundary])
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value=provider),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider=provider,
                session_agent="pr-reviewer",
                session_id=f"{provider}-session",
                cwd=tmp_path,
                timeout=60,
                remaining_timeout=remaining_timeout,
            )

        assert compacted is False
        resume.assert_not_called()

    @pytest.mark.parametrize("provider", ["codex", "opencode"])
    @pytest.mark.parametrize(
        "failure",
        [
            InterruptedError("compact operation cancelled"),
            subprocess.TimeoutExpired(["provider"], 30),
            subprocess.CalledProcessError(1, ["provider"], stderr="boom"),
            OSError("provider binary vanished"),
        ],
        ids=("cancellation", "timeout", "nonzero-exit", "oserror"),
    )
    def test_compact_run_failure_is_nonfatal(
        self, tmp_path: Path, provider: str, failure: BaseException
    ) -> None:
        """A failed provider run is an ordinary nonfatal compaction failure."""
        remaining_timeout = MagicMock(return_value=60)
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value=provider),
            patch("hephaestus.automation.learn.resume_agent_session", side_effect=failure),
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider=provider,
                session_agent="pr-reviewer",
                session_id=f"{provider}-session",
                cwd=tmp_path,
                timeout=60,
                remaining_timeout=remaining_timeout,
            )

        assert compacted is False

    def test_pi_compact_uses_a_real_session_binding(self, tmp_path: Path) -> None:
        """Pi compaction prepares resume arguments from a real session binding."""
        request = ExecutionRequest(
            AgentRole.PR_REVIEWER,
            AgentOperation.COMPACT,
            SessionLifecycle.RESUME_REQUIRED,
        )
        binding = create_pi_binding(
            session_id="pi-session",
            cwd=tmp_path,
            role=request.role,
            model="private/provider-model:high",
        )
        remaining_timeout = MagicMock(return_value=1200)
        with (
            patch("hephaestus.automation.learn.resolve_agent", return_value="pi"),
            patch("hephaestus.automation.learn.resume_agent_session") as resume,
        ):
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider="pi",
                session_agent="pr-reviewer",
                session_id="pi-session",
                cwd=tmp_path,
                timeout=60,
                model="private/provider-model:high",
                execution_request=request,
                session_binding=binding,
                remaining_timeout=remaining_timeout,
            )

        assert compacted is True
        resume.assert_called_once_with(
            agent="pi",
            session_id="pi-session",
            prompt="/compact",
            cwd=tmp_path,
            timeout=60,
            model="private/provider-model:high",
            sandbox="read-only",
            approval="never",
            disable_pi_automation=False,
            pi_dir=None,
            process_tracker=subprocess_registry.track_process_group,
            remaining_timeout=remaining_timeout,
            execution_request=request,
            resume_binding=binding,
        )


class TestCompactTranscriptPreservation:
    """Compaction must not rewrite the session transcript it compacts."""

    @pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "pi"])
    def test_compaction_preserves_the_session_transcript_bytes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
    ) -> None:
        """Each provider route keeps its isolated transcript fixture byte-identical."""
        monkeypatch.setattr(
            "hephaestus.automation.agent_config._checkout_identity",
            lambda _cwd, *, remaining_timeout=None: "test-checkout",
        )
        sentinel = b'{"type":"message","text":"original turn"}\n' + (b"x" * 4096)

        request: ExecutionRequest | None = None
        binding = None
        session_id: str | None
        if provider == "claude":
            sid = session_uuid("test-repo", 42, AGENT_PR_REVIEWER, cwd=tmp_path)
            session_id = None
            transcript = tmp_path / f"{sid}.jsonl"
        elif provider == "pi":
            request = ExecutionRequest(
                AgentRole.PR_REVIEWER,
                AgentOperation.COMPACT,
                SessionLifecycle.RESUME_REQUIRED,
            )
            binding = create_pi_binding(
                session_id="pi-session",
                cwd=tmp_path,
                role=request.role,
                model="",
            )
            session_id = "pi-session"
            transcript = tmp_path / f"{binding.session_id}.jsonl"
        else:
            session_id = f"{provider}-session"
            transcript = tmp_path / f"{session_id}.jsonl"
        transcript.write_bytes(sentinel)

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "hephaestus.automation.learn.run_subprocess",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                )
            )
            if provider != "claude":
                stack.enter_context(
                    patch("hephaestus.automation.learn.resolve_agent", return_value=provider)
                )
                stack.enter_context(patch("hephaestus.automation.learn.resume_agent_session"))
            compacted = compact_agent_session(
                repo="test-repo",
                issue=42,
                provider=provider,
                session_agent=AGENT_PR_REVIEWER,
                session_id=session_id,
                cwd=tmp_path,
                timeout=60,
                execution_request=request,
                session_binding=binding,
                remaining_timeout=MagicMock(return_value=60),
            )

        assert compacted is True
        assert transcript.read_bytes() == sentinel
