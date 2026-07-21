"""Regression coverage for Claude sessions shared by root and worktree callers."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import agent_config
from hephaestus.automation.models import PlanReviewerOptions
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.session_naming import (
    AGENT_PLAN_REVIEWER,
    session_jsonl_path,
    session_uuid,
)


def test_plan_reviewer_then_pipeline_worker_resumes_same_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root review and worktree pipeline jobs resume one deterministic session."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_root = tmp_path / "owner" / "Repo"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    model = "fable"
    sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, model)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        calls.append(argv)
        if len(calls) == 1:
            transcript = session_jsonl_path(sid, repo_root)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("{}\n", encoding="utf-8")
        return MagicMock(stdout="Verdict: GO", stderr="", returncode=0)

    monkeypatch.setattr(
        agent_config,
        "_registered_worktree_roots",
        lambda _cwd: (repo_root.resolve(), worktree.resolve()),
    )
    reviewer = PlanReviewer(PlanReviewerOptions(issues=[2284], enable_ui=False))
    completions: queue.Queue[object] = queue.Queue()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=completions,
        lock_dir=tmp_path / "locks",
    )
    try:
        with (
            patch("hephaestus.automation.claude_invoke._run_tracked", side_effect=fake_run),
            patch("hephaestus.automation.plan_reviewer.get_repo_root", return_value=repo_root),
            patch("hephaestus.automation.plan_reviewer.get_repo_slug", return_value="Repo"),
            patch("hephaestus.automation.plan_reviewer.reviewer_model", return_value=model),
            patch(
                "hephaestus.automation.pipeline.worker_pool.resolve_agent",
                return_value="claude",
            ),
        ):
            assert (
                reviewer._run_claude_analysis(2284, "title", "body", "# Implementation Plan")
                == "Verdict: GO"
            )
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
