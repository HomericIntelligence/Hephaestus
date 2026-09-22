"""Route one admitted Fleet turn through the normal worker boundary."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation.current_plan import CurrentPlanRead
from hephaestus.automation.fleet_job_results import digest, owner_identity
from hephaestus.automation.pipeline import fleet_execution
from hephaestus.automation.pipeline.jobs import AgentJob, JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.review_journal import PlanDiscoveryResult
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.utils.file_lock import LockUnavailableError, file_lock

_WORKER = "hephaestus.automation.pipeline.worker_pool"


def _git(path: Path, *args: str) -> str:
    """Use only the isolated repository made for this test."""
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _source(
    tmp_path: Path, *, source_repository: str = "HomericIntelligence/Athena"
) -> tuple[SourceWorkspaceManager, WorkspaceBinding]:
    """Prepare an owned source receipt through its existing manager."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("initial source\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "initial source")
    manager = SourceWorkspaceManager(repository, repository=source_repository)
    binding = manager.prepare(
        265, SourceLane.IMPLEMENTATION, _git(repository, "rev-parse", "HEAD"), branch="265-admitted"
    )
    return manager, binding


def _job(
    binding: WorkspaceBinding, *, operation: AgentOperation = AgentOperation.IMPLEMENT
) -> AgentJob:
    """Keep the normal implementation request and its real source binding."""
    return AgentJob(
        repo="Athena",
        issue=265,
        agent="codex",
        model="",
        prompt_builder=lambda: "Implement the admitted issue.",
        cwd=binding.cwd,
        workspace=binding,
        timeout_s=30,
        allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
        execution_request=ExecutionRequest(
            AgentRole.IMPLEMENTER, operation, SessionLifecycle.START_NEW
        ),
        descr="implement" if operation is AgentOperation.IMPLEMENT else "test_fix",
    )


class _RecordingExecutor:
    """Record the handoff while checking the real source lease."""

    def __init__(self, manager: SourceWorkspaceManager, result: JobResult) -> None:
        self.manager = manager
        self.result = result
        self.calls: list[tuple[AgentJob, Path, float, threading.Event]] = []

    def execute(
        self,
        job: AgentJob,
        cwd: Path,
        *,
        deadline_s: float,
        shutdown: threading.Event,
    ) -> JobResult:
        """Require exclusion for the complete external execution handoff."""
        with (
            pytest.raises(LockUnavailableError),
            file_lock(
                self.manager._lane_lock_path(265, SourceLane.IMPLEMENTATION),
                require_exclusive=True,
                blocking=False,
            ),
        ):
            pytest.fail("the worker released the source lane before Fleet execution")
        self.calls.append((job, cwd, deadline_s, shutdown))
        return self.result


def test_worker_routes_admitted_job_without_local_provider(tmp_path: Path) -> None:
    """The supplied executor runs under the source lease with one deadline."""
    assert "fleet_executor" in inspect.signature(WorkerPool).parameters, (
        "WorkerPool must accept the opt-in Fleet executor through its normal constructor"
    )
    manager, binding = _source(tmp_path)
    expected = JobResult(ok=True, value="The admitted implementation completed.")
    executor = _RecordingExecutor(manager, expected)
    shutdown = threading.Event()
    deadline = time.monotonic() + 20
    pool = WorkerPool(
        size=1, shutdown=shutdown, completion_q=queue.Queue(), fleet_executor=executor
    )
    try:
        with (
            patch(f"{_WORKER}.resolve_agent") as resolve,
            patch(f"{_WORKER}.run_agent_session") as native,
            patch(f"{_WORKER}.resume_agent_session") as resume,
            patch.object(pool, "_run_codex_implementation") as isolated,
        ):
            job = _job(binding)
            result = pool._run_agent(job, deadline_s=deadline)
        assert result is expected
        assert executor.calls == [(job, binding.cwd, deadline, shutdown)]
        resolve.assert_not_called()
        native.assert_not_called()
        resume.assert_not_called()
        isolated.assert_not_called()
        with file_lock(
            manager._lane_lock_path(265, SourceLane.IMPLEMENTATION),
            require_exclusive=True,
            blocking=False,
        ):
            assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) is not None
    finally:
        pool.shutdown()


def test_worker_holds_later_model_job_without_fallback(tmp_path: Path) -> None:
    """A test-repair request cannot use the initial implementation admission."""
    assert "fleet_hold" in JobResult.__dataclass_fields__, (
        "JobResult must distinguish new Fleet admission from an ordinary retryable failure"
    )
    manager, binding = _source(tmp_path)
    executor = _RecordingExecutor(manager, JobResult(ok=True, value="must not execute"))
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), fleet_executor=executor
    )
    try:
        with (
            patch(f"{_WORKER}.resolve_agent") as resolve,
            patch(f"{_WORKER}.run_agent_session") as native,
            patch(f"{_WORKER}.resume_agent_session") as resume,
            patch.object(pool, "_run_codex_implementation") as isolated,
        ):
            result = pool._run_agent(_job(binding, operation=AgentOperation.TEST_FIX))
        assert result.ok is False
        assert result.fleet_hold == "new_admission_required"
        assert result.error == "fleet_new_admission_required"
        assert result.interrupted is False
        assert executor.calls == []
        resolve.assert_not_called()
        native.assert_not_called()
        resume.assert_not_called()
        isolated.assert_not_called()
    finally:
        pool.shutdown()


class _PrivateWorker:
    """Supply the existing private protocol without a provider or engine."""

    def __init__(
        self, session, manager, now, deadline, *, pending_only=False, lease_digest="c" * 64
    ) -> None:
        self.session = session
        self.manager = manager
        self.now = now
        self.deadline = deadline
        self.pending_only = pending_only
        self.lease_digest = lease_digest
        self.association = None
        self.reads = 0
        self.calls = []
        self.timeouts = []
        self.terminal_sent = False

    def exchange(self, message, timeout):
        self.timeouts.append(timeout)
        if not 0 < timeout <= 300:
            raise ValueError("private attachment timeout must be at most 300 seconds")
        self.calls.append(message)
        operation = message["operation"]
        if operation == "inventory":
            return {
                **{
                    key: self.session[key]
                    for key in ("workerId", "poolId", "hostId", "allocationId", "generation")
                },
                "capacity": 1,
                "draining": False,
                "activeReservations": 1,
                "sessions": [dict(self.session)],
            }
        assert self.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations
        if operation == "associate-job":
            assert self.association is None
            self.association = dict(message)
            return {
                "schema": "hi/fleet/job/v1",
                "jobId": message["jobId"],
                "status": "associated",
                "lease": {"leaseId": "lease-1", "bindingDigest": self.lease_digest},
            }
        assert operation == "job-result"
        self.reads += 1
        if self.reads == 1:
            if self.pending_only:
                self.now[0] = self.deadline + 1
            return {
                "schema": "hi/fleet/job/v1",
                "jobId": message["jobId"],
                "status": "pending",
                "result": None,
            }
        self.session.update(
            activity="idle",
            outcome="completed",
            backgroundCleanup="confirmed_empty",
            containmentDisposal={"leaseId": "lease-1", "digest": "d" * 64},
            providerTurnId="turn-1",
        )
        self.terminal_sent = True
        answer = "The worker's actual final answer."
        result = {
            "schema": "hi/fleet/job-result/v1",
            "jobId": message["jobId"],
            "bindingDigest": message["bindingDigest"],
            "owner": owner_identity(self.session),
            "input": {
                "commandId": self.association["inputCommandId"],
                "idempotencyKey": self.association["inputIdempotencyKey"],
                "sha256": self.association["inputSha256"],
            },
            "providerTurnId": "turn-1",
            "outcome": "completed",
            "answer": {
                "itemId": "answer-1",
                "text": answer,
                "sha256": hashlib.sha256(answer.encode()).hexdigest(),
            },
            "error": None,
            "disposal": self.session["containmentDisposal"],
        }
        return {
            "schema": "hi/fleet/job/v1",
            "jobId": message["jobId"],
            "status": "completed",
            "result": {**result, "sha256": digest(result)},
        }


class _Controller:
    """Keep the supported async client methods on their owning loop."""

    def __init__(self, record, worker, spool, manager, *, command_error=None) -> None:
        self.record = record
        self.worker = worker
        self.spool = spool
        self.manager = manager
        self.command_error = command_error
        self.loop = asyncio.get_running_loop()
        self.commands = []
        self.inputs = []

    async def fleet_get(self, kind, identity):
        assert asyncio.get_running_loop() is self.loop
        if kind == "sessions":
            assert identity == self.record["sessionId"]
            return dict(self.record)
        assert kind == "workers" and identity == self.record["workerId"]
        return {
            "schema": "hi/fleet/v1",
            "kind": "workers",
            "id": identity,
            "generation": self.record["generation"],
            "poolId": self.record["poolId"],
            "host": self.record["host"],
            "allocationId": self.record["allocationId"],
            "status": "created",
            "capacity": 1,
        }

    async def fleet_command(self, kind, identity, operation, body):
        assert asyncio.get_running_loop() is self.loop
        assert (kind, identity, operation) == ("sessions", self.record["sessionId"], "input")
        assert self.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations
        assert self.worker.association is not None
        assert set(body) == {"commandId", "idempotencyKey", "generation", "payload"}
        assert set(body["payload"]) == {"inputRef"}
        path = self.spool / body["payload"]["inputRef"]
        assert path.stat().st_mode & 0o077 == 0
        document = json.loads(path.read_bytes())
        assert document == {
            "schema": "hi/fleet/private-input/v1",
            "commandId": body["commandId"],
            "workerId": self.record["workerId"],
            "generation": self.record["generation"],
            "sessionId": identity,
            "kind": "input",
            "text": "Implement the admitted issue.",
        }
        self.inputs.append(document)
        self.commands.append(body)
        if self.command_error is not None:
            raise self.command_error
        self.worker.session.update(activity="model_working", providerTurnId="turn-1")
        envelope = {
            "schema": "hi/fleet/v1",
            **body,
            "operation": operation,
            "targetKind": kind,
            "targetId": identity,
            "workerId": self.record["workerId"],
            **{
                key: self.record[key]
                for key in ("taskId", "agentId", "sessionId", "executionId", "workspace", "stage")
            },
        }
        return {"command": envelope, "status": "completed"}


def _attempt(manager, binding, plan="Synthetic authenticated finalized plan."):
    """Bind an existing synthetic admission to the real owned source receipt."""
    attempt_type = getattr(fleet_execution, "FleetAttempt", None)
    assert callable(attempt_type), "the Fleet route needs its bound attempt"
    assignment = {
        "workerId": "worker-1",
        "poolId": "pool-1",
        "host": "laptop",
        "allocationId": "allocation-1",
        "generation": 1,
        "sessionId": "session-1",
        "taskId": "task-1",
        "executionId": "execution-1",
        "agentId": "agent-1",
        "workspace": str(binding.cwd),
        "stage": "implementation",
        "domain": "software",
        "hmasRole": "implementer",
        "issueUrl": "https://github.com/HomericIntelligence/Athena/issues/265",
    }
    return attempt_type(
        repository="HomericIntelligence/Athena",
        issue=265,
        plan_sha256=hashlib.sha256(plan.encode()).hexdigest(),
        workspace=binding,
        source_receipt=manager._read_receipt(265, SourceLane.IMPLEMENTATION),
        assignment=assignment,
        provider_thread_id="thread-1",
        lease_id="lease-1",
        lease_binding_digest="c" * 64,
        job_id="job-1",
        command_id="input-1",
        idempotency_key="input-key-1",
        input_reference="e" * 32 + ".json",
    )


async def _protocol_case(
    tmp_path,
    *,
    pending_only=False,
    command_error=None,
    lease_digest="c" * 64,
    turn_budget_s=20,
    assignment_stage="implementation",
):
    executor_type = getattr(fleet_execution, "FleetExecutor", None)
    assert callable(executor_type), "the Fleet route needs its concrete private-result executor"
    manager, binding = _source(tmp_path)
    plan = "Synthetic authenticated finalized plan."
    attempt = _attempt(manager, binding, plan)
    assignment = {
        **attempt.assignment,
        "stage": assignment_stage,
        "hmasRole": "task-agent",
    }
    session = {
        **{
            key: value
            for key, value in assignment.items()
            if key not in {"host", "domain", "hmasRole", "issueUrl"}
        },
        "hostId": assignment["host"],
        "providerThreadId": "thread-1",
        "providerTurnId": None,
        "released": False,
        "admissionReserved": True,
        "activity": "idle",
        "outcome": None,
    }
    now = [time.monotonic()]
    deadline = now[0] + turn_budget_s
    worker = _PrivateWorker(
        session, manager, now, deadline, pending_only=pending_only, lease_digest=lease_digest
    )
    record = {
        "schema": "hi/fleet/v1",
        "kind": "sessions",
        "id": "session-1",
        **assignment,
        "claimStatus": "claimed",
        "status": "idle",
    }
    with tempfile.TemporaryDirectory(
        prefix="fleet-input-test-", dir=Path.home() / ".cache"
    ) as temporary:
        spool = Path(temporary)
        client = _Controller(record, worker, spool, manager, command_error=command_error)
        try:
            attempt = replace(attempt, assignment=assignment)
        except fleet_execution.FleetProtocolError as error:
            return SimpleNamespace(
                rejection=str(error),
                manager=manager,
                binding=binding,
                client=client,
                worker=worker,
                attempt=attempt,
                files={path.name: path.read_bytes() for path in spool.iterdir()},
            )
        executor = executor_type(
            attempt,
            client=client,
            loop=asyncio.get_running_loop(),
            private_spool=spool,
            worker_exchange=worker.exchange,
            plan_reader=lambda: CurrentPlanRead(PlanDiscoveryResult.found(plan), True),
            monotonic=lambda: now[0],
            poll_interval_s=0,
        )
        job = replace(
            _job(binding),
            timeout_s=max(30, int(turn_budget_s)),
            fleet_attempt=attempt,
            prompt_builder=lambda **_kwargs: "Implement the admitted issue.",
            prompt_kwargs={"issue_body": plan},
        )
        pool = WorkerPool(
            size=1, shutdown=threading.Event(), completion_q=queue.Queue(), fleet_executor=executor
        )
        try:
            with patch(f"{_WORKER}.resolve_agent") as local:
                result = await asyncio.to_thread(pool._run_agent, job, deadline_s=deadline)
            local.assert_not_called()
            files = {path.name: path.read_bytes() for path in spool.iterdir()}
            return SimpleNamespace(
                rejection=None,
                result=result,
                manager=manager,
                binding=binding,
                client=client,
                worker=worker,
                attempt=attempt,
                files=files,
            )
        finally:
            pool.shutdown()


def test_private_result_follows_one_durable_input_and_pending_read(tmp_path):
    """Only matching terminal evidence can finish the admitted AgentJob."""
    case = asyncio.run(_protocol_case(tmp_path))
    assert case.result.ok is True
    assert case.result.value == "The worker's actual final answer."
    assert case.result.fleet_hold is None
    assert case.attempt.assignment["hmasRole"] == "task-agent"
    assert case.attempt.assignment["domain"] == "software"
    assert len(case.client.commands) == 1
    assert case.worker.reads == 2
    assert case.worker.terminal_sent
    assert list(case.files) == ["e" * 32 + ".json"]
    assert case.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations == ()


@pytest.mark.parametrize("stage", ["review", "planning"])
def test_matching_nonimplementation_admission_is_rejected_before_effects(tmp_path, stage):
    """Matching session ownership cannot authorize a different stage's model work."""
    case = asyncio.run(_protocol_case(tmp_path, assignment_stage=stage))
    assert case.client.record["stage"] == case.worker.session["stage"] == stage
    assert case.client.record["hmasRole"] == "task-agent"
    assert case.rejection == "fleet_implementation_admission_required"
    assert case.client.commands == []
    assert case.worker.calls == []
    assert case.worker.association is None
    assert case.files == {}
    assert case.manager._read_receipt(265, SourceLane.IMPLEMENTATION) == case.attempt.source_receipt


def test_ack_without_terminal_result_retains_the_fence(tmp_path):
    """An acknowledged input with an expired result deadline remains uncertain."""
    case = asyncio.run(_protocol_case(tmp_path, pending_only=True))
    assert case.result.ok is False
    assert case.result.fleet_hold == "reconciliation_required"
    assert len(case.client.commands) == 1
    assert case.worker.reads == 1
    assert not case.worker.terminal_sent
    assert case.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations


@pytest.mark.parametrize(
    "error", [ValueError("canonical task rejected input"), TimeoutError("input reply lost")]
)
def test_rejected_or_lost_input_reply_never_retries(tmp_path, error):
    """The durable attempt stays reserved without a second input submission."""
    case = asyncio.run(_protocol_case(tmp_path, command_error=error))
    assert case.result.ok is False
    assert case.result.fleet_hold == "reconciliation_required"
    assert len(case.client.commands) == 1
    assert case.worker.reads == 0
    assert case.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations


def test_association_lease_mismatch_stops_before_controller_input(tmp_path):
    """A replacement contained lease cannot consume the caller's old binding."""
    case = asyncio.run(_protocol_case(tmp_path, lease_digest="b" * 64))
    assert case.result.ok is False
    assert case.result.fleet_hold == "reconciliation_required"
    assert case.worker.association is not None
    assert case.client.commands == []
    assert case.worker.reads == 0
    assert case.manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations


def test_long_turn_budget_uses_bounded_private_attachment_requests(tmp_path):
    """An ordinary implementation budget fits the existing attachment's request limit."""
    case = asyncio.run(_protocol_case(tmp_path, turn_budget_s=1800))
    assert case.result.ok is True
    assert len(case.client.commands) == 1
    assert case.worker.terminal_sent
    assert case.worker.timeouts and max(case.worker.timeouts) <= 40


def test_private_final_answer_is_not_copied_to_generic_completion_events(tmp_path):
    """The normal stage receives its answer without publishing it as a diagnostic."""
    from hephaestus.automation.pipeline.coordinator_runtime import CoordinatorRuntime

    case = asyncio.run(_protocol_case(tmp_path))
    answer = "The worker's actual final answer."
    assert case.result.ok is True and case.result.value == answer
    fields = CoordinatorRuntime._job_result_event_fields(case.result)
    assert answer not in json.dumps(fields)
    assert fields["ok"] is True


def _fleet_coordinator(
    tmp_path,
    *,
    config_overrides=None,
    plan="Synthetic authenticated finalized plan.",
    source_repository="HomericIntelligence/Athena",
):
    """Construct the normal coordinator and worker lanes with one admitted attempt."""
    from hephaestus.automation.pipeline.coordinator import Coordinator
    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    assert "fleet_executor" in inspect.signature(Coordinator).parameters, (
        "the normal Coordinator must accept the opt-in Fleet executor"
    )
    manager, binding = _source(tmp_path, source_repository=source_repository)
    attempt = _attempt(manager, binding, plan)
    config = PipelineConfig(
        org="HomericIntelligence",
        repos=["Athena"],
        issues=[265],
        loops=1,
        max_workers=1,
        parallel_repos=1,
        agent="codex",
        no_advise=True,
        enable_learn=False,
        run_pre_pr_tests=True,
        rate_guard_enabled=False,
        repo_roots={"Athena": binding.reusable_root},
        projects_dir=tmp_path,
    )
    config = replace(config, **(config_overrides or {}))
    loop = asyncio.new_event_loop()
    executor = fleet_execution.FleetExecutor(
        attempt,
        client=Mock(),
        loop=loop,
        private_spool=tmp_path / "unused-private-spool",
        worker_exchange=Mock(),
        plan_reader=Mock(),
    )
    try:
        coordinator = Coordinator(
            config,
            github=FakeStageGitHub(),
            fleet_executor=executor,
            install_signals=False,
        )
    except Exception:
        loop.close()
        raise
    return coordinator, executor, manager, binding, loop


def test_normal_coordinator_supplies_fleet_executor_to_its_real_worker(tmp_path):
    """Production construction keeps the normal worker and GitHub job runner."""
    from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

    coordinator, executor, _, _, loop = _fleet_coordinator(tmp_path)
    try:
        assert isinstance(coordinator.pool, WorkerPool)
        assert coordinator.pool._fleet_executor is executor
        assert isinstance(coordinator.pool._github_job_runner, PipelineGitHubJobRunner)
        assert coordinator.pool._host_capabilities is not None
    finally:
        coordinator._shutdown_pool()
        loop.close()


@pytest.mark.parametrize("hold", ["reconciliation_required", "new_admission_required"])
def test_coordinator_holds_without_stage_retry_or_source_cleanup(tmp_path, monkeypatch, hold):
    """A held attempt ends locally while the admitted workspace remains owned."""
    from hephaestus.automation.pipeline.jobs import JobHandle
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
    from tests.unit.automation.pipeline.conftest import claim_test_item

    coordinator, executor, manager, binding, loop = _fleet_coordinator(tmp_path)
    item = WorkItem(
        repo="Athena",
        kind=ItemKind.ISSUE,
        issue=265,
        stage=StageName.IMPLEMENTATION,
        state="IMPLEMENT_WAIT",
        branch=executor.attempt.source_receipt.branch,
        worktree=str(binding.cwd),
        payload={
            "_impl_source_workspace": binding.to_dict(),
            "_impl_source_revision": binding.revision,
            "_impl_source_receipt": executor.attempt.source_receipt,
        },
    )
    job = replace(_job(binding), fleet_attempt=executor.attempt)
    handle = JobHandle(job=job, on_done_state="TEST_WAIT")
    before = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
    callback = Mock()
    advance = Mock()
    monkeypatch.setattr(coordinator.stages[StageName.IMPLEMENTATION], "on_job_done", callback)
    monkeypatch.setattr(coordinator, "_run_item", advance)
    try:
        claim_test_item(coordinator, item)
        coordinator.in_flight[handle] = item
        coordinator.inflight_per_repo[item.repo] = 1
        coordinator._handle_completion(
            handle, JobResult(ok=False, error="fleet_result_unconfirmed", fleet_hold=hold)
        )
        callback.assert_not_called()
        advance.assert_not_called()
        assert item.stage is StageName.IMPLEMENTATION
        assert item.state == "IMPLEMENT_WAIT"
        assert item.result is not None and not item.result.passed
        assert hold in item.result.reason
        assert coordinator._all_idle()
        assert coordinator.live_work_count == 0
        assert coordinator._exit_code() == 1
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) == before
        assert binding.cwd.is_dir()
        assert coordinator._active_preserved_worktrees() == [("Athena", 265, str(binding.cwd))]
        assert item.session_ids == {}
    finally:
        coordinator._shutdown_pool()
        loop.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"issues": [265, 266]},
        {"repos": ["Athena", "Odysseus"]},
        {"max_workers": 2},
        {"parallel_repos": 2},
        {"loops": 2},
        {"repo_source_factory": lambda _shutdown: iter(["Athena"])},
        {"enable_learn": True},
        {"no_advise": False},
        {"agent": "claude"},
        {"model": "operator-selected-model"},
        {"implementer_model": "operator-selected-model"},
        {"fallback_model": "operator-selected-model"},
        {"prs": [17]},
        {"rebase": True},
        {"update_plan": True},
        {"force": True},
        {"dry_run": True},
        {"run_pre_pr_tests": False},
    ],
    ids=[
        "extra-issue",
        "extra-repo",
        "extra-worker",
        "parallel-repos",
        "extra-pass",
        "discovery",
        "learning",
        "advice",
        "wrong-provider",
        "model-override",
        "role-model-override",
        "fallback",
        "pr-intake",
        "rebase",
        "plan-update",
        "force",
        "dry-run",
        "tests-disabled",
    ],
)
def test_fleet_coordinator_rejects_unadmitted_work_configuration(tmp_path, overrides):
    """An opt-in run cannot broaden its admission or silently ignore execution choices."""
    created = []
    try:
        with pytest.raises(ValueError, match="fleet"):
            created.append(_fleet_coordinator(tmp_path, config_overrides=overrides))
    finally:
        for coordinator, _, _, _, loop in created:
            coordinator._shutdown_pool()
            loop.close()


def _public_route_setup(tmp_path, monkeypatch):
    """Supply a sealed plan through the existing GitHub read interface."""
    from hephaestus.automation.pipeline_github import PipelineGitHub
    from hephaestus.automation.state_labels import ATHENA_FINALIZED_PLAN_LABEL, STATE_PLAN_GO
    from tests.unit.automation.test_finalized_plan_scope import _finalized_body

    route = getattr(fleet_execution, "run_fleet_pipeline", None)
    assert callable(route), "Fleet needs a public async entry through the normal Coordinator"
    body = _finalized_body("## Files to Modify\n- `tracked.txt`")
    prototype, executor, manager, binding, loop = _fleet_coordinator(tmp_path, plan=body)
    config, attempt = prototype.config, executor.attempt
    prototype._shutdown_pool()
    loop.close()
    snapshot = {
        "number": 265,
        "state": "OPEN",
        "title": "Implement the admitted issue",
        "body": body,
        "bodyDigest": hashlib.sha256(body.encode()).hexdigest(),
        "labels": [{"name": STATE_PLAN_GO}, {"name": ATHENA_FINALIZED_PLAN_LABEL}],
    }
    monkeypatch.setattr(PipelineGitHub, "gh_issue_json", lambda self, number: dict(snapshot))
    monkeypatch.setattr(PipelineGitHub, "issue_body_edited_by_viewer", lambda self, number: True)
    return route, config, attempt, body, manager, binding


def test_public_async_route_uses_normal_coordinator_and_caller_owned_interfaces(
    tmp_path, monkeypatch
):
    """The supported entry supplies its real worker while preserving client loop ownership."""
    from hephaestus.automation.pipeline.coordinator import Coordinator

    route, config, attempt, body, _, _ = _public_route_setup(tmp_path, monkeypatch)
    observed = []
    client, exchange = Mock(), Mock()

    async def exercise():
        caller_loop = asyncio.get_running_loop()

        def run(coordinator):
            try:
                executor = coordinator.pool._fleet_executor
                assert isinstance(coordinator.pool, WorkerPool)
                assert executor.attempt is attempt
                assert executor.client is client
                assert executor.loop is caller_loop
                assert not coordinator._install_signals
                plan = executor.plan_reader()
                assert plan.finalized_body and plan.plan.plan_text == body
                observed.append(coordinator)
                return 7
            finally:
                coordinator._shutdown_pool()

        monkeypatch.setattr(Coordinator, "run", run)
        return await route(
            config,
            client=client,
            attempt=attempt,
            private_spool=tmp_path / "unused-private-spool",
            worker_exchange=exchange,
        )

    assert asyncio.run(exercise()) == 7
    assert len(observed) == 1
    client.aclose.assert_not_called()
    exchange.assert_not_called()


def test_async_route_cancellation_waits_for_the_owned_coordinator_thread(tmp_path, monkeypatch):
    """Cancellation signals the finite worker lifecycle and waits for its exit."""
    from hephaestus.automation.pipeline.coordinator import Coordinator

    route, config, attempt, _, manager, binding = _public_route_setup(tmp_path, monkeypatch)
    started, finished = threading.Event(), threading.Event()
    before = manager._read_receipt(265, SourceLane.IMPLEMENTATION)

    def run(coordinator):
        started.set()
        try:
            assert coordinator.worker_shutdown_event.wait(2), "cancel must interrupt owned work"
            assert coordinator.shutdown_event.is_set()
            return 130
        finally:
            coordinator._shutdown_pool()
            finished.set()

    monkeypatch.setattr(Coordinator, "run", run)

    async def exercise():
        task = asyncio.create_task(
            route(
                config,
                client=Mock(),
                attempt=attempt,
                private_spool=tmp_path / "unused-private-spool",
                worker_exchange=Mock(),
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert finished.is_set(), "a cancelled wrapper must not leave its writer thread running"

    asyncio.run(exercise())
    assert manager._read_receipt(265, SourceLane.IMPLEMENTATION) == before
    assert binding.cwd.is_dir()
