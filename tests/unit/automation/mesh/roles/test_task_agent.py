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


@dataclass
class FakeCIDriverResult:
    """CIDriver per-issue result double."""

    issue_number: int
    success: bool = True
    error: str | None = None
    pr_number: int | None = None


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

    def __init__(
        self,
        log: list[int],
        issue: int,
        results: dict[int, FakeCIDriverResult],
        open_prs_remaining: list[dict[str, Any]],
    ) -> None:
        self._log = log
        self._issue = issue
        self._results = results
        self.open_prs_remaining = open_prs_remaining

    def run(self) -> dict[int, Any]:
        self._log.append(self._issue)
        return self._results


def _handler(
    results: dict[int, FakeWorkerResult],
    ci_results: dict[int, FakeCIDriverResult] | None = None,
    open_prs_remaining: list[dict[str, Any]] | None = None,
    merge_gate: Any = None,
) -> tuple[TaskAgentHandler, list[Any], list[int]]:
    calls: list[Any] = []
    driven: list[int] = []

    def factory(issue: int, resume: bool) -> FakeImplementer:
        calls.append((issue, resume))
        return FakeImplementer(results)

    handler = TaskAgentHandler(
        implementer_factory=factory,
        ci_driver_factory=lambda issue: FakeDriver(
            driven,
            issue,
            ci_results
            or {
                issue: FakeCIDriverResult(
                    issue_number=issue,
                    success=True,
                    pr_number=results[issue].pr_number,
                )
            },
            open_prs_remaining or [],
        ),
        merge_gate=merge_gate if merge_gate is not None else (lambda pr: True),
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

    def test_success_drives_pr_to_merge_ready(self) -> None:
        handler, calls, driven = _handler({9: FakeWorkerResult(success=True, pr_number=42)})
        result = handler.handle(_ctx({"issue": 9}))
        assert result.ok
        assert result.pr == {"number": 42, "merge_ready": True}
        assert calls == [(9, False)]
        assert driven == [9]  # drive-green phase ran

    def test_open_armed_pr_completes_after_successful_ci_driver(self) -> None:
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)},
            ci_results={
                9: FakeCIDriverResult(
                    issue_number=9,
                    success=False,
                    error="timed out waiting for merge",
                    pr_number=42,
                )
            },
            open_prs_remaining=[
                {
                    "number": 42,
                    "autoMergeRequest": {"enabledAt": "2026-07-02T00:00:00Z"},
                    "mergeStateStatus": "BLOCKED",
                }
            ],
        )
        result = handler.handle(_ctx({"issue": 9}))
        assert result.ok
        assert result.pr == {"number": 42, "merge_ready": True}
        assert driven == [9]

    def test_failed_ci_driver_is_retryable_failure(self) -> None:
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)},
            ci_results={
                9: FakeCIDriverResult(
                    issue_number=9,
                    success=False,
                    error="required check failed",
                    pr_number=42,
                )
            },
        )
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "CIDriveFailed"
        assert result.retryable
        assert "required check failed" in result.error_message
        assert driven == [9]

    def test_unarmed_pr_without_go_label_is_retryable_review_failure(self) -> None:
        """A clean CI drive must not complete when the review gate never passed.

        ``_evaluate_run_result`` excuses an un-armed PR lacking
        ``state:implementation-go`` as "pending review" (#1576); in the mesh
        that means the review loop ended NOGO and nothing will ever arm the
        PR, so the task must fail retryably instead of delegating children
        onto an unmerged base (#1780).
        """
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)},
            merge_gate=lambda pr: False,
        )
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "ReviewNotGo"
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

    def test_redelivery_strips_stale_skip_label(self) -> None:
        removed: list[int] = []
        handler, _, _ = _handler({9: FakeWorkerResult(success=True)})
        handler._label_ops = (
            lambda n: ["state:skip"],
            removed.append,
            lambda labels: "state:skip" in labels,
        )
        handler.handle(_ctx({"issue": 9}, attempt=2))
        assert removed == [9]

    def test_redelivery_without_skip_does_not_remove(self) -> None:
        removed: list[int] = []
        handler, _, _ = _handler({9: FakeWorkerResult(success=True)})
        handler._label_ops = (
            lambda n: ["state:plan-go"],
            removed.append,
            lambda labels: "state:skip" in labels,
        )
        handler.handle(_ctx({"issue": 9}, attempt=2))
        assert removed == []

    def test_first_attempt_never_touches_labels(self) -> None:
        fetched: list[int] = []

        def get_labels(n: int) -> list[str]:
            fetched.append(n)
            return []

        handler, _, _ = _handler({9: FakeWorkerResult(success=True)})
        handler._label_ops = (
            get_labels,
            lambda n: None,
            lambda labels: False,
        )
        handler.handle(_ctx({"issue": 9}, attempt=1))
        assert fetched == []

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
