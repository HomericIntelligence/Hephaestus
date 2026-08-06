"""Hermetic orchestration tests for the CI-fix facade."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hephaestus.automation import ci_fix_orchestrator as ci_fix
from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator


def _orchestrator(tmp_path: Path, *, agent: str = "codex") -> CIFixOrchestrator:
    """Build an orchestrator whose external dependencies are all providers."""
    options = SimpleNamespace(agent=agent, agent_timeout=30, dry_run=False)
    return CIFixOrchestrator(
        options_provider=lambda: options,
        repo_root_provider=lambda: tmp_path,
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=MagicMock,
        get_pr_branch=lambda pr: f"{pr}-impl",
        get_worktree_path=lambda _issue, _pr: tmp_path,
        format_review_threads_block=lambda _pr: "",
        failing_required_check_names=lambda _pr: ["unit-tests"],
    )


def test_fresh_direct_agent_result_is_normalized(tmp_path: Path) -> None:
    """A successful direct-agent response becomes a completed process."""
    orchestrator = _orchestrator(tmp_path)
    result = SimpleNamespace(stdout="fixed", stderr=None)

    with (
        patch.object(ci_fix, "uses_direct_agent_runner", return_value=True),
        patch.object(ci_fix, "direct_agent_model", return_value="model"),
        patch.object(ci_fix, "run_agent_session", return_value=result) as run_agent,
    ):
        completed = ci_fix._invoke_agent_session(
            orchestrator,
            prompt="repair",
            session_id=None,
            worktree_path=tmp_path,
            issue_number=1,
            pr_number=2,
        )

    assert completed.returncode == 0
    assert completed.stdout == "fixed"
    assert run_agent.call_args.kwargs["sandbox"] == "workspace-write"


def test_failed_resume_falls_back_to_one_fresh_session(tmp_path: Path) -> None:
    """An expired direct session is replaced by exactly one fresh session."""
    orchestrator = _orchestrator(tmp_path)
    resumed_error = subprocess.CalledProcessError(
        1,
        ["codex"],
        stderr="expired session",
    )

    with (
        patch.object(ci_fix, "direct_agent_model", return_value="model"),
        patch.object(ci_fix, "resume_agent_session", side_effect=resumed_error),
        patch.object(
            ci_fix,
            "run_agent_session",
            return_value=SimpleNamespace(stdout="fresh", stderr=""),
        ) as fresh,
    ):
        completed = ci_fix._invoke_direct_agent_session(
            orchestrator,
            prompt="repair",
            session_id="old-session",
            worktree_path=tmp_path,
            issue_number=1,
            pr_number=2,
        )

    assert completed.stdout == "fresh"
    fresh.assert_called_once()


def test_claude_called_process_error_becomes_failed_result(tmp_path: Path) -> None:
    """A Claude process error is normalized for the caller."""
    orchestrator = _orchestrator(tmp_path, agent="claude")
    error = subprocess.CalledProcessError(7, ["claude"], stderr="agent failed")

    with (
        patch.object(ci_fix, "uses_direct_agent_runner", return_value=False),
        patch.object(ci_fix, "get_repo_slug", return_value="acme/widget"),
        patch.object(ci_fix, "implementer_model", return_value="model"),
        patch.object(ci_fix, "invoke_claude_with_session", side_effect=error),
    ):
        completed = ci_fix._invoke_agent_session(
            orchestrator,
            prompt="repair",
            session_id=None,
            worktree_path=tmp_path,
            issue_number=1,
            pr_number=2,
        )

    assert completed.returncode == 7
    assert completed.stderr == "agent failed"


def test_no_commit_retry_is_bounded_and_records_failure(tmp_path: Path) -> None:
    """Repeated no-commit turns stop at the configured retry bound."""
    orchestrator = _orchestrator(tmp_path)

    with (
        patch.object(orchestrator, "_tracked_worktree_changes", return_value=[]),
        patch.object(orchestrator, "force_engagement_prompt", return_value="retry"),
        patch.object(
            orchestrator,
            "invoke_agent_session",
            return_value=subprocess.CompletedProcess([], 0, "ok", ""),
        ) as invoke,
        patch.object(orchestrator, "_head_advanced", return_value=False),
        patch.object(orchestrator, "record_repeated_no_commit") as record,
    ):
        result = orchestrator.retry_no_commit_once(
            issue_number=1,
            pr_number=2,
            worktree_path=tmp_path,
            pr_head_branch="2-impl",
            pre_agent_sha="before",
            session_id="session",
            max_retries=2,
        )

    assert result is False
    assert invoke.call_count == 2
    record.assert_called_once()
