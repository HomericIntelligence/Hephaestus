"""Tests for hephaestus.automation.mesh.roles.task_agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.roles.task_agent import TaskAgentHandler
from hephaestus.automation.mesh.worker import TaskContext

CFG = MeshConfig(domain="pipeline", role="task-agent", agent_id="a-1", exec_host="h")


@dataclass
class FakeWorkerResult:
    """IssueImplementer per-issue result double."""

    success: bool = True
    error: str | None = None
    pr_number: int | None = None
    plan_review_not_go: bool = False


class FakeImplementer:
    """Implementer double returning canned results."""

    def __init__(self, results: dict[int, FakeWorkerResult]) -> None:
        self.results = results

    def run(self) -> dict[int, FakeWorkerResult]:
        return self.results


def _ctx(payload: dict[str, Any], attempt: int = 1) -> TaskContext:
    ctx = TaskContext(
        config=CFG,
        payload=payload,
        task_id="t-1",
        team_id="mesh",
        attempt=attempt,
        publisher=None,  # type: ignore[arg-type]
        agamemnon=None,  # type: ignore[arg-type]
        deadline=float("inf"),
    )
    ctx.progress = lambda text: None  # type: ignore[method-assign]
    return ctx


class FakeDriver:
    """CIDriver double."""

    def __init__(self, log: list[int], issue: int) -> None:
        self._log = log
        self._issue = issue

    def run(self) -> dict[int, Any]:
        self._log.append(self._issue)
        return {}


def _handler(
    results: dict[int, FakeWorkerResult], pr_state: str = "MERGED"
) -> tuple[TaskAgentHandler, list[Any], list[int]]:
    calls: list[Any] = []
    driven: list[int] = []

    def factory(issue: int, resume: bool) -> FakeImplementer:
        calls.append((issue, resume))
        return FakeImplementer(results)

    handler = TaskAgentHandler(
        implementer_factory=factory,
        ci_driver_factory=lambda issue: FakeDriver(driven, issue),
        pr_state=lambda pr: pr_state,
    )
    return handler, calls, driven


class TestTaskAgentHandler:
    """Tests for the task-agent role."""

    def test_missing_issue_is_non_retryable(self) -> None:
        handler, _, _ = _handler({})
        result = handler.handle(_ctx({}))
        assert not result.ok
        assert result.error_kind == "BadDispatch"
        assert not result.retryable

    def test_success_drives_pr_to_merge(self) -> None:
        handler, calls, driven = _handler({9: FakeWorkerResult(success=True, pr_number=42)})
        result = handler.handle(_ctx({"issue": 9}))
        assert result.ok
        assert result.pr == {"number": 42, "merged": True}
        assert calls == [(9, False)]
        assert driven == [9]  # drive-green phase ran

    def test_unmerged_pr_is_retryable_failure(self) -> None:
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)}, pr_state="OPEN"
        )
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "PRNotMerged"
        assert result.retryable
        assert driven == [9]

    def test_success_without_pr_skips_drive_phase(self) -> None:
        handler, _, driven = _handler({9: FakeWorkerResult(success=True)})
        result = handler.handle(_ctx({"issue": 9}))
        assert result.ok
        assert driven == []

    def test_redelivery_resumes(self) -> None:
        handler, calls, _ = _handler({9: FakeWorkerResult(success=True)})
        handler.handle(_ctx({"issue": 9}, attempt=2))
        assert calls == [(9, True)]

    def test_plan_not_go_is_retryable_failure(self) -> None:
        handler, _, _ = _handler({9: FakeWorkerResult(success=False, plan_review_not_go=True)})
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "PlanNotGo"
        assert result.retryable

    def test_failure_carries_error(self) -> None:
        handler, _, _ = _handler({9: FakeWorkerResult(success=False, error="agent died")})
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "ImplementFailed"
        assert "agent died" in result.error_message

    def test_missing_result_is_retryable(self) -> None:
        handler, _, _ = _handler({})
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "NoResult"
        assert result.retryable
