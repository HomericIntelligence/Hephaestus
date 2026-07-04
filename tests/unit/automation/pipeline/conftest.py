"""Shared test fixtures for pipeline tests.

Provides FakeWorkerPool (synchronous inline executor) and FakeGitHub (dict-backed
GitHub state mutations) for deterministic testing without threads or real API calls.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any

import pytest

from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName


@dataclass
class FakeGitHub:
    """Mock GitHub state for deterministic testing.

    Tracks labels, PR facts, comments, reviews, and all mutations in
    append-only mutation_log.
    """

    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    prs: dict[int, dict[str, Any]] = field(default_factory=dict)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, list[dict[str, Any]]] = field(
        default_factory=lambda: {}  # issue -> comments
    )
    reviews: dict[int, list[dict[str, Any]]] = field(
        default_factory=lambda: {}  # PR -> reviews
    )
    mutation_log: list[tuple[str, Any]] = field(default_factory=list)

    def add_issue_label(self, issue_num: int, label: str) -> None:
        """Add a label to an issue."""
        if issue_num not in self.issues:
            self.issues[issue_num] = {}
        labels = self.issues[issue_num].setdefault("labels", [])
        if label not in labels:
            labels.append(label)
        self.mutation_log.append(("add_issue_label", {"issue": issue_num, "label": label}))

    def remove_issue_label(self, issue_num: int, label: str) -> None:
        """Remove a label from an issue."""
        if issue_num in self.issues:
            labels = self.issues[issue_num].get("labels", [])
            if label in labels:
                labels.remove(label)
        self.mutation_log.append(("remove_issue_label", {"issue": issue_num, "label": label}))

    def add_issue_comment(self, issue_num: int, comment_text: str) -> int:
        """Add a comment to an issue. Returns comment ID."""
        if issue_num not in self.comments:
            self.comments[issue_num] = []
        comment_id = len(self.comments[issue_num]) + 1
        self.comments[issue_num].append({"id": comment_id, "body": comment_text})
        self.mutation_log.append(
            ("add_issue_comment", {"issue": issue_num, "comment_id": comment_id})
        )
        return comment_id

    def update_issue_comment(self, comment_id: int, issue_num: int, comment_text: str) -> None:
        """Update an existing comment."""
        if issue_num in self.comments:
            for comment in self.comments[issue_num]:
                if comment["id"] == comment_id:
                    comment["body"] = comment_text
        self.mutation_log.append(
            ("update_issue_comment", {"issue": issue_num, "comment_id": comment_id})
        )

    def create_pr(self, pr_num: int, title: str, body: str) -> None:
        """Create a PR."""
        self.prs[pr_num] = {"number": pr_num, "title": title, "body": body, "state": "open"}
        self.mutation_log.append(("create_pr", {"pr": pr_num}))

    def update_pr(self, pr_num: int, title: str | None = None, body: str | None = None) -> None:
        """Update PR metadata."""
        if pr_num in self.prs:
            if title is not None:
                self.prs[pr_num]["title"] = title
            if body is not None:
                self.prs[pr_num]["body"] = body
        self.mutation_log.append(("update_pr", {"pr": pr_num}))

    def add_pr_review(self, pr_num: int, review_text: str) -> int:
        """Add a review to a PR. Returns review ID."""
        if pr_num not in self.reviews:
            self.reviews[pr_num] = []
        review_id = len(self.reviews[pr_num]) + 1
        self.reviews[pr_num].append({"id": review_id, "body": review_text, "state": "PENDING"})
        self.mutation_log.append(("add_pr_review", {"pr": pr_num, "review_id": review_id}))
        return review_id

    def submit_pr_review(self, pr_num: int, review_id: int, event: str = "APPROVE") -> None:
        """Submit a pending review."""
        if pr_num in self.reviews:
            for review in self.reviews[pr_num]:
                if review["id"] == review_id:
                    review["state"] = event
        self.mutation_log.append(
            ("submit_pr_review", {"pr": pr_num, "review_id": review_id, "event": event})
        )

    def create_label(self, name: str, description: str = "") -> None:
        """Create a label."""
        self.labels[name] = {"name": name, "description": description}
        self.mutation_log.append(("create_label", {"label": name}))

    def clear_mutations(self) -> None:
        """Clear the mutation log (for testing phases in isolation)."""
        self.mutation_log.clear()


class FakeWorkerPool:
    """Synchronous inline executor for deterministic testing.

    Submits are executed immediately on the calling thread, draining results
    to the completion queue without any threading or sleeps. No concurrency
    issues, deterministic ordering, easy to mock/spy.
    """

    def __init__(
        self,
        shutdown: object = None,  # unused, for compatibility
        completion_q: CompletionQueue | None = None,
    ) -> None:
        """Initialize.

        Args:
            shutdown: Unused (for API compatibility with WorkerPool).
            completion_q: Queue to drain results to (optional; if not provided,
                a fresh one is created).

        """
        self._completion_q = completion_q or queue.Queue()
        self._results: dict[JobHandle, JobResult] = {}

    @property
    def completion_q(self) -> CompletionQueue:
        """Access the completion queue."""
        return self._completion_q

    def submit(self, job: AgentJob | BuildTestJob | GitJob, on_done_state: StageName) -> JobHandle:
        """Submit a job for immediate synchronous execution.

        Args:
            job: Frozen job spec.
            on_done_state: Target stage on completion.

        Returns:
            JobHandle to track the job.

        """
        handle = JobHandle(job=job, on_done_state=on_done_state)
        # Execute immediately and synchronously
        result = self._execute_job(job)
        self._results[handle] = result
        self._completion_q.put((handle, result))
        return handle

    def _execute_job(self, job: AgentJob | BuildTestJob | GitJob) -> JobResult:
        """Execute a job and return its result. Subclasses can override."""
        # Default: return ok=True for all jobs (suitable for smoke tests)
        if isinstance(job, AgentJob):
            return JobResult(ok=True, value="fake agent output")
        elif isinstance(job, BuildTestJob):
            return JobResult(ok=True, stdout_tail="0")
        elif isinstance(job, GitJob):
            return JobResult(ok=True)
        else:
            return JobResult(ok=False, error="unknown job type")

    def shutdown(self) -> None:
        """Shutdown (no-op for fake pool)."""
        pass


@pytest.fixture
def fake_github() -> FakeGitHub:
    """Fresh FakeGitHub instance for each test."""
    return FakeGitHub()


@pytest.fixture
def fake_pool(completion_q: CompletionQueue) -> FakeWorkerPool:
    """Fresh FakeWorkerPool instance for each test, wired to the provided completion queue."""
    return FakeWorkerPool(completion_q=completion_q)


@pytest.fixture
def completion_q() -> CompletionQueue:
    """Fresh completion queue for tests."""
    return queue.Queue()
