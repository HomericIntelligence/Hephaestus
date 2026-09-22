"""Execute one admitted Fleet job within its existing source lease."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from hephaestus.agents.execution_policy import AgentOperation, AgentRole, SessionLifecycle
from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind
from hephaestus.automation.current_plan import CurrentPlanRead, read_current_plan
from hephaestus.automation.fleet_job_results import MAX_ANSWER_BYTES, digest, encoded
from hephaestus.automation.review_journal import PlanDiscoveryStatus
from hephaestus.automation.source_worktree import FleetAttemptFence, SourceWorkspaceReceipt

from .job_results import JobResult

if TYPE_CHECKING:
    from .coordinator_types import PipelineConfig
    from .jobs import AgentJob

_MAX_PRIVATE_INPUT = 128 * 1024
_MAX_PRIVATE_FRAME = 1024 * 1024
_ASSIGNMENT_FIELDS = frozenset(
    {
        "workerId",
        "poolId",
        "host",
        "allocationId",
        "generation",
        "sessionId",
        "taskId",
        "executionId",
        "agentId",
        "workspace",
        "stage",
        "domain",
        "hmasRole",
        "issueUrl",
    }
)
_COMMAND_OWNER_FIELDS = ("taskId", "agentId", "sessionId", "executionId", "workspace", "stage")


class FleetProtocolError(ValueError):
    """Reject an unconfirmed boundary without requesting another turn."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise FleetProtocolError(reason)


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FleetProtocolError("fleet_record_invalid")
    _require(len(encoded(value)) <= _MAX_PRIVATE_FRAME, "fleet_record_limit")
    return value


def _matches(record: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    return all(
        type(record.get(key)) is type(value) and record.get(key) == value
        for key, value in expected.items()
    )


@dataclass(frozen=True)
class FleetAttempt:
    """Bind one existing admission and source receipt without replacing either."""

    repository: str
    issue: int
    plan_sha256: str
    workspace: WorkspaceBinding
    source_receipt: SourceWorkspaceReceipt
    assignment: Mapping[str, object]
    provider_thread_id: str
    lease_id: str
    lease_binding_digest: str
    job_id: str
    command_id: str
    idempotency_key: str
    input_reference: str

    def __post_init__(self) -> None:
        """Freeze the bounded admission references before constructing input."""
        _require(type(self.issue) is int and self.issue > 0, "fleet_issue_invalid")
        _require(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is not None,
            "fleet_repository_invalid",
        )
        _require(_sha(self.plan_sha256) and _sha(self.lease_binding_digest), "fleet_digest_invalid")
        _require(set(self.assignment) == _ASSIGNMENT_FIELDS, "fleet_assignment_invalid")
        for key, value in self.assignment.items():
            if key == "generation":
                _require(type(value) is int and value > 0, "fleet_generation_invalid")
            elif key != "allocationId" or value is not None:
                _require(
                    isinstance(value, str) and 0 < len(value) <= 1024, "fleet_identity_invalid"
                )
        _require(
            self.assignment["stage"] == "implementation",
            "fleet_implementation_admission_required",
        )
        for value in (
            self.provider_thread_id,
            self.lease_id,
            self.job_id,
            self.command_id,
            self.idempotency_key,
        ):
            _require(isinstance(value, str) and 0 < len(value) <= 1024, "fleet_identity_invalid")
        _require(
            re.fullmatch(r"[0-9a-f]{32}\.json", self.input_reference) is not None,
            "fleet_input_reference_invalid",
        )
        workspace = self.workspace
        _require(
            workspace.kind is WorkspaceKind.SOURCE
            and workspace.lane is SourceLane.IMPLEMENTATION
            and workspace.schema_version == 1
            and not workspace.detached
            and workspace.item_number == self.issue
            and workspace.repository in {self.repository, self.repository.rsplit("/", 1)[-1]}
            and workspace.reusable_root is not None,
            "fleet_source_invalid",
        )
        _require(
            workspace.reusable_root is not None
            and self.source_receipt.to_binding(workspace.reusable_root) == workspace
            and bool(self.source_receipt.branch),
            "fleet_source_receipt_invalid",
        )
        _require(self.assignment["workspace"] == str(workspace.cwd), "fleet_workspace_mismatch")
        _require(
            self.assignment["issueUrl"]
            == f"https://github.com/{self.repository}/issues/{self.issue}",
            "fleet_issue_mismatch",
        )
        object.__setattr__(self, "assignment", MappingProxyType(dict(self.assignment)))

    @property
    def binding_digest(self) -> str:
        """Hash the immutable references for private correlation only."""
        return digest(
            {
                "repository": self.repository,
                "issue": self.issue,
                "planSha256": self.plan_sha256,
                "workspace": self.workspace.to_dict(),
                "sourceReceipt": self.source_receipt.to_dict(),
                "assignment": dict(self.assignment),
                "providerThreadId": self.provider_thread_id,
                "leaseId": self.lease_id,
                "leaseBindingDigest": self.lease_binding_digest,
                "jobId": self.job_id,
                "commandId": self.command_id,
                "idempotencyKey": self.idempotency_key,
                "inputRef": self.input_reference,
            }
        )

    @property
    def owner(self) -> dict[str, object]:
        """Map supported public ownership fields to the private worker schema."""
        return {
            **{
                key: self.assignment[key]
                for key in (
                    "workerId",
                    "poolId",
                    "allocationId",
                    "generation",
                    "sessionId",
                    "taskId",
                    "executionId",
                    "agentId",
                    "workspace",
                    "stage",
                )
            },
            "hostId": self.assignment["host"],
            "providerThreadId": self.provider_thread_id,
        }


class FleetController(Protocol):
    """Use the supported caller-owned Agamemnon client."""

    async def fleet_get(self, kind: str, resource_id: str) -> dict[str, Any]:
        """Read an existing controller resource."""
        ...

    async def fleet_command(
        self, kind: str, resource_id: str, operation: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Submit one control command through the canonical controller."""
        ...


class PrivateWorkerExchange(Protocol):
    """Use the existing bounded private worker attachment."""

    def __call__(self, message: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Exchange one bounded private worker message."""
        ...


class FleetJobExecutor(Protocol):
    """Consume private Fleet evidence without granting admission."""

    @property
    def attempt(self) -> FleetAttempt:
        """Return the caller's existing admission references."""
        ...

    def execute(
        self,
        job: AgentJob,
        cwd: Path,
        *,
        deadline_s: float,
        shutdown: threading.Event,
        source_fence: FleetAttemptFence | None = None,
    ) -> JobResult:
        """Return one result while the worker retains its source lease."""
        ...


def is_initial_implementation(job: AgentJob) -> bool:
    """Identify the only model operation supported by the initial Fleet route."""
    request = job.execution_request
    return (
        request is not None
        and request.role is AgentRole.IMPLEMENTER
        and request.operation is AgentOperation.IMPLEMENT
        and request.lifecycle is SessionLifecycle.START_NEW
        and job.descr == "implement"
        and job.resume_session_id is None
        and job.resume_binding is None
    )


def _private_directory(path: Path, workspace: Path) -> int:
    canonical = path.resolve(strict=True)
    _require(path.is_absolute() and path == canonical, "fleet_spool_path_invalid")
    shared = (
        Path("/tmp"),  # nosec B108 - explicit shared-scratch blocklist entry
        Path("/var/tmp"),  # nosec B108 - explicit shared-scratch blocklist entry
        Path("/private/var/folders"),
        Path(tempfile.gettempdir()),
    )
    _require(
        not any(canonical.is_relative_to(root.resolve()) for root in shared),
        "fleet_spool_in_shared_scratch",
    )
    _require(
        not canonical.is_relative_to(workspace) and not workspace.is_relative_to(canonical),
        "fleet_spool_workspace_overlap",
    )
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    observed = os.fstat(descriptor)
    if observed.st_uid != os.getuid() or observed.st_mode & 0o077:
        os.close(descriptor)
        raise FleetProtocolError("fleet_spool_owner_or_mode")
    return descriptor


def _read_input(directory: int, reference: str) -> bytes:
    descriptor = os.open(reference, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.getuid()
            and not before.st_mode & 0o077,
            "fleet_input_owner_or_mode",
        )
        _require(before.st_size <= _MAX_PRIVATE_INPUT, "fleet_input_limit")
        data = bytearray()
        while len(data) <= _MAX_PRIVATE_INPUT:
            part = os.read(descriptor, min(8192, _MAX_PRIVATE_INPUT + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(descriptor)
        _require(
            (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and len(data) <= _MAX_PRIVATE_INPUT,
            "fleet_input_changed",
        )
        return bytes(data)
    finally:
        os.close(descriptor)


def _write_input(path: Path, attempt: FleetAttempt, prompt: str) -> None:
    data = encoded(
        {
            "schema": "hi/fleet/private-input/v1",
            "commandId": attempt.command_id,
            "workerId": attempt.assignment["workerId"],
            "generation": attempt.assignment["generation"],
            "sessionId": attempt.assignment["sessionId"],
            "kind": "input",
            "text": prompt,
        }
    )
    _require(len(data) <= _MAX_PRIVATE_INPUT, "fleet_input_limit")
    directory = _private_directory(path, attempt.workspace.cwd)
    try:
        try:
            descriptor = os.open(
                attempt.input_reference,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            _require(
                _read_input(directory, attempt.input_reference) == data, "fleet_input_conflict"
            )
            return
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                _require(written > 0, "fleet_input_write_failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)
        _require(_read_input(directory, attempt.input_reference) == data, "fleet_input_unconfirmed")
    finally:
        os.close(directory)


class FleetExecutor:
    """Consume one admitted private turn while the normal worker holds its source."""

    def __init__(
        self,
        attempt: FleetAttempt,
        *,
        client: FleetController,
        loop: asyncio.AbstractEventLoop,
        private_spool: Path,
        worker_exchange: PrivateWorkerExchange,
        plan_reader: Callable[[], CurrentPlanRead],
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_s: float = 0.1,
    ) -> None:
        """Keep credentials and event-loop lifetime with the supplied client."""
        _require(0 <= poll_interval_s <= 1, "fleet_poll_interval_invalid")
        self.attempt = attempt
        self.client = client
        self.loop = loop
        self.private_spool = private_spool
        self.worker_exchange = worker_exchange
        self.plan_reader = plan_reader
        self.monotonic = monotonic
        self.poll_interval_s = poll_interval_s
        self._used = False
        self._lock = threading.Lock()

    def _remaining(self, deadline: float, shutdown: threading.Event) -> float:
        _require(not shutdown.is_set(), "fleet_shutdown_requires_reconciliation")
        remaining = deadline - self.monotonic()
        _require(remaining > 0, "fleet_deadline_requires_reconciliation")
        return remaining

    def _control(
        self, method: str, args: tuple[Any, ...], deadline: float, shutdown: threading.Event
    ) -> dict[str, Any]:
        self._remaining(deadline, shutdown)
        coroutine = (
            self.client.fleet_get(*args) if method == "get" else self.client.fleet_command(*args)
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            while True:
                try:
                    return _record(
                        future.result(timeout=min(0.1, self._remaining(deadline, shutdown)))
                    )
                except TimeoutError:
                    if future.done():
                        raise
        except BaseException:
            future.cancel()
            raise

    def _private(
        self, message: dict[str, Any], deadline: float, shutdown: threading.Event
    ) -> dict[str, Any]:
        response = self.worker_exchange(
            message, timeout=min(40.0, self._remaining(deadline, shutdown))
        )
        self._remaining(deadline, shutdown)
        return _record(response)

    def _session(self, deadline: float, shutdown: threading.Event) -> dict[str, Any]:
        inventory = self._private({"operation": "inventory"}, deadline, shutdown)
        expected = {
            key: self.attempt.owner[key]
            for key in ("workerId", "poolId", "hostId", "allocationId", "generation")
        }
        _require(
            _matches(inventory, expected) and inventory.get("draining") is False,
            "fleet_inventory_mismatch",
        )
        sessions = inventory.get("sessions")
        if not isinstance(sessions, list):
            raise FleetProtocolError("fleet_sessions_invalid")
        matches = [
            value
            for value in sessions
            if isinstance(value, dict)
            and value.get("sessionId") == self.attempt.assignment["sessionId"]
        ]
        _require(len(matches) == 1, "fleet_session_missing_or_duplicate")
        session = _record(matches[0])
        _require(
            _matches(session, self.attempt.owner)
            and session.get("admissionReserved") is True
            and session.get("released") is False,
            "fleet_private_owner_mismatch",
        )
        return session

    def _validate(
        self, job: AgentJob, cwd: Path, deadline: float, shutdown: threading.Event
    ) -> None:
        attempt = self.attempt
        _require(
            is_initial_implementation(job)
            and job.agent == "codex"
            and not job.model
            and not job.fallback_model
            and job.fleet_attempt == attempt,
            "fleet_job_requires_new_admission",
        )
        _require(
            job.repo in {attempt.repository, attempt.repository.rsplit("/", 1)[-1]}
            and job.issue == attempt.issue
            and job.workspace == attempt.workspace
            and cwd == attempt.workspace.cwd,
            "fleet_job_source_mismatch",
        )
        self.validate_plan(job.prompt_kwargs.get("issue_body"))
        record = self._control(
            "get", ("sessions", attempt.assignment["sessionId"]), deadline, shutdown
        )
        _require(
            _matches(
                record,
                {
                    "schema": "hi/fleet/v1",
                    "kind": "sessions",
                    "id": attempt.assignment["sessionId"],
                    **attempt.assignment,
                },
            )
            and record.get("claimStatus") in {"claimed", "reserved"}
            and record.get("status") in {"idle", "running", "admitted"},
            "fleet_controller_owner_mismatch",
        )
        worker = self._control(
            "get", ("workers", attempt.assignment["workerId"]), deadline, shutdown
        )
        _require(
            _matches(
                worker,
                {
                    "schema": "hi/fleet/v1",
                    "kind": "workers",
                    "id": attempt.assignment["workerId"],
                    **{
                        key: attempt.assignment[key]
                        for key in ("poolId", "host", "allocationId", "generation")
                    },
                },
            )
            and worker.get("status") != "draining",
            "fleet_controller_worker_mismatch",
        )
        session = self._session(deadline, shutdown)
        _require(
            session.get("activity") == "idle" and session.get("outcome") is None,
            "fleet_session_not_ready",
        )

    def validate_plan(self, expected_body: object) -> None:
        """Require the current authenticated plan to match this admission."""
        plan = self.plan_reader()
        body = plan.plan.plan_text
        _require(
            plan.finalized_body
            and plan.plan.status is PlanDiscoveryStatus.FOUND
            and isinstance(body, str)
            and hashlib.sha256(body.encode()).hexdigest() == self.attempt.plan_sha256
            and expected_body == body,
            "fleet_finalized_plan_changed",
        )

    def _dispatch(
        self,
        prompt: str,
        deadline: float,
        shutdown: threading.Event,
        source_fence: FleetAttemptFence,
    ) -> tuple[dict[str, Any], str]:
        attempt = self.attempt
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        _write_input(self.private_spool, attempt, prompt)
        source_fence.arm()
        selector = {
            "schema": "hi/fleet/job/v1",
            "jobId": attempt.job_id,
            "targetId": attempt.assignment["sessionId"],
            "generation": attempt.assignment["generation"],
            "bindingDigest": attempt.binding_digest,
        }
        associated = self._private(
            {
                **selector,
                "operation": "associate-job",
                "inputCommandId": attempt.command_id,
                "inputIdempotencyKey": attempt.idempotency_key,
                "inputSha256": prompt_sha,
            },
            deadline,
            shutdown,
        )
        _require(
            _matches(
                associated,
                {"schema": "hi/fleet/job/v1", "jobId": attempt.job_id, "status": "associated"},
            )
            and associated.get("lease")
            == {"leaseId": attempt.lease_id, "bindingDigest": attempt.lease_binding_digest},
            "fleet_association_mismatch",
        )
        body = {
            "commandId": attempt.command_id,
            "idempotencyKey": attempt.idempotency_key,
            "generation": attempt.assignment["generation"],
            "payload": {"inputRef": attempt.input_reference},
        }
        response = self._control(
            "command",
            ("sessions", attempt.assignment["sessionId"], "input", body),
            deadline,
            shutdown,
        )
        expected = {
            "schema": "hi/fleet/v1",
            **body,
            "operation": "input",
            "targetKind": "sessions",
            "targetId": attempt.assignment["sessionId"],
            "workerId": attempt.assignment["workerId"],
            **{key: attempt.assignment[key] for key in _COMMAND_OWNER_FIELDS},
        }
        command = _record(response.get("command"))
        _require(
            set(command) == set(expected)
            and _matches(command, expected)
            and response.get("status") in {"pending", "accepted", "completed"},
            "fleet_input_receipt_mismatch",
        )
        return selector, prompt_sha

    def _terminal(
        self, selector: dict[str, Any], prompt_sha: str, deadline: float, shutdown: threading.Event
    ) -> dict[str, Any]:
        while True:
            response = self._private({**selector, "operation": "job-result"}, deadline, shutdown)
            _require(
                _matches(response, {"schema": "hi/fleet/job/v1", "jobId": self.attempt.job_id}),
                "fleet_result_identity_mismatch",
            )
            status = response.get("status")
            if status != "pending":
                if not isinstance(status, str) or status not in {"completed", "failed"}:
                    raise FleetProtocolError("fleet_result_unknown")
                result = _record(response.get("result"))
                self._verify_terminal(result, status, prompt_sha, deadline, shutdown)
                return result
            _require(response.get("result") is None, "fleet_pending_result_conflict")
            shutdown.wait(min(self.poll_interval_s, self._remaining(deadline, shutdown)))

    def _verify_terminal(
        self,
        result: dict[str, Any],
        status: str,
        prompt_sha: str,
        deadline: float,
        shutdown: threading.Event,
    ) -> None:
        attempt = self.attempt
        _require(
            result.get("sha256")
            == digest({key: value for key, value in result.items() if key != "sha256"}),
            "fleet_result_digest_mismatch",
        )
        owner = _record(result.get("owner"))
        _require(
            _matches(
                result,
                {
                    "schema": "hi/fleet/job-result/v1",
                    "jobId": attempt.job_id,
                    "bindingDigest": attempt.binding_digest,
                    "outcome": status,
                },
            )
            and set(owner) == set(attempt.owner)
            and _matches(owner, attempt.owner)
            and result.get("input")
            == {
                "commandId": attempt.command_id,
                "idempotencyKey": attempt.idempotency_key,
                "sha256": prompt_sha,
            },
            "fleet_result_owner_mismatch",
        )
        turn = result.get("providerTurnId")
        _require(isinstance(turn, str) and 0 < len(turn) <= 1024, "fleet_result_turn_invalid")
        session = self._session(deadline, shutdown)
        disposal = result.get("disposal")
        _require(
            isinstance(disposal, dict)
            and set(disposal) == {"leaseId", "digest"}
            and disposal.get("leaseId") == attempt.lease_id
            and _sha(disposal.get("digest"))
            and session.get("containmentDisposal") == disposal
            and session.get("backgroundCleanup") == "confirmed_empty"
            and session.get("providerTurnId") == turn
            and session.get("activity") == "idle"
            and session.get("outcome") == status,
            "fleet_disposal_unconfirmed",
        )
        answer = result.get("answer")
        if answer is not None:
            _require(isinstance(answer, dict), "fleet_answer_invalid")
            text = answer.get("text")
            _require(
                set(answer) == {"itemId", "text", "sha256"}
                and isinstance(answer.get("itemId"), str)
                and 0 < len(answer["itemId"]) <= 1024
                and isinstance(text, str)
                and 0 < len(text.encode()) <= MAX_ANSWER_BYTES
                and answer.get("sha256") == hashlib.sha256(text.encode()).hexdigest(),
                "fleet_answer_invalid",
            )
        if status == "completed":
            _require(answer is not None and result.get("error") is None, "fleet_answer_missing")
        else:
            error = result.get("error")
            _require(
                isinstance(error, dict)
                and isinstance(error.get("message"), str)
                and bool(error["message"])
                and len(encoded(error)) <= MAX_ANSWER_BYTES,
                "fleet_provider_error_invalid",
            )

    def execute(
        self,
        job: AgentJob,
        cwd: Path,
        *,
        deadline_s: float,
        shutdown: threading.Event,
        source_fence: FleetAttemptFence | None = None,
    ) -> JobResult:
        """Return terminal output or a finite hold, never a second input."""
        with self._lock:
            if self._used:
                return JobResult(
                    ok=False,
                    error="fleet_new_admission_required",
                    fleet_hold="new_admission_required",
                )
            self._used = True
        try:
            _require(source_fence is not None, "fleet_source_fence_missing")
            if source_fence is None:
                raise FleetProtocolError("fleet_source_fence_missing")
            self._validate(job, cwd, deadline_s, shutdown)
            prompt = job.prompt_builder(**job.prompt_kwargs)
            _require(isinstance(prompt, str) and bool(prompt), "fleet_prompt_invalid")
            selector, prompt_sha = self._dispatch(prompt, deadline_s, shutdown, source_fence)
            result = self._terminal(selector, prompt_sha, deadline_s, shutdown)
            source_fence.complete()
            if result["outcome"] == "failed":
                return JobResult(
                    ok=False,
                    value=result,
                    error="fleet_provider_failed",
                    fleet_hold="new_admission_required",
                )
            answer = result["answer"]["text"]
            try:
                value = job.parse(answer) if job.parse is not None else answer
            except Exception:
                return JobResult(
                    ok=False,
                    value=result,
                    error="fleet_result_parse_failed",
                    fleet_hold="new_admission_required",
                )
            return JobResult(ok=True, value=value)
        except FleetProtocolError as exc:
            return JobResult(ok=False, error=str(exc), fleet_hold="reconciliation_required")
        except Exception as exc:
            return JobResult(
                ok=False,
                error=f"fleet_exchange_unconfirmed:{type(exc).__name__}",
                fleet_hold="reconciliation_required",
            )


def validate_fleet_config(config: PipelineConfig, executor: FleetExecutor | None) -> None:
    """Require one explicit implementation run before constructing worker lanes."""
    if executor is None:
        return
    attempt = executor.attempt
    organization, repository = attempt.repository.split("/", 1)
    _require(
        config.org == organization
        and config.repos == [repository]
        and config.issues == [attempt.issue]
        and not config.prs
        and config.repo_source_factory is None
        and config.scope is None,
        "fleet_pipeline_scope_invalid",
    )
    _require(
        (config.max_workers, config.parallel_repos, config.loops) == (1, 1, 1)
        and not config.enable_learn
        and config.no_advise
        and config.run_pre_pr_tests
        and not any(
            (
                config.rebase,
                config.update_plan,
                config.force,
                config.dry_run,
                config.explicit_pr_review,
                config.rate_guard_enabled,
            )
        ),
        "fleet_pipeline_work_configuration_invalid",
    )
    _require(
        config.agent == "codex"
        and config.implementer_agent in {"", "codex"}
        and not any(
            (
                config.model,
                config.implementer_model,
                config.planner_model,
                config.reviewer_model,
                config.fallback_model,
                config.codex_isolation_adapter,
            )
        ),
        "fleet_pipeline_provider_configuration_invalid",
    )


async def run_fleet_pipeline(
    config: PipelineConfig,
    *,
    client: FleetController,
    attempt: FleetAttempt,
    private_spool: Path,
    worker_exchange: PrivateWorkerExchange,
) -> int:
    """Run the normal coordinator with a caller-owned admitted Fleet connection."""
    from .coordinator import _build_pipeline_coordinator

    executor = FleetExecutor(
        attempt,
        client=client,
        loop=asyncio.get_running_loop(),
        private_spool=private_spool,
        worker_exchange=worker_exchange,
        plan_reader=lambda: read_current_plan(
            attempt.issue,
            coordinator._ctx_for_repo(attempt.repository.rsplit("/", 1)[-1]).github,
        ),
    )
    validate_fleet_config(config, executor)
    coordinator = _build_pipeline_coordinator(
        config, fleet_executor=executor, install_signals=False
    )
    run = asyncio.create_task(asyncio.to_thread(coordinator.run))
    try:
        return await asyncio.shield(run)
    except asyncio.CancelledError:
        coordinator.shutdown_event.set()
        coordinator.worker_shutdown_event.set()
        coordinator._wake_completion_wait()
        await asyncio.shield(run)
        raise
