"""Coordinator event-loop tests (epic #1809, #1817).

Covers: quiescence with FakeWorkerPool/FakeStageGitHub, the journal-order
invariant (durable mutation precedes the queue push in one shared trace),
downstream-first drain order, per-repo in-flight cap, zero-work convergence,
the loop budget, the non-blocking rate-budget park, dry-run
asserts-no-submit, poisoned-item isolation, and FAIL_BACK routing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import seeding as seeding_mod
from hephaestus.automation.pipeline.coordinator import (
    _FAIL_BACK_CAP,
    Coordinator,
    PipelineConfig,
)
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.routing import Disposition, StageName, StageOutcome
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages.base import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
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
        item_a = _issue_item(21, StageName.IMPLEMENTATION)
        item_a.payload["dependencies"] = [22]
        item_b = _issue_item(22, StageName.IMPLEMENTATION)
        coordinator._push_item(item_a, StageName.IMPLEMENTATION, enter=True)
        coordinator._push_item(item_b, StageName.IMPLEMENTATION, enter=True)
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.admission._select_non_overlapping",
            lambda issues: (issues[:1], issues[1:]),  # defer everything but the first
        )

        coordinator._drain_implementation()

        assert run_order == [22]  # dependency first; 21 deferred by overlap
        assert len(coordinator.queues[StageName.IMPLEMENTATION]) == 1
