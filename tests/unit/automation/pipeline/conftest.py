"""Shared test doubles for pipeline stage/coordinator tests.

FakeWorkerPool runs jobs synchronously and inline (no threads, no sleeps) so
stage/coordinator tests are deterministic. FakeGitHub is a dict-backed stand-in
for github_api mutators with an append-only mutation_log; its method names and
signatures mirror the real ``hephaestus.automation.github_api`` mutator surface
so later coordinator tests can swap it in without renaming call sites.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from pathlib import Path
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


class FakeWorkerPool:
    """Synchronous inline stand-in for :class:`WorkerPool`.

    Matches the real constructor signature ``(size, shutdown, completion_q)``
    (all defaulted so tests can construct it bare). ``submit`` completes the
    job immediately on the calling thread and puts ``(handle, result)`` on the
    completion queue — no threads, no sleeps, deterministic ordering.

    Scriptable outcomes (FIFO): :meth:`script` / :meth:`queue_result` /
    :meth:`queue_exception` enqueue outcomes consumed one per ``submit`` in
    order. A scripted ``Exception`` is converted to an error ``JobResult``
    (mirroring how the real pool isolates job failures). When the FIFO is
    empty, a per-job-type ok result is synthesized.
    """

    def __init__(
        self,
        size: int = 1,
        shutdown: threading.Event | None = None,
        completion_q: CompletionQueue | None = None,
        lock_dir: Path | None = None,
    ) -> None:
        """Initialize the fake pool.

        Args:
            size: Accepted for signature parity with WorkerPool; unused.
            shutdown: Accepted for signature parity with WorkerPool; unused.
            completion_q: Queue to drain results to (created if omitted).
            lock_dir: Accepted for signature parity with WorkerPool; unused.

        """
        self.size = size
        self.shutdown_event = shutdown if shutdown is not None else threading.Event()
        self.completion_q: CompletionQueue = (
            completion_q if completion_q is not None else (queue.Queue())
        )
        self.submitted: list[JobHandle] = []
        self._scripted: deque[JobResult | Exception] = deque()

    def script(self, *outcomes: JobResult | Exception) -> None:
        """FIFO-enqueue scripted outcomes for subsequent :meth:`submit` calls."""
        self._scripted.extend(outcomes)

    def queue_result(self, result: JobResult) -> None:
        """FIFO-enqueue a single scripted result."""
        self._scripted.append(result)

    def queue_exception(self, exc: Exception) -> None:
        """FIFO-enqueue an exception, delivered as an error JobResult."""
        self._scripted.append(exc)

    def submit(self, job: AgentJob | BuildTestJob | GitJob, on_done_state: StageName) -> JobHandle:
        """Execute *job* inline and put its completion on the queue.

        Args:
            job: Frozen job spec (any of the three job types).
            on_done_state: Target stage on completion.

        Returns:
            The JobHandle also recorded in :attr:`submitted`.

        """
        handle = JobHandle(job=job, on_done_state=on_done_state)
        self.submitted.append(handle)
        outcome: JobResult | Exception = (
            self._scripted.popleft() if self._scripted else self._default_result(job)
        )
        if isinstance(outcome, Exception):
            outcome = JobResult(
                ok=False,
                error=f"{type(outcome).__name__}: {outcome!s}",
            )
        self.completion_q.put((handle, outcome))
        return handle

    @staticmethod
    def _default_result(job: AgentJob | BuildTestJob | GitJob) -> JobResult:
        """Synthesize a per-job-type ok result when nothing is scripted."""
        if isinstance(job, AgentJob):
            return JobResult(ok=True, value="fake agent output")
        if isinstance(job, BuildTestJob):
            return JobResult(ok=True, value=0)
        return JobResult(ok=True)

    def shutdown(self) -> None:
        """Match the real pool's API (sets the shutdown event; nothing to cancel)."""
        self.shutdown_event.set()


class FakeGitHub:
    """Dict-backed GitHub state with an append-only ``mutation_log``.

    Method names and signatures mirror the 12 real
    ``hephaestus.automation.github_api`` mutators the pipeline stages call
    (``gh_issue_add_labels``, ``gh_issue_remove_labels``, ``gh_create_label``,
    ``skip_epics``, ``gh_issue_comment``, ``gh_issue_upsert_comment``,
    ``gh_issue_create``, ``gh_issue_delete_comment``, ``gh_pr_create``,
    ``gh_pr_review_post``, ``gh_pr_update_review_comment``,
    ``gh_pr_resolve_thread``), so coordinator tests can inject this double
    without adapting call sites.
    """

    def __init__(self) -> None:
        """Initialize empty GitHub state."""
        self.labels: dict[int, set[str]] = {}
        self.defined_labels: dict[str, dict[str, str]] = {}
        self.comments: dict[int, list[str]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.prs: dict[int, dict[str, Any]] = {}
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.resolved_threads: set[str] = set()
        self.mutation_log: list[tuple[str, tuple[Any, ...]]] = []
        self._next_issue = 900
        self._next_pr = 1000

    def _log(self, name: str, *args: Any) -> None:
        self.mutation_log.append((name, args))

    # -- label mutators ----------------------------------------------------
    def gh_create_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        """Mirror github_api.gh_create_label."""
        self.defined_labels[name] = {"color": color, "description": description}
        self._log("gh_create_label", name)

    def gh_issue_add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Mirror github_api.gh_issue_add_labels."""
        self.labels.setdefault(issue_number, set()).update(labels)
        self._log("gh_issue_add_labels", issue_number, tuple(labels))

    def gh_issue_remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Mirror github_api.gh_issue_remove_labels."""
        self.labels.setdefault(issue_number, set()).difference_update(labels)
        self._log("gh_issue_remove_labels", issue_number, tuple(labels))

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Mirror github_api.skip_epics (tags each epic state:skip)."""
        for issue_number in epics_labels:
            self.labels.setdefault(issue_number, set()).add("state:skip")
        self._log("skip_epics", tuple(sorted(epics_labels)))

    # -- issue/comment mutators --------------------------------------------
    def gh_issue_comment(self, issue_number: int, body: str) -> None:
        """Mirror github_api.gh_issue_comment."""
        self.comments.setdefault(issue_number, []).append(body)
        self._log("gh_issue_comment", issue_number)

    def gh_issue_upsert_comment(self, issue_number: int, marker_prefix: str, body: str) -> int:
        """Mirror github_api.gh_issue_upsert_comment (returns a comment id)."""
        self.comments.setdefault(issue_number, []).append(body)
        self._log("gh_issue_upsert_comment", issue_number, marker_prefix)
        return len(self.comments[issue_number])

    def gh_issue_create(self, title: str, body: str, labels: list[str] | None = None) -> int:
        """Mirror github_api.gh_issue_create (returns the new issue number)."""
        self._next_issue += 1
        self.issues[self._next_issue] = {"title": title, "body": body}
        if labels:
            self.labels.setdefault(self._next_issue, set()).update(labels)
        self._log("gh_issue_create", self._next_issue)
        return self._next_issue

    def gh_issue_delete_comment(self, comment_id: int) -> None:
        """Mirror github_api.gh_issue_delete_comment."""
        self._log("gh_issue_delete_comment", comment_id)

    # -- PR + review mutators ------------------------------------------------
    def gh_pr_create(
        self,
        branch: str,
        title: str,
        body: str,
        auto_merge: bool = False,
        base: str = "main",
    ) -> int:
        """Mirror github_api.gh_pr_create (returns the new PR number)."""
        self._next_pr += 1
        self.prs[self._next_pr] = {
            "branch": branch,
            "title": title,
            "body": body,
            "auto_merge": auto_merge,
            "base": base,
        }
        self._log("gh_pr_create", self._next_pr, branch)
        return self._next_pr

    def gh_pr_review_post(
        self,
        pr_number: int,
        comments: list[dict[str, Any]],
        summary: str,
        event: str = "COMMENT",
        dry_run: bool = False,
        dedupe_existing: bool = False,
    ) -> list[str]:
        """Mirror github_api.gh_pr_review_post (returns fake thread ids)."""
        del dedupe_existing  # signature parity only
        if dry_run:
            return []
        review = {"comments": comments, "summary": summary, "event": event}
        self.reviews.setdefault(pr_number, []).append(review)
        self._log("gh_pr_review_post", pr_number, event)
        return [f"thread-{pr_number}-{i}" for i in range(len(comments))]

    def gh_pr_update_review_comment(self, comment_node_id: str, body: str) -> None:
        """Mirror github_api.gh_pr_update_review_comment."""
        del body  # state not modelled; the log entry is the observable
        self._log("gh_pr_update_review_comment", comment_node_id)

    def gh_pr_resolve_thread(
        self,
        thread_id: str,
        reply_body: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """Mirror github_api.gh_pr_resolve_thread."""
        if dry_run:
            return
        self.resolved_threads.add(thread_id)
        self._log("gh_pr_resolve_thread", thread_id, reply_body)


@pytest.fixture
def fake_github() -> FakeGitHub:
    """Fresh FakeGitHub instance for each test."""
    return FakeGitHub()


@pytest.fixture
def completion_q() -> CompletionQueue:
    """Fresh completion queue for tests."""
    return queue.Queue()


@pytest.fixture
def fake_pool(completion_q: CompletionQueue) -> FakeWorkerPool:
    """Fresh FakeWorkerPool wired to the shared completion queue."""
    return FakeWorkerPool(completion_q=completion_q)
