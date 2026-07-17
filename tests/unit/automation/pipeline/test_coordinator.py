"""Coordinator event-loop tests (epic #1809, #1817).

Covers: quiescence with FakeWorkerPool/FakeStageGitHub, the journal-order
invariant (durable mutation precedes the queue push in one shared trace),
downstream-first drain order, per-repo in-flight cap, zero-work convergence,
the loop budget, the non-blocking rate-budget park, dry-run
asserts-no-submit, poisoned-item isolation, and FAIL_BACK routing.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from hephaestus.automation.claude_invoke import ReviewVerdict
from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import (
    _FAIL_BACK_CAP,
    Coordinator,
    PipelineConfig,
)
from hephaestus.automation.pipeline.events import (
    PrReviewZeroThreadNogoEvent,
    ZeroThreadNogoAction,
)
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import (
    Disposition,
    PipelineScope,
    StageName,
    StageOutcome,
)
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages import Continue, StrictReviewEvidence
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.stages.pr_review import (
    REVIEW_ERROR_RETRY_CAP,
    PrReviewStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.resilience import (
    all_circuit_breaker_snapshots,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _agent_job(repo: str = "repo-a", issue: int = 1) -> AgentJob:
    return AgentJob(
        repo=repo,
        issue=issue,
        agent="claude",
        model="m",
        prompt_builder=lambda **kwargs: "prompt",
        cwd=Path("/tmp"),
        timeout_s=10,
        descr="stub agent job",
    )


class StubStage:
    """Scripted stage: each step() pops the next scripted StepResult."""

    def __init__(self, *results: Any, enter: Any = None) -> None:
        self.results = deque(results)
        self.enter_result = enter
        self.calls: list[tuple[str, Any]] = []

    def on_enter(self, item: WorkItem, ctx: Any) -> Any:
        self.calls.append(("enter", item.issue or item.repo))
        return self.enter_result

    def step(self, item: WorkItem, ctx: Any) -> Any:
        self.calls.append(("step", item.issue or item.repo))
        if not self.results:
            return StageOutcome(Disposition.FINISH_FAIL, "script exhausted")
        return self.results.popleft()

    def on_job_done(self, item: WorkItem, result: JobResult, ctx: Any) -> None:
        self.calls.append(("job_done", result.ok))


def make_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repos: list[str] | None = None,
    seed_entries: list[list[SeedEntry]] | None = None,
    loops: int = 1,
    max_workers: int = 1,
    dry_run: bool = False,
    serialize_file_overlap: bool = True,
    github: FakeStageGitHub | None = None,
    rate_budget_ok: Callable[[], tuple[bool, float]] | None = None,
) -> tuple[Coordinator, FakeWorkerPool, FakeStageGitHub]:
    """Build a Coordinator wired to fakes, with seeding scripted per pass."""
    config = PipelineConfig(
        org="org",
        repos=repos if repos is not None else ["repo-a"],
        loops=loops,
        max_workers=max_workers,
        dry_run=dry_run,
        serialize_file_overlap=serialize_file_overlap,
        projects_dir=tmp_path,
    )
    gh = github or FakeStageGitHub()
    pool = FakeWorkerPool()
    passes = deque(seed_entries or [[]])

    def fake_seed(repos_arg: Any, issues_arg: Any, prs_arg: Any) -> list[SeedEntry]:
        return list(passes.popleft()) if passes else []

    monkeypatch.setattr(seeding_mod, "seed_from_cli", fake_seed)
    coordinator = Coordinator(config, github=gh, pool=pool, install_signals=False)
    coordinator._rate_budget_ok = rate_budget_ok or (lambda: (True, 0.0))  # type: ignore[method-assign]
    return coordinator, pool, gh


def _issue_item(
    issue: int = 1, stage: StageName = StageName.PLANNING, repo: str = "repo-a"
) -> WorkItem:
    return WorkItem(repo=repo, kind=ItemKind.ISSUE, issue=issue, stage=stage, state="ENTER")


class TestQuiescence:
    """Full-run tests driving seeded items to the finished ledger."""

    def test_metrics_server_starts_for_run_and_stops_on_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coordinator teardown closes the explicit local listener on every normal exit."""
        from hephaestus.observability import server as server_mod

        created: list[object] = []

        class FakeMetricsServer:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs
                self.started = False
                self.stopped = False
                created.append(self)

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.stopped = True

        monkeypatch.setattr(server_mod, "MetricsHTTPServer", FakeMetricsServer)
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            PipelineConfig(
                org="org", repos=["repo-a"], loops=1, projects_dir=tmp_path, metrics_port=9123
            ),
            github=FakeStageGitHub(),
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        assert coordinator.run() == 0
        assert len(created) == 1
        assert created[0].started is True  # type: ignore[attr-defined]
        assert created[0].stopped is True  # type: ignore[attr-defined]

    def test_explicit_issue_scope_suppresses_repo_discovery_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--issues N scopes the run to N instead of reconstructing the whole repo."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[1850],
            loops=1,
            projects_dir=tmp_path,
        )
        gh = FakeStageGitHub(merged_pr=1851)

        def fake_seed(
            repos_arg: list[str], issues_arg: list[int], prs_arg: list[int]
        ) -> list[SeedEntry]:
            assert repos_arg == []
            assert issues_arg == []
            assert prs_arg == []
            return []

        monkeypatch.setattr(seeding_mod, "seed_from_cli", fake_seed)
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
            lambda _repo, issues: list(issues),
        )
        coordinator = Coordinator(
            config,
            github=gh,
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        assert coordinator.run() == 0
        assert [item.issue for item in coordinator.items] == [1850]
        assert all(item.kind is not ItemKind.REPO for item in coordinator.items)

    def test_repo_products_flow_to_finished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo seed's products traverse their entry stages into the ledger."""
        seed = [SeedEntry(kind="repo", identifier="repo-a", stage=StageName.REPO, reason="seed")]
        coordinator, pool, _ = make_coordinator(tmp_path, monkeypatch, seed_entries=[seed])

        class ProducingRepoStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                item.payload["products"] = [
                    {"kind": "issue", "number": 11, "stage": StageName.PLANNING, "reason": "r"},
                    {"kind": "issue", "number": 12, "stage": None, "reason": "excluded"},
                ]
                return StageOutcome(Disposition.FINISH_PASS, "seeded:1")

        coordinator.stages[StageName.REPO] = ProducingRepoStage()
        coordinator.stages[StageName.PLANNING] = StubStage(
            StageOutcome(Disposition.ADVANCE, "planned")
        )
        coordinator.stages[StageName.PLAN_REVIEW] = StubStage(
            StageOutcome(Disposition.FINISH_PASS, "done")
        )

        exit_code = coordinator.run()

        assert exit_code == 0
        assert len(coordinator.ledger) == 2  # repo item + issue item
        assert all(result.passed for result in coordinator.ledger)
        assert len(pool.submitted) == 0
        keys = [key for kind, *key in coordinator.event_log if kind == "push"]
        assert ["planning", "repo-a#11"] in [list(k) for k in keys]

    def test_zero_work_convergence_exits_before_loop_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All-terminal seeds produce zero actionable work: exit after one pass."""
        seed = [
            SeedEntry(kind="issue", identifier=5, stage=StageName.FINISHED, reason="PR merged"),
        ]
        coordinator, _, _ = make_coordinator(
            tmp_path, monkeypatch, seed_entries=[seed, seed, seed], loops=5
        )

        exit_code = coordinator.run()

        assert exit_code == 0
        assert coordinator._loops_run == 1  # converged, budget not consumed
        assert len(coordinator.ledger) == 1
        assert coordinator.ledger[0].passed

    def test_loop_budget_bounds_reseeding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Actionable work each pass re-seeds only up to --loops."""
        seed = [SeedEntry(kind="issue", identifier=7, stage=StageName.PLANNING, reason="r")]
        coordinator, _, _ = make_coordinator(
            tmp_path, monkeypatch, seed_entries=[seed, seed, seed, seed], loops=2
        )
        coordinator.stages[StageName.PLANNING] = StubStage(
            StageOutcome(Disposition.SKIP, "skip"),
            StageOutcome(Disposition.SKIP, "skip"),
        )

        exit_code = coordinator.run()

        assert coordinator._loops_run == 2
        assert exit_code == 1  # SKIP counts as non-passing (fail-skip-blocked)
        assert [result.reason for result in coordinator.ledger] == ["skip: skip", "skip: skip"]

    def test_reseed_with_only_repo_seeds_is_zero_work_convergence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo pushes do not keep a zero-work reseed alive."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch, loops=2)
        coordinator._loops_run = 1
        coordinator._pass_work_count = 1

        def reseed_repo_only() -> int:
            coordinator._pass_work_count = 0
            return 1

        monkeypatch.setattr(coordinator, "_seed_pass", reseed_repo_only)

        assert coordinator._reseed_if_converged() is False
        assert coordinator._loops_run == 2

    def test_poisoned_item_routes_finished_fail_and_loop_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stage exception fails the ITEM, not the loop."""
        seed = [
            SeedEntry(kind="issue", identifier=1, stage=StageName.PLANNING, reason="poison"),
            SeedEntry(kind="issue", identifier=2, stage=StageName.PLAN_REVIEW, reason="ok"),
        ]
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch, seed_entries=[seed])

        class PoisonStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                raise RuntimeError("boom")

        coordinator.stages[StageName.PLANNING] = PoisonStage()
        coordinator.stages[StageName.PLAN_REVIEW] = StubStage(
            StageOutcome(Disposition.FINISH_PASS, "fine")
        )

        exit_code = coordinator.run()

        assert exit_code == 1
        reasons = sorted(result.reason for result in coordinator.ledger)
        assert any(reason.startswith("poisoned: boom") for reason in reasons)
        assert any(reason == "fine" for reason in reasons)

    def test_direct_pr_worktree_completion_advances_without_resubmitting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coordinator completion order preserves a direct PR's synced worktree."""
        head = "a" * 40
        github = FakeStageGitHub(
            pr_state={"state": "OPEN", "headRefOid": head, "autoMergeRequest": None},
            pr_head_branch="strict-review-pr-601",
            strict_evidence=StrictReviewEvidence(
                head_sha=head,
                issue_title="Strict-review task",
                issue_body="Review the current head.",
                diff="diff --git a/file.py b/file.py\n+change",
                ci_status="checks clean",
                prior_pr_review_verdict="Grade: A\nVerdict: GO",
            ),
        )
        coordinator, pool, _ = make_coordinator(tmp_path, monkeypatch, github=github)
        pool.queue_result(JobResult(ok=True, value={"path": str(tmp_path / "pr-601")}))
        item = WorkItem(
            repo="repo-a",
            kind=ItemKind.PR,
            issue=1,
            pr=601,
            stage=StageName.STRICT_REVIEW,
            state="HEAD_CHECK",
        )

        coordinator._run_item(item)
        coordinator._drain_completions()

        worktree_jobs = [
            handle.job
            for handle in pool.submitted
            if getattr(handle.job, "op", "") == "create_worktree"
        ]
        assert len(worktree_jobs) == 1
        assert item.worktree == str(tmp_path / "pr-601")
        assert any(isinstance(handle.job, AgentJob) for handle in pool.submitted)


def _fake_in_flight_item(coordinator: Coordinator, item: WorkItem) -> JobHandle:
    """Register *item* as in-flight under a synthetic handle (no real submit)."""
    handle = JobHandle(
        job=AgentJob(
            repo=item.repo,
            issue=item.issue or 0,
            agent="claude",
            model="m",
            prompt_builder=lambda **_kw: "p",
            cwd=Path("."),
            timeout_s=1,
        ),
        on_done_state=item.stage,
    )
    coordinator.in_flight[handle] = item
    coordinator.inflight_per_repo[item.repo] += 1
    return handle


class TestFatalTeardown:
    """Fatal-exception exit must reap the pool + park in-flight items (#2059)."""

    def test_fatal_exception_shuts_down_pool_and_parks_in_flight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fatal (non-signal) exit cancels the pool and never leaks in-flight work.

        Before #2059, ``run()``'s finally only reaped on the signal path
        (``self.shutdown`` set), so a fatal exception left the executor and its
        in-flight AgentJob subprocesses running.
        """
        coordinator, pool, _ = make_coordinator(tmp_path, monkeypatch)
        in_flight = _issue_item(42, StageName.IMPLEMENTATION)
        _fake_in_flight_item(coordinator, in_flight)

        def boom() -> None:
            raise RuntimeError("fatal boom")

        # Raise OUTSIDE _run_item's per-item guard so it reaches run()'s except.
        monkeypatch.setattr(coordinator, "_drain_queues", boom)

        exit_code = coordinator.run()

        # Fatal, not interrupt: shutdown must NOT be set (would mis-report 130).
        assert not coordinator.shutdown.is_set()
        assert coordinator._fatal is True
        assert exit_code == 1
        # Pool reaped exactly once; in-flight maps cleared.
        assert pool.shutdown_calls == 1
        assert coordinator.in_flight == {}
        assert not coordinator.inflight_per_repo
        # The in-flight item was parked RESUMABLE (never silently dropped).
        assert in_flight.result is not None
        assert not in_flight.result.passed
        assert in_flight.result.reason == "resumable at implementation"

    def test_signal_teardown_shuts_down_pool_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Signal path + finally must not double-shutdown the pool (idempotent guard)."""
        coordinator, pool, _ = make_coordinator(tmp_path, monkeypatch)
        in_flight = _issue_item(7, StageName.IMPLEMENTATION)
        _fake_in_flight_item(coordinator, in_flight)

        # Trip an immediate (second-signal) shutdown before the first tick's
        # top-of-loop check, so _teardown_immediate runs and breaks the loop.
        original_seed = coordinator._seed_pass

        def seed_then_immediate() -> int:
            pushed = original_seed()
            coordinator._immediate = True
            return pushed

        monkeypatch.setattr(coordinator, "_seed_pass", seed_then_immediate)

        exit_code = coordinator.run()

        assert coordinator.shutdown.is_set()
        assert exit_code == 130  # interrupt
        assert pool.shutdown_calls == 1  # _teardown_immediate + finally == once
        assert coordinator.in_flight == {}
        assert in_flight.result is not None
        assert in_flight.result.reason == "resumable at implementation"


class TestJournalOrder:
    """The durable-mutation-precedes-queue-push invariant, in one shared trace."""

    def test_durable_mutation_precedes_queue_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every push of a mutated item appears AFTER its durable write."""
        seed = [SeedEntry(kind="issue", identifier=9, stage=StageName.PLANNING, reason="r")]
        coordinator, _, gh = make_coordinator(tmp_path, monkeypatch, seed_entries=[seed])

        original_log = gh._log

        def shared_log(name: str, *args: Any) -> None:
            original_log(name, *args)
            coordinator.event_log.append(("mutation", name, args))

        gh._log = shared_log  # type: ignore[method-assign]

        class MutatingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                # Durable write immediately before the ADVANCE outcome that
                # causes the queue push (the house journal-order pattern).
                ctx.github.add_labels(item.issue, ["state:plan-go"])
                return StageOutcome(Disposition.ADVANCE, "go")

        coordinator.stages[StageName.PLANNING] = MutatingStage()
        coordinator.stages[StageName.PLAN_REVIEW] = StubStage(
            StageOutcome(Disposition.FINISH_PASS, "done")
        )

        coordinator.run()

        trace = coordinator.event_log
        mutation_idx = next(
            i for i, entry in enumerate(trace) if entry[:2] == ("mutation", "gh_issue_add_labels")
        )
        push_idx = next(i for i, entry in enumerate(trace) if entry[:2] == ("push", "plan_review"))
        assert mutation_idx < push_idx, f"push preceded durable mutation: {trace}"


class TestDrainOrder:
    """Downstream-first queue draining."""

    def test_downstream_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """merge_wait drains before ci ... before planning before repo."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch)
        for stage in (
            StageName.PLANNING,
            StageName.MERGE_WAIT,
            StageName.CI,
            StageName.REPO,
        ):
            coordinator.stages[stage] = StubStage(StageOutcome(Disposition.FINISH_PASS, "x"))
        coordinator._push_item(_issue_item(1, StageName.PLANNING), StageName.PLANNING, enter=True)
        coordinator._push_item(
            _issue_item(2, StageName.MERGE_WAIT), StageName.MERGE_WAIT, enter=True
        )
        coordinator._push_item(_issue_item(3, StageName.CI), StageName.CI, enter=True)
        repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
        coordinator._push_item(repo_item, StageName.REPO, enter=True)
        coordinator.event_log.clear()

        coordinator._drain_queues()

        drained = [entry[1] for entry in coordinator.event_log if entry[0] == "drain"]
        assert drained.index("merge_wait") < drained.index("ci")
        assert drained.index("ci") < drained.index("planning")
        assert drained.index("planning") < drained.index("repo")


class TestAdmission:
    """Per-repo in-flight cap via the distinct inflight_per_repo Counter."""

    def test_per_repo_cap_defers_second_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_workers=1: the second same-repo item is not admitted."""
        coordinator, pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=1)
        coordinator.stages[StageName.PLANNING] = StubStage(
            JobRequest(_agent_job(issue=1), on_done_state="VERIFY"),
            JobRequest(_agent_job(issue=2), on_done_state="VERIFY"),
        )
        coordinator._push_item(_issue_item(1), StageName.PLANNING, enter=True)
        coordinator._push_item(_issue_item(2), StageName.PLANNING, enter=True)

        coordinator._drain_queues()

        assert len(pool.submitted) == 1
        assert coordinator.inflight_per_repo["repo-a"] == 1
        assert len(coordinator.queues[StageName.PLANNING]) == 1

    def test_cap_is_per_repo_not_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Items of different repos are admitted independently."""
        coordinator, pool, _ = make_coordinator(
            tmp_path, monkeypatch, repos=["repo-a", "repo-b"], max_workers=1
        )
        coordinator.stages[StageName.PLANNING] = StubStage(
            JobRequest(_agent_job(repo="repo-a", issue=1), on_done_state="V"),
            JobRequest(_agent_job(repo="repo-b", issue=2), on_done_state="V"),
        )
        coordinator._push_item(_issue_item(1, repo="repo-a"), StageName.PLANNING, enter=True)
        coordinator._push_item(_issue_item(2, repo="repo-b"), StageName.PLANNING, enter=True)

        coordinator._drain_queues()

        assert len(pool.submitted) == 2
        assert coordinator.inflight_per_repo["repo-a"] == 1
        assert coordinator.inflight_per_repo["repo-b"] == 1


class TestRateBudget:
    """The non-blocking rate gate parks agent jobs on the timer heap."""

    def test_low_budget_parks_instead_of_submitting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Low GraphQL budget: the AgentJob is timer-parked, never submitted."""
        coordinator, pool, _ = make_coordinator(
            tmp_path,
            monkeypatch,
            rate_budget_ok=lambda: (False, 60.0),
        )
        coordinator.stages[StageName.PLANNING] = StubStage(
            JobRequest(_agent_job(), on_done_state="VERIFY")
        )
        coordinator._push_item(_issue_item(1), StageName.PLANNING, enter=True)

        coordinator._drain_queues()

        assert pool.submitted == []
        assert len(coordinator.timers) == 1
        assert any(entry[0] == "timer_park" for entry in coordinator.event_log)


class TestDryRun:
    """Dry-run: stages' JobRequests are logged-and-advanced; _submit asserts."""

    def test_job_request_advances_without_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[dry-run] a requested job is logged and the item ADVANCEs."""
        seed = [SeedEntry(kind="issue", identifier=3, stage=StageName.PLANNING, reason="r")]
        coordinator, pool, _ = make_coordinator(
            tmp_path, monkeypatch, seed_entries=[seed], dry_run=True
        )
        coordinator.stages[StageName.PLANNING] = StubStage(
            JobRequest(_agent_job(issue=3), on_done_state="VERIFY")
        )
        coordinator.stages[StageName.PLAN_REVIEW] = StubStage(
            StageOutcome(Disposition.FINISH_PASS, "done")
        )

        exit_code = coordinator.run()

        assert exit_code == 0
        assert pool.submitted == []
        assert not any(entry[0] == "submit" for entry in coordinator.event_log)

    def test_submit_asserts_in_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_submit is guarded by an assert: dry-run must never reach it."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch, dry_run=True)
        request = JobRequest(_agent_job(), on_done_state="VERIFY")

        with pytest.raises(AssertionError, match="dry-run must never submit"):
            coordinator._submit(_issue_item(1), request)


class TestFailBackRouting:
    """The Disposition->action table's FAIL_BACK rows."""

    def test_named_reason_routes_to_mapped_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pr_review FAIL_BACK(agent_error) regresses to implementation."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch)
        item = _issue_item(4, StageName.PR_REVIEW)
        coordinator._push_item(item, StageName.PR_REVIEW, enter=False)
        coordinator.event_log.clear()

        coordinator._route(item, StageOutcome(Disposition.FAIL_BACK, "agent_error"))

        assert item.stage is StageName.IMPLEMENTATION
        assert len(coordinator.queues[StageName.IMPLEMENTATION]) == 1

    def test_unknown_reason_uses_default_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unmapped reason falls back to the '*' target."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch)
        item = _issue_item(5, StageName.PLANNING)

        coordinator._route(item, StageOutcome(Disposition.FAIL_BACK, "mystery"))

        # planning "*" -> finished(fail)
        assert item.stage is StageName.FINISHED
        assert item.result is not None and not item.result.passed

    def test_dry_run_fail_back_finishes_instead_of_regressing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry-run FAIL_BACK finishes with the would-regress note.

        Dry-run cannot write gate labels, so a real regression would
        ping-pong until the safety cap while burning live reads.
        """
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch, dry_run=True)
        item = _issue_item(7, StageName.IMPLEMENTATION)

        coordinator._route(item, StageOutcome(Disposition.FAIL_BACK, "plan_not_go"))

        assert item.stage is StageName.FINISHED
        assert item.result is not None
        assert item.result.reason == "[dry-run] would fail_back: plan_not_go"

    def test_fail_back_safety_cap_terminates_cycles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pathological regress cycle terminates at the global cap."""
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch)
        item = _issue_item(6, StageName.PR_REVIEW)
        item.payload["_fail_backs"] = _FAIL_BACK_CAP

        coordinator._route(item, StageOutcome(Disposition.FAIL_BACK, "agent_error"))

        assert item.stage is StageName.FINISHED
        assert item.result is not None
        assert "safety cap" in item.result.reason


class TestImplementationAdmission:
    """Topological order + file-overlap reuse for the implementation queue."""

    def test_duplicate_issue_numbers_collapse_to_first_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duplicate issue work item is dropped, not fatal (#1952 regression).

        A transient retry/fail-back can re-enqueue an issue while a prior copy
        is still queued. The drain must keep the first-queued item, terminalize
        the later duplicate as superseded, and dispatch normally — never crash
        the whole org-wide run.
        """
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        ran: list[int] = []

        class RecordingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                ran.append(item.issue or 0)
                return StageOutcome(Disposition.SKIP, "recorded")

        coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
        first = _issue_item(21, StageName.IMPLEMENTATION)
        duplicate = _issue_item(21, StageName.IMPLEMENTATION)
        # Inject the collision BELOW the upstream idempotency guard (#2107):
        # push straight onto the queue (the same raw API the drain uses to
        # re-push deferred items, coordinator.py:873) so this test exercises
        # the drain-level safety net for a duplicate that arrived by a path
        # the upstream guard does not cover.
        for it in (first, duplicate):
            it.payload["_enter_pending"] = True
            coordinator._seen_item_ids.add(id(it))
            coordinator.items.append(it)
            coordinator.queues[StageName.IMPLEMENTATION].push(it)

        coordinator._drain_implementation()

        # First-queued item dispatched exactly once; duplicate never ran.
        assert ran == [21]
        # The duplicate was terminalized as superseded (kept out of dispatch).
        assert duplicate.stage is StageName.FINISHED
        assert duplicate.result is not None
        assert duplicate.result.passed
        assert "superseded" in duplicate.result.reason
        # Implementation queue is drained — no duplicate left behind.
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []
        # Repo-scoped reason string (#2057).
        assert duplicate.result.reason == "repo-a#21 superseded by queued duplicate"

    def test_cross_repo_same_issue_number_both_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two repos sharing an issue number are DISTINCT work — never collapsed (#2057).

        The implementation queue is stage-keyed, so one drain round can hold
        ``A#71`` and ``B#71``. Dedup is per-repo: both must dispatch; neither may
        be silently terminalized as a duplicate of the other.
        """
        coordinator, _pool, _ = make_coordinator(
            tmp_path, monkeypatch, repos=["repo-a", "repo-b"], max_workers=2
        )
        ran: list[str] = []

        class RecordingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                ran.append(f"{item.repo}#{item.issue}")
                return StageOutcome(Disposition.SKIP, "recorded")

        coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
        a71 = _issue_item(71, StageName.IMPLEMENTATION, repo="repo-a")
        b71 = _issue_item(71, StageName.IMPLEMENTATION, repo="repo-b")
        coordinator._push_item(a71, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(b71, StageName.IMPLEMENTATION, enter=True)

        coordinator._drain_implementation()

        # BOTH ran — the cross-repo pair is not collapsed.
        assert sorted(ran) == ["repo-a#71", "repo-b#71"]
        # Neither was terminalized as a superseded duplicate.
        assert a71.result is None or "superseded" not in (a71.result.reason or "")
        assert b71.result is None or "superseded" not in (b71.result.reason or "")
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []

    def test_three_duplicates_collapse_to_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3+ copies of one issue: first dispatches, ALL later copies terminalize (#2057)."""
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        ran: list[int] = []

        class RecordingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                ran.append(item.issue or 0)
                return StageOutcome(Disposition.SKIP, "recorded")

        coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
        first = _issue_item(21, StageName.IMPLEMENTATION)
        dup2 = _issue_item(21, StageName.IMPLEMENTATION)
        dup3 = _issue_item(21, StageName.IMPLEMENTATION)
        # Inject the collisions BELOW the upstream idempotency guard (#2107):
        # push straight onto the queue (the same raw API the drain uses to
        # re-push deferred items, coordinator.py:873) so this test exercises
        # the drain-level safety net for duplicates that arrived by a path the
        # upstream guard does not cover. (The guard would otherwise drop dup2/
        # dup3 at push time, so they'd never reach the drain-level collapse.)
        for it in (first, dup2, dup3):
            it.payload["_enter_pending"] = True
            coordinator._seen_item_ids.add(id(it))
            coordinator.items.append(it)
            coordinator.queues[StageName.IMPLEMENTATION].push(it)

        coordinator._drain_implementation()

        assert ran == [21]  # dispatched exactly once
        for dup in (dup2, dup3):  # every later copy terminalized
            assert dup.stage is StageName.FINISHED
            assert dup.result is not None
            assert dup.result.passed
            assert "superseded" in dup.result.reason
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []

    def test_issue_none_items_never_deduped_or_terminalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue=None items bypass issue-number dedup — never collapsed, never terminalized (#2057).

        The number-keyed topo/overlap dispatch path only handles issue items, so
        issue=None items are left queued (re-pushed) rather than dispatched here.
        The invariant this guards: two issue=None items must NOT be treated as
        duplicates of each other (``None == None``) and terminalized.
        """
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        pr_a = WorkItem(
            repo="repo-a", kind=ItemKind.PR, pr=500, stage=StageName.IMPLEMENTATION, state="ENTER"
        )
        pr_b = WorkItem(
            repo="repo-a", kind=ItemKind.PR, pr=501, stage=StageName.IMPLEMENTATION, state="ENTER"
        )
        coordinator._push_item(pr_a, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(pr_b, StageName.IMPLEMENTATION, enter=True)

        coordinator._drain_implementation()

        # Neither was terminalized as a duplicate of the other.
        assert pr_a.result is None
        assert pr_b.result is None
        # Both remain queued for their proper stage handling (order preserved).
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [pr_a, pr_b]

    def test_reseed_while_queued_refuses_second_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A new (repo, issue) already queued is refused upstream (#2107).

        The drain-level dedup warning must NOT fire, proving the duplicate never
        entered the queue in the first place.
        """
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        first = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")
        reseed = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")  # new object, same key
        coordinator._push_item(first, StageName.IMPLEMENTATION, enter=True)

        with caplog.at_level(logging.INFO):
            coordinator._push_item(reseed, StageName.IMPLEMENTATION, enter=True)

        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [first]
        assert "already queued/in-flight" in caplog.text
        # The drain-level dedup net was never exercised.
        assert "dropping duplicate work item" not in caplog.text

    def test_cross_repo_same_issue_number_both_enqueue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(#2058) same issue number in different repos remain distinct work."""
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        a = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")
        b = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-b")
        coordinator._push_item(a, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(b, StageName.IMPLEMENTATION, enter=True)
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == [a, b]

    def test_same_item_repush_not_blocked_by_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-tracked item re-pushing itself (retry/advance) is allowed."""
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        item = _issue_item(21, StageName.PLANNING, repo="repo-a")
        coordinator._push_item(item, StageName.PLANNING, enter=True)
        coordinator.queues[StageName.PLANNING].pop()  # simulate drain popping it
        coordinator._push_item(item, StageName.PLANNING, enter=False)  # retry re-push
        assert coordinator.queues[StageName.PLANNING].snapshot() == [item]

    def test_reseed_while_in_flight_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A (repo, issue) held in in_flight blocks a new seed of the same key."""
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        live = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")
        coordinator.in_flight[cast(Any, "h1")] = live  # simulate dispatched/in-flight
        reseed = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")
        coordinator._push_item(reseed, StageName.IMPLEMENTATION, enter=True)
        assert coordinator.queues[StageName.IMPLEMENTATION].snapshot() == []

    def test_topo_order_and_overlap_reuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """order_for_implementation and _select_non_overlapping gate dispatch."""
        coordinator, _pool, _ = make_coordinator(tmp_path, monkeypatch, max_workers=2)
        run_order: list[int] = []

        class RecordingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                run_order.append(item.issue or 0)
                return StageOutcome(Disposition.SKIP, "recorded")

        coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
        # 21 depends on 22 (payload dependency): topo order runs 22 first.
        # Distinct repos: the implementation queue is keyed by stage, not repo,
        # so the coordinator must resolve each issue's repo from its own item (#1795).
        item_a = _issue_item(21, StageName.IMPLEMENTATION, repo="repo-a")
        item_a.payload["dependencies"] = [22]
        item_b = _issue_item(22, StageName.IMPLEMENTATION, repo="repo-b")
        coordinator._push_item(item_a, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(item_b, StageName.IMPLEMENTATION, enter=True)
        seen_repo_of: dict[int, tuple[str, str]] = {}

        def _fake_select(
            issues: list[int], repo_of: dict[int, tuple[str, str]] | None = None
        ) -> tuple[list[int], list[int]]:
            seen_repo_of.update(repo_of or {})
            return issues[:1], issues[1:]  # defer everything but the first

        monkeypatch.setattr(
            "hephaestus.automation.pipeline.admission._select_non_overlapping",
            _fake_select,
        )

        coordinator._drain_implementation()

        assert run_order == [22]  # dependency first; 21 deferred by overlap
        assert len(coordinator.queues[StageName.IMPLEMENTATION]) == 1
        # Each issue is scoped to the repo of ITS OWN WorkItem, not the ambient CWD.
        assert seen_repo_of == {21: ("org", "repo-a"), 22: ("org", "repo-b")}

    def test_file_overlap_serialization_can_be_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-serialize-file-overlap lets all ready implementation items dispatch."""
        coordinator, _pool, _ = make_coordinator(
            tmp_path, monkeypatch, max_workers=2, serialize_file_overlap=False
        )
        run_order: list[int] = []

        class RecordingStage(StubStage):
            def step(self, item: WorkItem, ctx: Any) -> Any:
                run_order.append(item.issue or 0)
                return StageOutcome(Disposition.SKIP, "recorded")

        coordinator.stages[StageName.IMPLEMENTATION] = RecordingStage()
        item_a = _issue_item(21, StageName.IMPLEMENTATION)
        item_b = _issue_item(22, StageName.IMPLEMENTATION)
        coordinator._push_item(item_a, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(item_b, StageName.IMPLEMENTATION, enter=True)
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.admission._select_non_overlapping",
            lambda issues: (_ for _ in ()).throw(AssertionError("should not serialize overlap")),
        )

        coordinator._drain_implementation()

        assert run_order == [21, 22]
        assert len(coordinator.queues[StageName.IMPLEMENTATION]) == 0


class TestDurableEventLog:
    """Optional JSONL event log mirrors the coordinator's in-memory event log."""

    def test_observability_tick_zeroes_previous_circuit_breaker_state(self, tmp_path: Path) -> None:
        """A transition leaves exactly one active state gauge for each breaker."""
        snapshots: dict[str, dict[str, Any]] = {"github": {"state": "closed"}}
        coordinator = Coordinator(
            PipelineConfig(
                org="org",
                repos=["repo-a"],
                projects_dir=tmp_path,
                metrics_port=9123,
                circuit_breaker_snapshot_provider=lambda: snapshots,
            ),
            github=FakeStageGitHub(),
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        coordinator._emit_observability_tick()
        snapshots["github"]["state"] = "open"
        coordinator._emit_observability_tick()

        assert coordinator._metrics_registry is not None
        rendered = coordinator._metrics_registry.render_prometheus()
        assert 'hephaestus_circuit_breaker_state{name="github",state="closed"} 0' in rendered
        assert 'hephaestus_circuit_breaker_state{name="github",state="open"} 1' in rendered

    def test_observability_tick_zeroes_removed_inflight_repo(self, tmp_path: Path) -> None:
        """A repo that leaves the in-flight counter no longer reports active work."""
        coordinator = Coordinator(
            PipelineConfig(
                org="org",
                repos=["repo-a"],
                projects_dir=tmp_path,
                metrics_port=9123,
            ),
            github=FakeStageGitHub(),
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator.inflight_per_repo["repo-a"] = 1

        coordinator._emit_observability_tick()
        del coordinator.inflight_per_repo["repo-a"]
        coordinator._emit_observability_tick()

        assert coordinator._metrics_registry is not None
        assert 'hephaestus_pipeline_inflight_per_repo{repo="repo-a"} 0' in (
            coordinator._metrics_registry.render_prometheus()
        )

    def test_observability_tick_snapshots_lifecycle_and_deduplicates_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A metrics-enabled coordinator durably records only alert transitions."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            metrics_port=9123,
            alert_queue_depth_threshold=0,
            circuit_breaker_snapshot_provider=all_circuit_breaker_snapshots,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        coordinator.queues[StageName.PLANNING].push(_issue_item(44, StageName.PLANNING))
        breaker = get_circuit_breaker("github-observed", failure_threshold=1)
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("down")))

        coordinator._emit_observability_tick()
        coordinator._emit_observability_tick()
        coordinator.queues[StageName.PLANNING].pop()
        reset_all_circuit_breakers()
        coordinator._emit_observability_tick()

        events = [entry for entry in coordinator.event_log if entry[0].startswith("alert_")]
        assert [(event[0], event[1]["name"]) for event in events] == [
            ("alert_fired", "circuit_breaker_open"),
            ("alert_fired", "queue_depth_exceeds"),
            ("alert_resolved", "circuit_breaker_open"),
            ("alert_resolved", "queue_depth_exceeds"),
        ]
        snapshots = [entry[1] for entry in coordinator.event_log if entry[0] == "metrics_snapshot"]
        assert snapshots[0]["queue_depths"]["planning"] == 1
        # The registry reflects the latest resolved lifecycle state, not a stale
        # historical high-water mark.
        assert coordinator._metrics_registry is not None
        assert 'hephaestus_pipeline_queue_depth{stage="planning"} 0' in (
            coordinator._metrics_registry.render_prometheus()
        )

    def test_event_log_path_persists_queue_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configured event_log_path receives JSONL queue/event records."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        item = _issue_item(44, StageName.PLANNING)

        coordinator._push_item(item, StageName.PLANNING, enter=True)

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        assert records[-1]["event"] == "push"
        assert records[-1]["fields"] == ["planning", "repo-a#44"]
        assert coordinator.event_log[-1] == ("push", "planning", "repo-a#44")

    def test_zero_thread_nogo_event_is_durable_and_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stage events persist the fixed zero-thread audit fields."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        coordinator._ctx_for_repo("repo-a").emit_event(
            PrReviewZeroThreadNogoEvent(
                repo="repo-a",
                issue=1985,
                pr=1984,
                completed_rounds=0,
                retry_attempt=1,
                retry_cap=2,
                action=ZeroThreadNogoAction.RETRY_FRESH_REVIEW,
                artifact_written=True,
            )
        )

        record = json.loads(event_log_path.read_text().splitlines()[-1])
        assert record["event"] == "pr_review_zero_thread_nogo"
        assert record["fields"] == [
            {
                "action": "retry_fresh_review",
                "artifact_written": True,
                "completed_rounds": 0,
                "issue": 1985,
                "posted_threads": 0,
                "pr": 1984,
                "repo": "repo-a",
                "retry_attempt": 1,
                "retry_cap": 2,
                "round_consumed": False,
                "unresolved_threads": 0,
            }
        ]
        assert "summary" not in record["fields"][0]

    def test_pr_review_stage_event_uses_coordinator_encoder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real stage callback reaches the coordinator's closed JSONL schema."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(by_severity=[(0, 0, 0)]),
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        item = WorkItem(
            repo="repo-a",
            kind=ItemKind.PR,
            issue=1985,
            pr=1984,
            stage=StageName.PR_REVIEW,
            state="EVAL",
        )
        item.payload["review_verdict"] = ReviewVerdict(
            grade="B",
            verdict="NOGO",
            raw='```json\n{"summary":"No actionable location."}\n```',
        )
        stage = PrReviewStage()
        ctx = coordinator._ctx_for_repo("repo-a")

        first = stage.step(item, ctx)
        assert isinstance(first, Continue)
        assert first.next_state == "REVIEW_WAIT"

        item.payload["review_error_retries"] = REVIEW_ERROR_RETRY_CAP
        second = stage.step(item, ctx)
        assert second == StageOutcome(Disposition.SKIP, "zero_thread_nogo_exhausted")

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        assert [record["event"] for record in records] == [
            "pr_review_zero_thread_nogo",
            "pr_review_zero_thread_nogo",
        ]
        assert records[0]["fields"] == [
            {
                "action": "retry_fresh_review",
                "artifact_written": True,
                "completed_rounds": 0,
                "issue": 1985,
                "posted_threads": 0,
                "pr": 1984,
                "repo": "repo-a",
                "retry_attempt": 1,
                "retry_cap": 2,
                "round_consumed": False,
                "unresolved_threads": 0,
            }
        ]
        assert records[1]["fields"] == [
            {
                "action": "escalate_skip",
                "artifact_written": True,
                "completed_rounds": 0,
                "issue": 1985,
                "posted_threads": 0,
                "pr": 1984,
                "repo": "repo-a",
                "retry_attempt": 3,
                "retry_cap": 2,
                "round_consumed": False,
                "unresolved_threads": 0,
            }
        ]

    def test_stage_event_rejects_foreign_raw_content_before_jsonl_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Foreign event objects cannot inject reviewer text into the log."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        @dataclass(frozen=True)
        class UnsafeEvent:
            reviewer_summary: str

        with pytest.raises(TypeError, match="unsupported stage event"):
            coordinator._ctx_for_repo("repo-a").emit_event(
                cast(Any, UnsafeEvent("untrusted reviewer data"))
            )
        assert not event_log_path.exists()

    def test_event_log_path_persists_job_completion_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Job completions are durable without logging raw agent output."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        stage = StubStage()
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=FakeWorkerPool(),
            stages={StageName.PLANNING: stage},
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
        item = _issue_item(44, StageName.PLANNING)

        coordinator._submit(item, JobRequest(_agent_job(issue=44), "REVIEWED"))
        coordinator._drain_completions()

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        complete = next(record for record in records if record["event"] == "complete")
        assert complete["fields"] == [
            "AgentJob",
            "repo-a#44",
            "planning",
            "REVIEWED",
            {
                "descr": "stub agent job",
                "duration_s": 0.0,
                "error": None,
                "interrupted": False,
                "ok": True,
            },
        ]
        assert "stdout_tail" not in complete["fields"][-1]
        assert "stderr_tail" not in complete["fields"][-1]

    def test_event_log_completion_records_worker_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Completion records include the worker that executed the submitted job."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        pool = FakeWorkerPool()
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=pool,
            stages={StageName.PLANNING: StubStage()},
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
        result = JobResult(ok=True, value="done")
        object.__setattr__(result, "worker_id", "hephaestus-pipeline-worker_0")
        pool.queue_result(result)
        item = _issue_item(45, StageName.PLANNING)

        coordinator._submit(item, JobRequest(_agent_job(issue=45), "REVIEWED"))
        coordinator._drain_completions()

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        complete = next(record for record in records if record["event"] == "complete")
        assert complete["fields"][-1]["worker_id"] == "hephaestus-pipeline-worker_0"

    def test_submit_forwards_claim_context_to_worker_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Submitted jobs carry item/stage context for worker-claim logging."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        pool = FakeWorkerPool()
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=pool,
            stages={StageName.PLANNING: StubStage()},
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
        item = _issue_item(46, StageName.PLANNING)

        coordinator._submit(item, JobRequest(_agent_job(issue=46), "REVIEWED"))

        assert pool.submitted_claims == [("repo-a#46", "planning")]

    def test_event_log_path_sanitizes_job_error_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Job completions persist safe error classes, not raw error text."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        stage = StubStage()
        pool = FakeWorkerPool()
        pool.queue_result(JobResult(ok=False, error="token=secret private-endpoint"))
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=pool,
            stages={StageName.PLANNING: stage},
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
        item = _issue_item(44, StageName.PLANNING)

        coordinator._submit(item, JobRequest(_agent_job(issue=44), "REVIEWED"))
        coordinator._drain_completions()

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        text = event_log_path.read_text()
        complete = next(record for record in records if record["event"] == "complete")
        assert "token=secret" not in text
        assert "private-endpoint" not in text
        assert complete["fields"][-1]["error"] == "error"

    def test_event_log_path_persists_resumable_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interrupted items leave durable resumable breadcrumbs."""
        event_log_path = tmp_path / "pipeline-events.jsonl"
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            loops=1,
            projects_dir=tmp_path,
            event_log_path=event_log_path,
        )
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        item = _issue_item(44, StageName.PR_REVIEW)
        item.state = "REVIEW_WAIT"

        coordinator._park_resumable(item)

        records = [json.loads(line) for line in event_log_path.read_text().splitlines()]
        assert records[-1]["event"] == "resumable"
        assert records[-1]["fields"] == ["repo-a#44", "pr_review", "REVIEW_WAIT"]

    def test_park_resumable_does_not_keep_private_buffer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RESUMABLE parking should not accumulate hidden coordinator state."""
        monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: [])
        coordinator, _, _ = make_coordinator(tmp_path, monkeypatch)
        item = _issue_item(44, StageName.PR_REVIEW)
        item.state = "REVIEW_WAIT"

        coordinator._park_resumable(item)

        assert "_resumable" not in coordinator.__dict__


class TestPipelineScopeWiring:
    """Scope-trimmed routing + the planner CLI's --force re-plan override (#1820)."""

    @pytest.fixture(autouse=True)
    def _stub_open_issue_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep scope-wiring tests unit-local; filter behavior has its own test."""
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
            lambda _repo, issues: list(issues),
        )

    def _scoped_config(
        self, tmp_path: Path, *, issues: list[int], force: bool = False
    ) -> PipelineConfig:
        from hephaestus.automation.pipeline.routing import PipelineScope

        return PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=issues,
            loops=1,
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})),
            force=force,
        )

    def test_scoped_routes_include_finished_sink(self, tmp_path: Path) -> None:
        """A scoped run's route table always carries the FINISHED sink row."""
        config = self._scoped_config(tmp_path, issues=[1])
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        # Only the two in-scope stages plus the always-present FINISHED sink.
        assert set(coordinator._routes) == {
            StageName.PLANNING,
            StageName.PLAN_REVIEW,
            StageName.FINISHED,
        }
        # PLANNING.next (PLAN_REVIEW) stays in scope; PLAN_REVIEW.next
        # (IMPLEMENTATION) is out of scope -> rewritten to FINISHED.
        assert coordinator._routes[StageName.PLANNING].next == StageName.PLAN_REVIEW
        assert coordinator._routes[StageName.PLAN_REVIEW].next == StageName.FINISHED

    def test_full_run_uses_global_routes(self, tmp_path: Path) -> None:
        """Without a scope the coordinator routes through the full ROUTES table."""
        from hephaestus.automation.pipeline.routing import ROUTES

        config = PipelineConfig(org="org", repos=["repo-a"], loops=1, projects_dir=tmp_path)
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        assert coordinator._routes is ROUTES

    def test_direct_issue_scope_hydrates_issue_context_payload(self, tmp_path: Path) -> None:
        """Explicit --issues seeding preserves the real issue title/body for prompts."""
        gh = FakeStageGitHub(
            labels=["state:needs-plan"],
            issue_title="Hydrate planner context",
            issue_body="Use the real issue body.",
        )
        config = self._scoped_config(tmp_path, issues=[1881])
        coordinator = Coordinator(config, github=gh, pool=FakeWorkerPool(), install_signals=False)

        entries = coordinator._seed_direct_scope("repo-a")
        item = coordinator._entry_to_item(entries[0], "repo-a")

        assert entries[0].issue_title == "Hydrate planner context"
        assert entries[0].issue_body == "Use the real issue body."
        assert item.payload["issue_title"] == "Hydrate planner context"
        assert item.payload["issue_body"] == "Use the real issue body."

    def test_direct_merged_pr_with_pending_arm_record_reenters_merge_wait(
        self, tmp_path: Path
    ) -> None:
        """A restarted drive must not drop the post-merge learn handoff (#2055)."""

        class _RecoveryGitHub(FakeStageGitHub):
            def pending_drive_green_arms(self) -> list[tuple[int, int]]:
                return [(2055, 601)]

        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            prs=[601],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        )
        github = _RecoveryGitHub(
            pr_issue=2055,
            pr_state={"state": "MERGED", "headRefOid": "a" * 40},
        )
        coordinator = Coordinator(
            config, github=github, pool=FakeWorkerPool(), install_signals=False
        )

        entries = coordinator._seed_direct_scope("repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.MERGE_WAIT
        assert entries[0].issue_number == 2055
        assert entries[0].merge_wait_recovery is True

    def test_direct_open_pr_with_pending_arm_record_bypasses_strict_review(
        self, tmp_path: Path
    ) -> None:
        """An explicit PR restart preserves its durable arm handoff too."""

        class _RecoveryGitHub(FakeStageGitHub):
            def pending_drive_green_arms(self) -> list[tuple[int, int]]:
                return [(2055, 601)]

        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            prs=[601],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        )
        coordinator = Coordinator(
            config,
            github=_RecoveryGitHub(
                pr_issue=2055,
                pr_state={"state": "OPEN", "headRefOid": "a" * 40},
            ),
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        entries = coordinator._seed_direct_scope("repo-a")
        item = coordinator._entry_to_item(entries[0], "repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.MERGE_WAIT
        assert entries[0].merge_wait_recovery is True
        assert item.payload["merge_wait_recovery"] is True

    def test_repo_recovery_seed_marks_merge_wait_reconstruction(self, tmp_path: Path) -> None:
        """The no-CLI-scope recovery scan carries the same stage handoff."""

        class _RecoveryGitHub(FakeStageGitHub):
            def pending_drive_green_arms(self) -> list[tuple[int, int]]:
                return [(2055, 601)]

        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        )
        coordinator = Coordinator(
            config,
            github=_RecoveryGitHub(),
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        entries = coordinator._pending_arm_recovery_entries()
        item = coordinator._entry_to_item(entries[0], "repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.MERGE_WAIT
        assert entries[0].merge_wait_recovery is True
        assert item.payload["merge_wait_recovery"] is True

    def test_direct_merged_issue_with_pending_arm_record_reenters_merge_wait(
        self, tmp_path: Path
    ) -> None:
        """Issue-scoped restart preserves the same post-merge learn handoff."""

        class _RecoveryGitHub(FakeStageGitHub):
            def pending_drive_green_arms(self) -> list[tuple[int, int]]:
                return [(2055, 601)]

        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[2055],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        )
        github = _RecoveryGitHub(merged_pr=601)
        coordinator = Coordinator(
            config, github=github, pool=FakeWorkerPool(), install_signals=False
        )

        entries = coordinator._seed_direct_scope("repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.MERGE_WAIT
        assert entries[0].pr_number == 601
        assert entries[0].merge_wait_recovery is True

    def test_issue_scoped_pending_arm_recovers_before_closed_issue_filter(
        self, tmp_path: Path
    ) -> None:
        """A merged PR's closed issue still restores its unconsumed learn record."""

        class _RecoveryGitHub(FakeStageGitHub):
            def pending_drive_green_arms(self) -> list[tuple[int, int]]:
                return [(2055, 601)]

        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            issues=[2055],
            projects_dir=tmp_path,
            scope=PipelineScope(frozenset({StageName.MERGE_WAIT})),
        )
        coordinator = Coordinator(
            config,
            github=_RecoveryGitHub(merged_pr=601),
            pool=FakeWorkerPool(),
            install_signals=False,
        )

        assert coordinator._seed_pass() == 1
        assert coordinator.items[0].stage is StageName.MERGE_WAIT
        assert coordinator.items[0].issue == 2055
        assert coordinator.items[0].pr == 601
        assert coordinator.items[0].payload["merge_wait_recovery"] is True

    def test_seed_pass_filters_closed_explicit_issues_before_classification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Closed explicit --issues are dropped before pipeline classification (#1899)."""

        class RecordingGitHub(FakeStageGitHub):
            def __init__(self) -> None:
                super().__init__(labels=["state:needs-plan"])
                self.issue_json_calls: list[int] = []

            def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
                self.issue_json_calls.append(issue_number)
                return super().gh_issue_json(issue_number)

        def fake_filter(repo: str, issue_numbers: list[int]) -> list[int]:
            assert repo == "repo-a"
            assert issue_numbers == [1, 2, 3]
            return [1, 3]

        gh = RecordingGitHub()
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
            fake_filter,
        )
        config = self._scoped_config(tmp_path, issues=[1, 2, 3])
        coordinator = Coordinator(config, github=gh, pool=FakeWorkerPool(), install_signals=False)

        assert coordinator._seed_pass() == 2
        assert [item.issue for item in coordinator.items] == [1, 3]
        assert gh.issue_json_calls == [1, 3]

    def test_at_or_past_plan_go_issue_clamps_to_finished(self, tmp_path: Path) -> None:
        """A plan-go issue classifies to IMPLEMENTATION; the scope clamps it to FINISHED-pass."""
        gh = FakeStageGitHub(labels=["state:plan-go"])
        config = self._scoped_config(tmp_path, issues=[1])
        coordinator = Coordinator(config, github=gh, pool=FakeWorkerPool(), install_signals=False)

        assert coordinator.run() == 0
        assert len(coordinator.ledger) == 1
        result = coordinator.ledger[0]
        assert result.passed
        assert result.final_stage is StageName.FINISHED
        # The item never entered an out-of-scope IMPLEMENTATION stage.
        assert all(item.stage is StageName.FINISHED for item in coordinator.items)

    def test_force_reroutes_plan_go_issue_to_planning(self, tmp_path: Path) -> None:
        """--force re-routes an at-or-past-plan-go issue back to the scope's first stage."""
        gh = FakeStageGitHub(labels=["state:plan-go"])
        config = self._scoped_config(tmp_path, issues=[1], force=True)
        coordinator = Coordinator(config, github=gh, pool=FakeWorkerPool(), install_signals=False)

        entries = coordinator._seed_direct_scope("repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.PLANNING
        assert "force re-plan" in entries[0].reason

    def test_force_leaves_pre_scope_stage_untouched(self, tmp_path: Path) -> None:
        """--force must NOT pull a PRE-scope stage forward into the scope.

        Regression (#1820 review): under a later scope (e.g.
        implementation->pr_review) a PLANNING classification is upstream of the
        scope. force is a redo knob for work at-or-past the scope, not a
        fast-forward that advances un-started upstream work into it.
        """
        from hephaestus.automation.pipeline.routing import PipelineScope

        # Scope starts at IMPLEMENTATION; PLANNING is pre-scope.
        scope = PipelineScope(frozenset({StageName.IMPLEMENTATION, StageName.PR_REVIEW}))
        config = self._scoped_config(tmp_path, issues=[1], force=True)
        object.__setattr__(config, "scope", scope)
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        stage, reason = coordinator._clamp_seed_stage_to_scope(
            1, StageName.PLANNING, "needs-plan", scope.stages
        )

        assert stage is StageName.PLANNING  # left untouched, NOT forced to IMPLEMENTATION
        assert "force re-plan" not in reason

    def test_needs_plan_issue_seeds_into_planning_within_scope(self, tmp_path: Path) -> None:
        """An in-scope PLANNING classification is preserved (no clamp)."""
        gh = FakeStageGitHub(labels=["state:needs-plan"])
        config = self._scoped_config(tmp_path, issues=[1])
        coordinator = Coordinator(config, github=gh, pool=FakeWorkerPool(), install_signals=False)

        entries = coordinator._seed_direct_scope("repo-a")

        assert len(entries) == 1
        assert entries[0].stage is StageName.PLANNING


class TestConfigWiring:
    """PipelineConfig fields reach the per-repo StageContext the stages read."""

    def test_budget_override_takes_precedence_over_routes_default(self, tmp_path: Path) -> None:
        """budget_overrides={"ci_fix": N} overrides the ROUTES ci_fix default (1)."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            budget_overrides={"ci_fix": 3},
        )
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.budget("ci_fix") == 3
        # A non-overridden key still resolves from ROUTES (rebase default 2).
        assert ctx.budget("rebase") == 2

    def test_drive_green_filters_flow_to_stage_config(self, tmp_path: Path) -> None:
        """Discovery flags survive the coordinator's stage-config copy."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            include_bot_prs=False,
            include_all_authors=True,
        )
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )

        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.config.include_bot_prs is False
        assert ctx.config.include_all_authors is True

    def test_no_budget_override_uses_routes_default(self, tmp_path: Path) -> None:
        """Without an override the ci_fix budget is the ROUTES default (1)."""
        config = PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path)
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.budget("ci_fix") == 1

    def test_enable_mechanical_rebase_flows_to_stage_config(self, tmp_path: Path) -> None:
        """enable_mechanical_rebase=False reaches ctx.config (read by stages/ci.py)."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            enable_mechanical_rebase=False,
        )
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.config.enable_mechanical_rebase is False

    def test_poll_max_wait_flows_to_stage_config(self, tmp_path: Path) -> None:
        """poll_max_wait reaches ctx.config for wall-clock CI polling."""
        config = PipelineConfig(
            org="org",
            repos=["repo-a"],
            projects_dir=tmp_path,
            poll_max_wait=42,
        )
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.config.poll_max_wait == 42

    def test_enable_mechanical_rebase_defaults_true(self, tmp_path: Path) -> None:
        """The default keeps the CI stage's mechanical rebase enabled."""
        config = PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path)
        coordinator = Coordinator(
            config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
        )
        ctx = coordinator._ctx_for_repo("repo-a")

        assert ctx.config.enable_mechanical_rebase is True
