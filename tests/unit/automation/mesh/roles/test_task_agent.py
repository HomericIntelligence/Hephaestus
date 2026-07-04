"""Tests for hephaestus.automation.mesh.roles.task_agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pytest import MonkeyPatch

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
    label_ops: Any = None,
    use_default_merge_gate: bool = False,
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
        label_ops=label_ops,
        merge_gate=(
            None
            if use_default_merge_gate
            else merge_gate
            if merge_gate is not None
            else (lambda pr: True)
        ),
    )
    return handler, calls, driven


class TestMergeGateState:
    """Pure merge-gate decisions over gh pr view payloads."""

    def test_merged_always_passes(self) -> None:
        from hephaestus.automation.mesh.roles.task_agent import _pr_merge_gate_state

        assert _pr_merge_gate_state({"state": "MERGED"})

    def test_armed_with_pending_checks_passes(self) -> None:
        from hephaestus.automation.mesh.roles.task_agent import _pr_merge_gate_state

        data = {
            "state": "OPEN",
            "autoMergeRequest": {"enabledAt": "2026-07-02T00:00:00Z"},
            "statusCheckRollup": [{"conclusion": ""}, {"conclusion": "SUCCESS"}],
        }
        assert _pr_merge_gate_state(data)

    def test_armed_with_failed_check_is_rejected(self) -> None:
        """Armed + a FAILURE check can never merge on its own (ProjectOdyssey#5523)."""
        from hephaestus.automation.mesh.roles.task_agent import _pr_merge_gate_state

        data = {
            "state": "OPEN",
            "autoMergeRequest": {"enabledAt": "2026-07-02T00:00:00Z"},
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}],
        }
        assert not _pr_merge_gate_state(data)

    def test_unarmed_unlabeled_is_rejected(self) -> None:
        from hephaestus.automation.mesh.roles.task_agent import _pr_merge_gate_state

        assert not _pr_merge_gate_state({"state": "OPEN", "labels": []})

    def test_go_label_with_green_checks_passes(self) -> None:
        from hephaestus.automation.mesh.roles.task_agent import _pr_merge_gate_state

        data = {
            "state": "OPEN",
            "labels": [{"name": "state:implementation-go"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        assert _pr_merge_gate_state(data)


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

    def test_unarmed_pr_without_go_label_is_non_retryable_review_failure(self) -> None:
        """A clean CI drive must not complete when the review gate never passed.

        ``_evaluate_run_result`` excuses an un-armed PR lacking
        ``state:implementation-go`` as "pending review" (#1576); in the mesh
        that means the review loop ended NOGO and nothing will ever arm the
        PR, so the task must fail terminally instead of delegating children
        onto an unmerged base (#1780).
        """
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)},
            merge_gate=lambda pr: False,
        )
        result = handler.handle(_ctx({"issue": 9}))
        assert not result.ok
        assert result.error_kind == "ReviewNotGo"
        assert not result.retryable
        assert driven == [9]

    def test_merge_gate_read_failure_is_retryable(self, monkeypatch: MonkeyPatch) -> None:
        def fail_gh_call(_argv: list[str]) -> Any:
            raise RuntimeError("gh pr view failed")

        monkeypatch.setattr("hephaestus.github.client._gh_call", fail_gh_call)
        handler, _, driven = _handler(
            {9: FakeWorkerResult(success=True, pr_number=42)},
            use_default_merge_gate=True,
        )

        result = handler.handle(_ctx({"issue": 9}))

        assert not result.ok
        assert result.error_kind == "MergeGateReadFailed"
        assert result.retryable
        assert result.pr == {"number": 42}
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

    def test_missing_result_with_state_skip_is_non_retryable_review_failure(self) -> None:
        handler, calls, _ = _handler(
            {},
            label_ops=(lambda n: ["state:skip"], lambda labels: "state:skip" in labels),
        )

        result = handler.handle(_ctx({"issue": 9}, attempt=2))

        assert calls == [(9, True)]
        assert not result.ok
        assert result.error_kind == "ReviewNotGo"
        assert not result.retryable

    def test_missing_result_without_state_skip_remains_retryable_no_result(self) -> None:
        handler, _, _ = _handler(
            {},
            label_ops=(lambda n: ["state:plan-go"], lambda labels: "state:skip" in labels),
        )

        result = handler.handle(_ctx({"issue": 9}, attempt=2))

        assert not result.ok
        assert result.error_kind == "NoResult"
        assert result.retryable

    def test_redelivery_does_not_read_or_remove_skip_before_successful_resume(self) -> None:
        reads: list[int] = []

        def read_labels(issue: int) -> list[str]:
            reads.append(issue)
            return ["state:skip"]

        handler, calls, _ = _handler(
            {9: FakeWorkerResult(success=True)},
            label_ops=(read_labels, lambda labels: True),
        )

        result = handler.handle(_ctx({"issue": 9}, attempt=2))

        assert result.ok
        assert calls == [(9, True)]
        assert reads == []

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
