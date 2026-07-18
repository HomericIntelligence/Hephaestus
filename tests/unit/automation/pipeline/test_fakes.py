"""Direct tests for the shared pipeline test doubles in conftest.py.

FakeWorkerPool must work for all three job types and honor scripted
results/exceptions; FakeGitHub must record every mutation in mutation_log.
"""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.pipeline.jobs import (
    AgentJob,
    BuildTestJob,
    GitJob,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName

from .conftest import FakeGitHub, FakeWorkerPool


def _jobs() -> tuple[AgentJob, BuildTestJob, GitJob]:
    """One job of each of the three types."""
    agent = AgentJob(
        repo="test/repo",
        issue=1,
        agent="claude",
        model="opus-4-8",
        prompt_builder=lambda: "prompt",
        cwd=Path("/tmp"),
        timeout_s=60,
    )
    build = BuildTestJob(repo="test/repo", cwd=Path("/tmp"), argv=("true",), timeout_s=60)
    git = GitJob(repo="test/repo", op="rebase", timeout_s=60)
    return agent, build, git


class TestFakeWorkerPool:
    """FakeWorkerPool completes inline for all three job types."""

    def test_matches_real_constructor_signature(self) -> None:
        """Positional (size, shutdown, completion_q) construction works."""
        import queue
        import threading

        q: CompletionQueue = queue.Queue()
        event = threading.Event()
        fake = FakeWorkerPool(2, event, q)
        assert fake.size == 2
        assert fake.shutdown_event is event
        assert fake.completion_q is q

    def test_all_three_job_types_complete_inline(self) -> None:
        """Every job type produces exactly one immediate ok completion."""
        fake = FakeWorkerPool()
        for job in _jobs():
            handle = fake.submit(job, StageName.PR_REVIEW)
            got_handle, result = fake.completion_q.get_nowait()
            assert got_handle is handle
            assert result.ok is True
        assert len(fake.submitted) == 3
        assert fake.completion_q.empty()

    def test_scripted_results_consumed_fifo(self) -> None:
        """script() outcomes are consumed in FIFO order across job types."""
        fake = FakeWorkerPool()
        agent, build, git = _jobs()
        fake.script(
            JobResult(ok=True, value="first"),
            JobResult(ok=False, error="second failed"),
        )

        fake.submit(agent, StageName.PLANNING)
        _, r1 = fake.completion_q.get_nowait()
        fake.submit(build, StageName.PR_REVIEW)
        _, r2 = fake.completion_q.get_nowait()
        fake.submit(git, StageName.MERGE_WAIT)
        _, r3 = fake.completion_q.get_nowait()

        assert r1.value == "first"
        assert r2.ok is False
        assert r2.error == "second failed"
        assert r3.ok is True  # FIFO exhausted -> default ok result

    def test_scripted_failure_for_each_job_type(self) -> None:
        """A scripted failing JobResult is delivered for every job type."""
        for job in _jobs():
            fake = FakeWorkerPool()
            fake.queue_result(JobResult(ok=False, error="scripted failure"))
            fake.submit(job, StageName.PR_REVIEW)
            _, result = fake.completion_q.get_nowait()
            assert result.ok is False
            assert result.error == "scripted failure"

    def test_scripted_exception_becomes_error_result(self) -> None:
        """queue_exception() delivers an error JobResult, mirroring the pool."""
        fake = FakeWorkerPool()
        fake.queue_exception(RuntimeError("kaboom"))
        agent, _, _ = _jobs()
        fake.submit(agent, StageName.PLANNING)
        _, result = fake.completion_q.get_nowait()
        assert result.ok is False
        assert result.error == "RuntimeError: kaboom"

    def test_handles_are_unique_per_submit(self) -> None:
        """Identical job specs still yield distinct, dict-keyable handles."""
        fake = FakeWorkerPool()
        _, _, git = _jobs()
        h1 = fake.submit(git, StageName.PR_REVIEW)
        h2 = fake.submit(git, StageName.PR_REVIEW)
        assert h1 is not h2
        assert h1 != h2  # identity equality (eq=False)
        assert len({h1: 1, h2: 2}) == 2

    def test_shutdown_sets_event(self) -> None:
        """shutdown() flips the shutdown event like the real pool."""
        fake = FakeWorkerPool()
        fake.shutdown()
        assert fake.shutdown_event.is_set()


class TestFakeGitHub:
    """FakeGitHub mirrors the real mutator surface and logs every mutation."""

    def test_label_and_comment_mutators_update_state_and_log(self, fake_github: FakeGitHub) -> None:
        """Label/comment mutators mutate dict state and append to the log."""
        fake_github.gh_create_label("state:needs-plan")
        fake_github.gh_issue_add_labels(7, ["state:needs-plan"])
        fake_github.gh_issue_comment(7, "planning started")
        fake_github.gh_issue_remove_labels(7, ["state:needs-plan"])
        fake_github.skip_epics({9: ["epic"]})

        assert fake_github.labels[7] == set()
        assert fake_github.labels[9] == {"state:skip"}
        assert fake_github.comments[7] == ["planning started"]
        assert [name for name, _ in fake_github.mutation_log] == [
            "gh_create_label",
            "gh_issue_add_labels",
            "gh_issue_comment",
            "gh_issue_remove_labels",
            "skip_epics",
        ]

    def test_pr_and_review_mutators(self, fake_github: FakeGitHub) -> None:
        """PR/review mutators return ids and record resolved threads."""
        pr = fake_github.gh_pr_create("7-auto", "title", "body\n\nCloses #7\n")
        threads = fake_github.gh_pr_review_post(
            pr, [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "nit"}], "summary"
        )
        assert len(threads) == 1
        fake_github.gh_pr_resolve_thread(threads[0], reply_body="done")
        fake_github.gh_pr_update_review_comment("comment-node", "edited")
        issue = fake_github.gh_issue_create("t", "b", labels=["bug"])
        comment_id = fake_github.gh_issue_upsert_comment(issue, "<!-- marker -->", "hello")
        fake_github.gh_issue_delete_comment(comment_id)

        assert pr in fake_github.prs
        assert threads[0] in fake_github.resolved_threads
        assert "bug" in fake_github.labels[issue]
        mutated = [name for name, _ in fake_github.mutation_log]
        assert mutated == [
            "gh_pr_create",
            "gh_pr_review_post",
            "gh_pr_resolve_thread",
            "gh_pr_update_review_comment",
            "gh_issue_create",
            "gh_issue_upsert_comment",
            "gh_issue_delete_comment",
        ]
