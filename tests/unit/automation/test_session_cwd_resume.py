"""Regression tests for checkout-scoped Claude session transcript lookup."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import agent_config
from hephaestus.automation.models import PlanReviewerOptions
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.plan_reviewer import PlanReviewer
from hephaestus.automation.session_naming import session_jsonl_path, session_uuid


def test_registered_worktree_resolves_repo_root_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree caller finds a session first created from the repo root."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_root = tmp_path / "owner-a" / "Hephaestus"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    sid = session_uuid("Hephaestus", 2284, "plan-reviewer", "fable")

    transcript = session_jsonl_path(sid, repo_root)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")

    with patch(
        "hephaestus.automation.agent_config._registered_worktree_roots",
        return_value=(repo_root.resolve(), worktree.resolve()),
    ):
        assert agent_config.resolve_session_jsonl_path(sid, worktree) == transcript


def test_plan_reviewer_then_pipeline_worker_resumes_same_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo-root and pipeline-worktree Claude paths share one session."""
    repo_root = tmp_path / "owner-a" / "Hephaestus"
    worktree = repo_root / "build" / ".worktrees" / "issue-2284"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    model = "fable"
    sid = session_uuid("Hephaestus", 2284, "plan-reviewer", model)
    calls: list[list[str]] = []

    def fake_run_tracked(argv: list[str], **_kwargs: object) -> MagicMock:
        calls.append(argv)
        if len(calls) == 1:
            transcript = session_jsonl_path(sid, repo_root)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text("{}\n", encoding="utf-8")
        return MagicMock(stdout="Verdict: GO", stderr="", returncode=0)

    reviewer = PlanReviewer(
        PlanReviewerOptions(issues=[2284], dry_run=False, max_workers=1, enable_ui=False)
    )
    completion_q: CompletionQueue = queue.Queue()
    shutdown = threading.Event()
    pool = WorkerPool(size=1, shutdown=shutdown, completion_q=completion_q, lock_dir=tmp_path)

    try:
        with (
            patch(
                "hephaestus.automation.agent_config._registered_worktree_roots",
                return_value=(repo_root.resolve(), worktree.resolve()),
            ),
            patch("hephaestus.automation.claude_invoke._run_tracked", side_effect=fake_run_tracked),
            patch("hephaestus.automation.plan_reviewer.get_repo_root", return_value=repo_root),
            patch(
                "hephaestus.automation.plan_reviewer.get_repo_slug",
                return_value="Hephaestus",
            ),
            patch("hephaestus.automation.plan_reviewer.reviewer_model", return_value=model),
            patch(
                "hephaestus.automation.pipeline.worker_pool.resolve_agent",
                return_value="claude",
            ),
        ):
            assert reviewer._run_claude_analysis(2284, "title", "body", "plan") == "Verdict: GO"
            job = AgentJob(
                repo="Hephaestus",
                issue=2284,
                agent="claude",
                session_agent="plan-reviewer",
                model=model,
                prompt_builder=lambda: "pipeline turn",
                cwd=worktree,
                timeout_s=60,
            )
            pool.submit(job, StageName.PLAN_REVIEW)
            _handle, result = completion_q.get(timeout=10)

            assert agent_config.resolve_session_jsonl_path(
                sid, repo_root
            ) == agent_config.resolve_session_jsonl_path(sid, worktree)

        assert result.ok is True
    finally:
        pool.shutdown(mark_interrupted=False)

    assert "--session-id" in calls[0]
    assert "--resume" not in calls[0]
    assert "--resume" in calls[1]
    assert "--session-id" not in calls[1]
    assert sid in calls[0] and sid in calls[1]
