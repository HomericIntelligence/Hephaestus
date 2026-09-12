"""Explicit worker lane and factory contracts for the coordinator."""

from threading import Event
from typing import Protocol

from .athena_skill_jobs import AthenaSkillJob
from .github_jobs import GitHubJob
from .jobs import AgentJob, BuildTestJob, CompactJob, GitJob, JobHandle, JobResult
from .queues import CompletionQueue
from .routing import StageName


class WorkerLane(Protocol):
    """Provide completion signals and bounded shutdown for one worker lane."""

    def set_completion_notifiers(self, *, wakeup: Event, saturation: Event) -> None:
        """Use the coordinator's wake and saturation signals."""
        ...

    def shutdown(self, *, mark_interrupted: bool = True) -> None:
        """Stop the lane and preserve the caller's interruption decision."""
        ...


class MainWorker(WorkerLane, Protocol):
    """Run ordinary work and release its local completion authority."""

    def submit(
        self,
        job: AgentJob | BuildTestJob | CompactJob | GitJob | GitHubJob | AthenaSkillJob,
        on_done_state: str | StageName,
        *,
        claim_key: str = "",
        claim_stage: str = "",
        remediation_owner_id: int | None = None,
    ) -> JobHandle:
        """Submit one ordinary job with its owner identity."""
        ...

    def discard_remediation_pretest_successes(self, claim_key: str, *, owner_id: int) -> None:
        """Release authority when its owner no longer holds a work permit."""
        ...

    def run_cleanup_git(self, job: GitJob) -> JobResult:
        """Run one allowlisted cleanup operation in the auxiliary lane."""
        ...

    def release_repo_intake_leases(self) -> None:
        """Release run-lifetime repository-intake leases."""
        ...


class AuxiliaryWorker(WorkerLane, Protocol):
    """Run host learning and terminal cleanup on an independent lane."""

    def submit(self, job: object, on_done_state: str) -> JobHandle:
        """Submit one allowlisted auxiliary job."""
        ...


class WorkerFactory[Lane: WorkerLane](Protocol):
    """Create one lane with the coordinator's completion channel and cancellation."""

    def __call__(self, *, size: int, shutdown: Event, completion_q: CompletionQueue) -> Lane:
        """Create the lane before the coordinator admits any job."""
        ...
