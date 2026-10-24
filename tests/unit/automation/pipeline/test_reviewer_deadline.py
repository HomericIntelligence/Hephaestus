"""Worker-pool deadline behavior for reviewer-sized agent budgets (#3185).

The reviewer stages bound their jobs with the raised ``AGENT_REVIEW_TIMEOUT``
default (3600s).  These tests pin that the worker pool hands the full
remaining budget to the provider invocation, clamps it to an explicit job
deadline (the coordinator's ``--phase-timeout`` wiring), and fails closed
when the deadline has already passed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.constants import AGENT_REVIEW_TIMEOUT
from tests.unit.automation.pipeline.conftest import FakeSigningProvider

_WP = "hephaestus.automation.pipeline.worker_pool"
_START_S = 100.0
_REVIEW_DURATION_S = 1473  # a slow review that still fits the default budget
_PHASE_BUDGET_S = 1847  # smaller than the reviewer default


@pytest.fixture
def shutdown_event() -> threading.Event:
    """Fresh shutdown event for each test."""
    return threading.Event()


@pytest.fixture
def pool(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> Iterator[WorkerPool]:
    """Worker pool with a single thread and a temp cross-process lock dir."""
    p = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(None, "unit-worker", FakeSigningProvider()),
    )
    yield p
    p.shutdown()


def _reviewer_job(cwd: Path, **overrides: object) -> AgentJob:
    """Build a reviewer agent job with an external binding to ``cwd``."""
    defaults: dict[str, object] = {
        "repo": "test/repo",
        "issue": 3185,
        "agent": "codex",
        "model": "unit-reviewer",
        "prompt_builder": lambda: "review prompt",
        "cwd": cwd,
        "timeout_s": AGENT_REVIEW_TIMEOUT,
        "descr": "review",
    }
    defaults.update(overrides)
    if "workspace" not in overrides:
        root = Path(str(defaults["cwd"]))
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise ValueError("Repository tests must supply their source workspace binding")
        defaults["workspace"] = WorkspaceBinding.external(root)
    return AgentJob(**defaults)  # type: ignore[arg-type]


def test_full_reviewer_budget_reaches_the_provider_invocation(
    pool: WorkerPool, tmp_path: Path
) -> None:
    """A 1473s review fits the raised reviewer default and returns cleanly."""
    clock = [_START_S]
    job = _reviewer_job(tmp_path)

    def reviewed(**kwargs: object) -> AgentRunResult:
        clock[0] += _REVIEW_DURATION_S
        assert kwargs["timeout"] == AGENT_REVIEW_TIMEOUT
        return AgentRunResult("verdict: GO", "", "session-review")

    with (
        patch(f"{_WP}.time.monotonic", side_effect=lambda: clock[0]),
        patch(f"{_WP}.resolve_agent", return_value="codex"),
        patch(f"{_WP}.run_agent_session", side_effect=reviewed) as run,
    ):
        result = pool._run_agent(job)

    assert result.ok is True
    run.assert_called_once()


def test_expired_reviewer_deadline_fails_closed(pool: WorkerPool, tmp_path: Path) -> None:
    """An expired reviewer deadline never reaches provider resolution."""
    job = _reviewer_job(tmp_path, deadline_s=_START_S - 1.0)

    with (
        patch(f"{_WP}.time.monotonic", return_value=_START_S),
        patch(f"{_WP}.resolve_agent") as resolve,
        patch(f"{_WP}.run_agent_session") as run,
    ):
        result = pool._run_agent(job)

    assert result.ok is False
    assert result.error == "timeout"
    resolve.assert_not_called()
    run.assert_not_called()


def test_explicit_job_deadline_clamps_the_reviewer_default(
    pool: WorkerPool, tmp_path: Path
) -> None:
    """A coordinator-set deadline bounds the run below the 3600s reviewer default."""
    clock = [_START_S]
    job = _reviewer_job(tmp_path, deadline_s=_START_S + _PHASE_BUDGET_S)

    def reviewed(**kwargs: object) -> AgentRunResult:
        assert kwargs["timeout"] == _PHASE_BUDGET_S
        return AgentRunResult("verdict: GO", "", "session-review")

    with (
        patch(f"{_WP}.time.monotonic", side_effect=lambda: clock[0]),
        patch(f"{_WP}.resolve_agent", return_value="codex"),
        patch(f"{_WP}.run_agent_session", side_effect=reviewed) as run,
    ):
        result = pool._run_agent(job)

    assert result.ok is True
    run.assert_called_once()
