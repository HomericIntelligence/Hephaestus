"""Test session lookup across registered worktrees."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation import agent_config
from hephaestus.automation.agent_config import (
    AGENT_PLAN_REVIEWER,
    session_jsonl_path,
    session_uuid,
)
from hephaestus.automation.claude_invoke import invoke_claude_with_session
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def test_registered_worktree_resolves_repo_root_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree caller finds a session first created from the repo root."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_root = tmp_path / "owner-a" / "Hephaestus"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(
        agent_config,
        "_checkout_identity",
        lambda _cwd, *, remaining_timeout=None: "checkout-family",
    )
    sid = session_uuid("Hephaestus", 2284, "plan-reviewer", "fable", cwd=repo_root)

    transcript = session_jsonl_path(sid, repo_root)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")

    with patch(
        "hephaestus.automation.agent_config._registered_worktree_roots",
        return_value=(repo_root.resolve(), worktree.resolve()),
    ):
        assert agent_config.resolve_session_jsonl_path(sid, worktree) == transcript


def test_repo_root_turn_then_worker_turn_resumes_same_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root review and worktree pipeline jobs resume one deterministic session."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_root = tmp_path / "owner" / "Repo"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    model = "fable"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        calls.append(argv)
        if len(calls) == 1:
            transcript = session_jsonl_path(sid, repo_root)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(f'{{"cwd": "{repo_root.resolve()}"}}\n', encoding="utf-8")
        return MagicMock(stdout="Verdict: GO", stderr="", returncode=0)

    monkeypatch.setattr(
        agent_config,
        "_registered_worktree_roots",
        lambda _cwd, *, remaining_timeout=None: (repo_root.resolve(), worktree.resolve()),
    )
    monkeypatch.setattr(
        agent_config,
        "_checkout_identity",
        lambda _cwd, *, remaining_timeout=None: "checkout-family",
    )
    # Compute sid after patches so it uses the patched _checkout_identity.
    sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, model, cwd=repo_root)

    completions: queue.Queue[tuple[JobHandle, JobResult]] = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        lock_dir=tmp_path / "locks",
    )
    try:
        with (
            patch("hephaestus.automation.claude_invoke._run_tracked", side_effect=fake_run),
            patch(
                "hephaestus.automation.pipeline.worker_pool.resolve_agent",
                return_value="claude",
            ),
        ):
            _, first_sid = invoke_claude_with_session(
                repo="Repo",
                issue=2284,
                agent=AGENT_PLAN_REVIEWER,
                prompt="plan review",
                model=model,
                cwd=repo_root,
            )
            assert first_sid == sid
            job = AgentJob(
                repo="Repo",
                issue=2284,
                agent="claude",
                session_agent=AGENT_PLAN_REVIEWER,
                model=model,
                prompt_builder=lambda: "pipeline prompt",
                cwd=worktree,
                timeout_s=60,
                descr="resume review session",
                workspace=WorkspaceBinding.external(worktree),
            )
            pool.submit(job, StageName.PLAN_REVIEW)
            _, result = completions.get(timeout=10)
    finally:
        pool.shutdown()

    assert result.ok is True
    assert "--session-id" in calls[0]
    assert "--resume" not in calls[0]
    assert "--resume" in calls[1]
    assert "--session-id" not in calls[1]
    assert sid in calls[0]
    assert sid in calls[1]
