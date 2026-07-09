"""Coordinator edge-path tests: wiring, accounting, stalls, fatal errors (#1817).

Complements test_coordinator.py with the seams a happy-path run never hits:
default pool construction, unknown-handle completions, agent-time
accounting, on_enter routing, runaway-Continue guards, the phase-timeout ->
AgentJob.timeout_s mapping, the stall liveness guard, seeding's epic-tag
chokepoint, grace-window expiry, and run_pipeline's production wiring.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any, get_type_hints
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.pipeline as pipeline_pkg
import hephaestus.automation.pipeline.coordinator as coordinator_mod
import hephaestus.automation.pipeline.jobs as jobs_mod
import hephaestus.automation.pipeline.routing as routing_mod
import hephaestus.automation.pipeline.seeding as seeding_mod
import hephaestus.automation.pipeline.stages.base as stage_base_mod
import hephaestus.automation.pipeline.work_item as work_item_mod
from hephaestus.automation.state_labels import STATE_PLAN_GO
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

Coordinator = coordinator_mod.Coordinator
PipelineConfig = coordinator_mod.PipelineConfig
_budget_lookup = coordinator_mod._budget_lookup
run_pipeline = coordinator_mod.run_pipeline

AgentJob = jobs_mod.AgentJob
GitJob = jobs_mod.GitJob
JobResult = jobs_mod.JobResult

Disposition = routing_mod.Disposition
StageName = routing_mod.StageName
StageOutcome = routing_mod.StageOutcome

EPIC_NEEDS_SKIP_TAG = seeding_mod.EPIC_NEEDS_SKIP_TAG
SeedEntry = seeding_mod.SeedEntry

Continue = stage_base_mod.Continue
JobRequest = stage_base_mod.JobRequest

ItemKind = work_item_mod.ItemKind
WorkItem = work_item_mod.WorkItem


def _agent_job(descr: str = "stub") -> AgentJob:
    return AgentJob(
        repo="repo-a",
        issue=1,
        agent="claude",
        model="m",
        prompt_builder=lambda **kwargs: "p",
        cwd=Path("/tmp"),
        timeout_s=10,
        descr=descr,
    )


def _coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: list[SeedEntry] | None = None,
    **config_overrides: Any,
) -> Coordinator:
    config = PipelineConfig(org="org", repos=["repo-a"], projects_dir=tmp_path, **config_overrides)
    monkeypatch.setattr(seeding_mod, "seed_from_cli", lambda r, i, p: list(seed or []))
    coordinator = Coordinator(
        config, github=FakeStageGitHub(), pool=FakeWorkerPool(), install_signals=False
    )
    coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]
    return coordinator


def _item(issue: int = 1, stage: StageName = StageName.PLANNING) -> WorkItem:
    return WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, stage=stage, state="ENTER")


class TestWiring:
    """Constructor and run_pipeline production wiring."""

    def test_package_binds_public_coordinator_exports(self) -> None:
        """Public coordinator exports resolve lazily without eager package imports."""
        assert "PipelineConfig" not in vars(pipeline_pkg)
        assert "run_pipeline" not in vars(pipeline_pkg)
        assert pipeline_pkg.PipelineConfig is PipelineConfig
        assert pipeline_pkg.run_pipeline is run_pipeline

    def test_run_has_single_exit_code_assignment(self) -> None:
        """The run loop has one authoritative exit-code assignment in ``finally``."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(Coordinator.run)))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "exit_code" for target in node.targets
            )
        ]
        assert len(assignments) == 1

    def test_default_pool_constructed_with_product_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pool size = parallel_repos x max_workers (epic contract)."""
        created: dict[str, Any] = {}

        class SpyPool:
            def __init__(self, size: int, shutdown: Any, completion_q: Any) -> None:
                created["size"] = size
                created["shutdown"] = shutdown
                created["completion_q"] = completion_q

        monkeypatch.setattr("hephaestus.automation.pipeline.worker_pool.WorkerPool", SpyPool)
        config = PipelineConfig(
            org="org", repos=["r"], parallel_repos=3, max_workers=4, projects_dir=tmp_path
        )
        coordinator = Coordinator(config, github=FakeStageGitHub(), install_signals=False)

        assert created["size"] == 12
        assert created["shutdown"] is coordinator.shutdown
        assert created["completion_q"] is coordinator.completion_q

    def test_run_pipeline_wires_accessor_and_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accessor = MagicMock()
        monkeypatch.setattr(
            "hephaestus.automation.pipeline_github.PipelineGitHub",
            MagicMock(return_value=accessor),
        )
        monkeypatch.setattr(coordinator_mod.Coordinator, "run", lambda self: 7)
        config = PipelineConfig(org="org", repos=["r"], dry_run=True, projects_dir=tmp_path)

        assert run_pipeline(config) == 7

    def test_budget_lookup_known_and_default(self) -> None:
        assert _budget_lookup("clone") == 2
        assert _budget_lookup("nonexistent") == 1

    def test_step_with_watchdog_uses_stage_step_result_contract(self) -> None:
        """The watchdog wrapper should preserve the stage step result union."""
        assert hasattr(coordinator_mod, "StageStepResult")
        hints = get_type_hints(Coordinator._step_with_watchdog)
        assert hints["return"] is coordinator_mod.StageStepResult


class TestExitCode:
    """Exit-code precedence contract."""

    def test_exit_code_documents_interrupt_priority(self) -> None:
        """The interrupt branch should explain why it wins over prior failures."""
        source = inspect.getsource(Coordinator._exit_code)

        assert "Interrupt deliberately takes priority" in source
        assert "earlier work had already failed" in source

    @pytest.mark.parametrize(
        ("reason"),
        [
            "fail: stage crash",
            "skip: state:skip",
            "blocked: unresolved review",
        ],
    )
    def test_shutdown_takes_priority_over_recorded_nonpassing_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
    ) -> None:
        """Interrupts should still exit 130 when the ledger already has failures."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        coordinator.ledger.append(
            work_item_mod.ItemResult(
                passed=False,
                reason=reason,
                final_stage=StageName.FINISHED,
            )
        )
        coordinator.shutdown.set()

        assert coordinator._exit_code() == 130

    def test_later_pass_supersedes_earlier_failed_logical_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retry loop that later passes must not exit failed due to stale ledger rows."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        failed = _item(2009, StageName.FINISHED)
        failed.pr = 2010
        failed.result = work_item_mod.ItemResult(
            passed=False, reason="git_error", final_stage=StageName.FINISHED
        )
        passed = _item(2009, StageName.FINISHED)
        passed.pr = 2010
        passed.result = work_item_mod.ItemResult(
            passed=True, reason="merged", final_stage=StageName.FINISHED
        )
        coordinator.items = [failed, passed]
        coordinator.ledger.extend([failed.result, passed.result])

        assert coordinator._exit_code() == 0

    def test_preserved_worktrees_ignore_superseded_passed_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale preserved path from an earlier failed attempt should not be reported."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        preserved_path = tmp_path / "issue-2009"
        preserved_path.mkdir()
        failed = _item(2009, StageName.FINISHED)
        failed.pr = 2010
        failed.result = work_item_mod.ItemResult(
            passed=False, reason="git_error", final_stage=StageName.FINISHED
        )
        passed = _item(2009, StageName.FINISHED)
        passed.pr = 2010
        passed.result = work_item_mod.ItemResult(
            passed=True, reason="merged", final_stage=StageName.FINISHED
        )
        coordinator.items = [failed, passed]
        coordinator.preserved.append((2009, str(preserved_path)))

        assert coordinator._active_preserved_worktrees() == []

    def test_preserved_worktrees_ignore_missing_paths_for_failed_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing preserved path for the latest failed item should not be reported."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        missing_path = tmp_path / "issue-2009"
        failed = _item(2009, StageName.FINISHED)
        failed.pr = 2010
        failed.result = work_item_mod.ItemResult(
            passed=False, reason="git_error", final_stage=StageName.FINISHED
        )
        coordinator.items = [failed]
        coordinator.preserved.append((2009, str(missing_path)))

        assert coordinator._active_preserved_worktrees() == []


class TestCompletionEdges:
    """_handle_completion bookkeeping branches."""

    def test_unknown_handle_is_ignored_with_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch)
        pool = FakeWorkerPool()
        handle = pool.submit(_agent_job(), StageName.PLANNING)  # never registered

        with caplog.at_level("WARNING"):
            coordinator._handle_completion(handle, JobResult(ok=True))

        assert any("unknown handle" in record.message for record in caplog.records)

    def test_agent_job_time_accounting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent completions accrue count + duration for the summary."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        pool = coordinator.pool
        assert isinstance(pool, FakeWorkerPool)
        pool.queue_result(JobResult(ok=True, value="out", duration_s=2.5))
        item = _item()
        item.state = "PLAN_WAIT"
        coordinator._submit(item, JobRequest(_agent_job(), on_done_state="VERIFY"))

        class RecordStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.SKIP, "done")

            def on_job_done(self, i: WorkItem, result: JobResult, ctx: Any) -> None:
                i.payload["value"] = result.value

        coordinator.stages[StageName.PLANNING] = RecordStage()
        coordinator._drain_completions()

        assert coordinator._agent_job_count == 1
        assert coordinator._agent_job_time_s == pytest.approx(2.5)
        assert item.payload["value"] == "out"
        assert item.state == "VERIFY" or item.stage is StageName.FINISHED
        assert coordinator.inflight_per_repo["repo-a"] == 0

    def test_on_job_done_exception_poisons_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch)
        pool = coordinator.pool
        assert isinstance(pool, FakeWorkerPool)
        item = _item()
        coordinator._submit(item, JobRequest(_agent_job(), on_done_state="VERIFY"))

        class PoisonStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.SKIP, "unreached")

            def on_job_done(self, i: WorkItem, result: JobResult, ctx: Any) -> None:
                raise ValueError("bad parse")

        coordinator.stages[StageName.PLANNING] = PoisonStage()
        coordinator._drain_completions()

        assert item.result is not None
        assert "poisoned" in item.result.reason
        assert item.stage is StageName.FINISHED
        assert len(pool.submitted) == 1  # nothing new was submitted


class TestRunItemEdges:
    """on_enter routing, runaway Continue, dry-run descr fallback."""

    def test_on_enter_outcome_routes_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch)

        class FastForwardStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.ADVANCE, "already plan-go")

            def step(self, i: WorkItem, ctx: Any) -> Any:  # pragma: no cover
                raise AssertionError("step must not run")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PLANNING] = FastForwardStage()
        item = _item()
        coordinator._push_item(item, StageName.PLANNING, enter=True)

        coordinator._drain_queues()

        assert item.stage is StageName.PLAN_REVIEW

    def test_runaway_continue_is_poisoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stage that only ever Continues trips the per-tick step bound."""
        coordinator = _coordinator(tmp_path, monkeypatch)

        class SpinStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                return Continue(next_state="LOOP")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.PLANNING] = SpinStage()
        item = _item()

        coordinator._run_item(item)

        assert item.result is not None
        assert "exceeded" in item.result.reason

    def test_dry_run_descr_falls_back_to_job_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch, dry_run=True)
        job = GitJob(repo="repo-a", op="rebase", timeout_s=5)  # no descr

        class GitRequestingStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                return JobRequest(job, on_done_state="DONE")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.CI] = GitRequestingStage()

        with caplog.at_level("INFO"):
            coordinator._run_item(_item(stage=StageName.CI))

        assert any("[dry-run] would submit GitJob: GitJob" in r.message for r in caplog.records)


class TestSubmitEdges:
    """phase-timeout mapping and non-agent jobs bypassing the rate gate."""

    def test_phase_timeout_maps_onto_agent_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M4: --phase-timeout bounds each queue-pipeline AGENT JOB."""
        monkeypatch.setattr(
            "hephaestus.automation.pipeline_github.rate_budget_ok",
            lambda now_epoch=None: (True, 0.0),
        )
        coordinator = _coordinator(tmp_path, monkeypatch, phase_timeout_s=1234.0)
        pool = coordinator.pool
        assert isinstance(pool, FakeWorkerPool)

        coordinator._submit(_item(), JobRequest(_agent_job(), on_done_state="V"))

        assert pool.submitted[0].job.timeout_s == 1234

    def test_git_job_bypasses_rate_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only agent jobs are rate-gated; git jobs always submit."""
        monkeypatch.setattr(
            "hephaestus.automation.pipeline_github.rate_budget_ok",
            lambda now_epoch=None: (False, 60.0),
        )
        coordinator = _coordinator(tmp_path, monkeypatch)
        pool = coordinator.pool
        assert isinstance(pool, FakeWorkerPool)
        job = GitJob(repo="repo-a", op="clone", timeout_s=5, kwargs={"repo": "o/r", "dest": "d"})

        coordinator._submit(_item(), JobRequest(job, on_done_state="D"))

        assert len(pool.submitted) == 1
        assert coordinator.timers == []


class TestSeedingEdges:
    """CLI-scope seeding: epic chokepoint, --prs entries, finished entries."""

    def test_epic_needs_skip_tag_executes_chokepoint_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ONE sanctioned seeding write goes through skip_epics."""
        seed = [
            SeedEntry(
                kind="issue",
                identifier=44,
                stage=None,
                reason=f"{EPIC_NEEDS_SKIP_TAG}: #44 is an epic without state:skip",
            )
        ]
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed)
        gh = coordinator.github
        assert isinstance(gh, FakeStageGitHub)

        pushed = coordinator._seed_pass()

        assert pushed == 0
        assert ("skip_epics", ((44,),)) in gh.mutation_log
        assert "state:skip" in gh.labels[44]

    def test_plain_exclusion_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = [SeedEntry(kind="issue", identifier=45, stage=None, reason="state:skip")]
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed)
        gh = coordinator.github
        assert isinstance(gh, FakeStageGitHub)

        assert coordinator._seed_pass() == 0
        assert gh.mutation_log == []

    def test_pr_entry_becomes_pr_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = [SeedEntry(kind="pr", identifier=88, stage=StageName.CI, reason="impl-go")]
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed)

        coordinator._seed_pass()

        item = coordinator.queues[StageName.CI].snapshot()[0]
        assert item.kind is ItemKind.PR and item.pr == 88 and item.repo == "repo-a"

    def test_issue_entry_with_pr_preserves_pr_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --issues entries with open PRs must enter PR stages with item.pr set."""
        seed = [
            SeedEntry(
                kind="issue",
                identifier=1818,
                stage=StageName.PR_REVIEW,
                reason="open PR awaiting review",
                pr_number=1854,
            )
        ]
        coordinator = _coordinator(tmp_path, monkeypatch, seed=seed)

        coordinator._seed_pass()

        item = coordinator.queues[StageName.PR_REVIEW].snapshot()[0]
        assert item.kind is ItemKind.ISSUE
        assert item.issue == 1818
        assert item.pr == 1854

    def test_direct_issue_scope_uses_repo_scoped_github_accessor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --issues seeding must read the target repo, not ambient cwd state."""
        monkeypatch.setattr(
            seeding_mod,
            "seed_issue",
            MagicMock(side_effect=AssertionError("ambient issue seeding called")),
        )
        monkeypatch.setattr(
            "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
            lambda _repo, issues: list(issues),
        )
        target_github = FakeStageGitHub(
            labels=[STATE_PLAN_GO],
            open_pr=1854,
            pr_impl_state=(True, False),
        )
        created: list[tuple[str, Path]] = []

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            created.append((repo, repo_root))
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            issues=[1818],
            projects_dir=tmp_path,
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        coordinator._seed_pass()

        assert created == [("target-repo", tmp_path / "target-repo")]
        item = coordinator.queues[StageName.CI].snapshot()[0]
        assert item.repo == "target-repo"
        assert item.kind is ItemKind.ISSUE
        assert item.issue == 1818
        assert item.pr == 1854

    def test_direct_pr_scope_uses_repo_scoped_github_accessor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --prs seeding must read PR labels through the target repo accessor."""
        monkeypatch.setattr(
            seeding_mod,
            "gh_pr_label_names",
            MagicMock(side_effect=AssertionError("ambient PR label seeding called")),
        )
        target_github = FakeStageGitHub(pr_impl_state=(True, False), pr_issue=1818)
        created: list[tuple[str, Path]] = []

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            created.append((repo, repo_root))
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            prs=[1854],
            projects_dir=tmp_path,
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        coordinator._seed_pass()

        assert created == [("target-repo", tmp_path / "target-repo")]
        item = coordinator.queues[StageName.CI].snapshot()[0]
        assert item.repo == "target-repo"
        assert item.kind is ItemKind.PR
        assert item.issue == 1818
        assert item.pr == 1854

    def test_direct_pr_scope_hydrates_issue_for_pr_review_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --prs seeding preserves linked issue context for PR review."""
        monkeypatch.setattr(
            seeding_mod,
            "gh_pr_label_names",
            MagicMock(side_effect=AssertionError("ambient PR label seeding called")),
        )
        target_github = FakeStageGitHub(pr_impl_state=(False, False), pr_issue=1818)

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            prs=[1854],
            projects_dir=tmp_path,
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        coordinator._seed_pass()

        item = coordinator.queues[StageName.PR_REVIEW].snapshot()[0]
        assert item.repo == "target-repo"
        assert item.kind is ItemKind.PR
        assert item.issue == 1818
        assert item.pr == 1854

    def test_direct_pr_scope_finishes_already_merged_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --prs seeding must not adopt a deleted branch for a merged PR."""
        monkeypatch.setattr(
            seeding_mod,
            "gh_pr_label_names",
            MagicMock(side_effect=AssertionError("ambient PR label seeding called")),
        )

        class MergedPrGitHub(FakeStageGitHub):
            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("merged PRs should finish before label routing")

        target_github = MergedPrGitHub(
            pr_issue=1912,
            pr_state={"state": "MERGED", "headRefOid": "abc123"},
        )

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            prs=[2004],
            projects_dir=tmp_path,
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        assert coordinator.run() == 0

        assert not coordinator.queues[StageName.CI].snapshot()
        assert not coordinator.queues[StageName.PR_REVIEW].snapshot()
        assert len(coordinator.ledger) == 1
        assert coordinator.ledger[0].passed
        assert coordinator.ledger[0].final_stage is StageName.FINISHED
        assert "already merged" in coordinator.ledger[0].reason

    def test_direct_pr_scope_finishes_already_closed_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct --prs seeding must not adopt a deleted branch for a closed PR."""
        monkeypatch.setattr(
            seeding_mod,
            "gh_pr_label_names",
            MagicMock(side_effect=AssertionError("ambient PR label seeding called")),
        )

        class ClosedPrGitHub(FakeStageGitHub):
            def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
                raise AssertionError("closed PRs should finish before label routing")

        target_github = ClosedPrGitHub(
            pr_issue=1912,
            pr_state={"state": "CLOSED", "headRefOid": "abc123"},
        )

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            prs=[2004],
            projects_dir=tmp_path,
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        assert coordinator.run() == 1

        assert not coordinator.queues[StageName.CI].snapshot()
        assert not coordinator.queues[StageName.PR_REVIEW].snapshot()
        assert len(coordinator.ledger) == 1
        assert not coordinator.ledger[0].passed
        assert coordinator.ledger[0].final_stage is StageName.FINISHED
        assert "already closed" in coordinator.ledger[0].reason

    def test_direct_pr_scope_respects_drive_green_phase_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-implementation-go direct PR must not enter pr_review in CI scope."""
        monkeypatch.setattr(
            seeding_mod,
            "gh_pr_label_names",
            MagicMock(side_effect=AssertionError("ambient PR label seeding called")),
        )
        target_github = FakeStageGitHub(pr_impl_state=(False, False), pr_issue=1818)

        def github_factory(repo: str, repo_root: Path) -> FakeStageGitHub:
            return target_github

        config = PipelineConfig(
            org="org",
            repos=["target-repo"],
            prs=[1854],
            projects_dir=tmp_path,
            scope=routing_mod.PipelineScope(frozenset({StageName.CI, StageName.MERGE_WAIT})),
        )
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            github_factory=github_factory,
            pool=FakeWorkerPool(),
            install_signals=False,
        )
        coordinator._rate_budget_ok = lambda: (True, 0.0)  # type: ignore[method-assign]

        assert coordinator.run() == 1

        assert not coordinator.queues[StageName.PR_REVIEW].snapshot()
        assert len(coordinator.ledger) == 1
        assert not coordinator.ledger[0].passed
        assert "not ready for selected scope" in coordinator.ledger[0].reason

    def test_repo_product_finished_entry_gets_pass_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A merged-PR product enters finished with an idempotent pass result."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        repo_item = WorkItem(repo="repo-a", kind=ItemKind.REPO, stage=StageName.REPO)
        repo_item.payload["products"] = [
            {"kind": "issue", "number": 1, "stage": StageName.FINISHED, "reason": "PR merged"}
        ]

        coordinator._seed_products(repo_item)

        finished = coordinator.queues[StageName.FINISHED].snapshot()[0]
        assert finished.result is not None and finished.result.passed
        assert coordinator._pass_work_count == 0  # finished entries are not work


class TestLivenessAndFatal:
    """Stall guard, grace expiry, fatal seeding errors."""

    def test_stall_guard_force_runs_most_downstream_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch)
        ran: list[int] = []

        class SinkStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                ran.append(i.issue or 0)
                return StageOutcome(Disposition.SKIP, "forced")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.MERGE_WAIT] = SinkStage()
        coordinator._push_item(_item(1, StageName.MERGE_WAIT), StageName.MERGE_WAIT, enter=False)
        coordinator._progress = False
        coordinator._stalled_ticks = 2  # third stalled tick triggers the guard

        coordinator._idle_wait()

        assert ran == [1]

    def test_idle_wait_uses_named_poll_interval_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The coordinator's idle sleep should come from the module constant."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        delays: list[float] = []

        assert coordinator_mod._IDLE_POLL_S == 1.0
        monkeypatch.setattr(coordinator_mod, "_IDLE_POLL_S", 0.25)

        def capture_wait(timeout: float) -> None:
            delays.append(timeout)

        coordinator._wait_for_completion = capture_wait  # type: ignore[method-assign]

        coordinator._idle_wait()

        assert delays == [0.25]

    def test_idle_wait_uses_named_stall_threshold_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stall escape hatch should honor the module constant."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        ran: list[int] = []

        assert coordinator_mod._STALL_TICKS_BEFORE_FORCE == 3
        monkeypatch.setattr(coordinator_mod, "_STALL_TICKS_BEFORE_FORCE", 1)

        class SinkStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                ran.append(i.issue or 0)
                return StageOutcome(Disposition.SKIP, "forced")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        def unexpected_wait(timeout: float) -> None:
            pytest.fail(f"unexpected wait with timeout={timeout}")

        coordinator.stages[StageName.MERGE_WAIT] = SinkStage()
        coordinator._push_item(_item(1, StageName.MERGE_WAIT), StageName.MERGE_WAIT, enter=False)
        coordinator._progress = False
        coordinator._stalled_ticks = 0
        coordinator._wait_for_completion = unexpected_wait  # type: ignore[method-assign]

        coordinator._idle_wait()

        assert ran == [1]

    def test_force_run_one_asserts_no_in_flight_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stall escape hatch must only run after the event loop is truly idle."""
        coordinator = _coordinator(tmp_path, monkeypatch)
        item = _item(1, StageName.MERGE_WAIT)
        coordinator._push_item(item, StageName.MERGE_WAIT, enter=False)
        coordinator.in_flight[object()] = _item(2)  # type: ignore[index]

        with pytest.raises(AssertionError, match="force-run requires no in-flight work"):
            coordinator._force_run_one()

        assert coordinator.queues[StageName.MERGE_WAIT].snapshot() == [item]

    def test_force_run_one_logs_inflight_per_repo_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A forced run should expose leaked per-repo slots in its diagnostic log."""
        coordinator = _coordinator(tmp_path, monkeypatch)

        class SinkStage:
            def on_enter(self, i: WorkItem, ctx: Any) -> Any:
                return None

            def step(self, i: WorkItem, ctx: Any) -> Any:
                return StageOutcome(Disposition.SKIP, "forced")

            def on_job_done(self, i: WorkItem, result: Any, ctx: Any) -> None:
                pass

        coordinator.stages[StageName.MERGE_WAIT] = SinkStage()
        coordinator.inflight_per_repo["repo-a"] = 1
        coordinator._push_item(_item(1, StageName.MERGE_WAIT), StageName.MERGE_WAIT, enter=False)

        with caplog.at_level("ERROR"):
            coordinator._force_run_one()

        assert any("inflight_per_repo={'repo-a': 1}" in record.message for record in caplog.records)

    def test_idle_wait_resets_stall_counter_on_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coordinator = _coordinator(tmp_path, monkeypatch)
        coordinator._progress = True
        coordinator._stalled_ticks = 2
        coordinator._timer_park(_item(), 0.01)  # bounds the blocking wait

        coordinator._idle_wait()

        assert coordinator._stalled_ticks == 0

    def test_grace_window_expiry_forces_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A graceful shutdown that outlives its grace window tears down."""
        coordinator = _coordinator(tmp_path, monkeypatch, grace_s=0.0)
        item = _item()
        coordinator.in_flight[object()] = item  # type: ignore[index]
        coordinator.shutdown.set()
        coordinator._grace_deadline = 0.0  # already expired

        exit_code = coordinator.run()

        assert exit_code == 130
        assert item.result is not None
        assert item.result.reason.startswith("resumable")

    def test_fatal_seeding_error_exits_1_with_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A seeding crash fails the run (exit 1) but still prints the summary."""
        coordinator = _coordinator(tmp_path, monkeypatch)

        def boom(r: Any, i: Any, p: Any) -> Any:
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(seeding_mod, "seed_from_cli", boom)

        with caplog.at_level("INFO"):
            exit_code = coordinator.run()

        assert exit_code == 1
        assert "Pipeline summary" in caplog.text
