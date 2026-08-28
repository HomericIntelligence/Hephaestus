"""Contracts for the pipeline's closed GitHub worker boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    EnsureScopeExpansionChildrenRequest,
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
    RecoverReplyJournalRequest,
    ReplyHandoffAttempted,
    ReplyJournalAppended,
    ReplyJournalRecovered,
    RunMergeWaitCycleRequest,
    ScopeExpansionChildrenEnsured,
)
from hephaestus.automation.pipeline.reply_handoff import (
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
)
from hephaestus.automation.review_journal import IssueComment
from hephaestus.automation.scope_expansion_domain import ScopeExpansion


def test_frozen_json_is_detached_from_sources_and_thawed_values() -> None:
    """Mutable GitHub data cannot cross or mutate the worker boundary."""
    try:
        module = importlib.import_module("hephaestus.automation.pipeline.github_jobs")
    except ModuleNotFoundError:
        pytest.fail("the typed GitHub worker boundary does not exist", pytrace=False)

    frozen_json = module.FrozenJson
    source: list[dict[str, Any]] = [{"id": "thread-1", "comments": [{"body": "original"}]}]

    snapshot = frozen_json.snapshot(source)
    source[0]["comments"][0]["body"] = "source changed"
    first_thaw = cast(list[dict[str, Any]], snapshot.thaw())
    first_thaw[0]["comments"][0]["body"] = "thaw changed"

    assert snapshot.thaw() == [{"comments": [{"body": "original"}], "id": "thread-1"}]


def test_runner_dispatches_append_with_a_fresh_accessor_per_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker requests never share the coordinator or another job's client."""
    try:
        module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    except ModuleNotFoundError:
        pytest.fail("the production typed GitHub runner does not exist", pytrace=False)

    clients: list[object] = []
    appends: list[tuple[int, str, str]] = []

    class FakePipelineGitHub:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            clients.append(self)

        def append_issue_comment(self, issue: int, marker: str, body: str) -> None:
            appends.append((issue, marker, body))

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(
        issue_number=3,
        marker=marker,
        body=f'{marker}\n<!-- {{"format":1}} -->',
    )
    job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=request,
        descr="append journal",
    )
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)

    first = runner.run(job)
    second = runner.run(job)

    assert first == ReplyJournalAppended(request=request)
    assert second == ReplyJournalAppended(request=request)
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert appends == [(3, marker, request.body), (3, marker, request.body)]


def test_runner_recovers_version_one_journal_and_delivers_exact_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery keeps the v1 payload and delivery's exact head/nonce/subset."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    threads = [{"id": "thread-1", "comments": [{"id": "comment-1", "body": "fix it"}]}]
    handoff = implementation_reply_handoff(
        "a" * 40,
        threads,
        {"thread-1": "fixed"},
        "b" * 32,
    )
    assert handoff is not None
    journal = implementation_reply_handoff_journal_entry(7, handoff)
    assert journal is not None
    _marker, journal_body = journal
    delivery_calls: list[tuple[object, ...]] = []

    class FakePipelineGitHub:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def issue_comments(self, issue: int) -> list[IssueComment]:
            assert issue == 7
            return [IssueComment(body=journal_body, viewer_did_author=True)]

        def gh_pr_state(self, pr: int) -> dict[str, object]:
            assert pr == 7
            return {"state": "OPEN", "autoMergeRequest": None, "headRefOid": "a" * 40}

        def post_implementation_thread_replies(
            self,
            pr: int,
            *,
            expected_head_sha: str,
            threads: list[dict[str, object]],
            replies: dict[str, str],
            batch_nonce: str,
        ) -> object:
            delivery_calls.append((pr, expected_head_sha, threads, replies, batch_nonce))
            return SimpleNamespace(
                replied_thread_ids=("thread-1",),
                receipts=({"id": "thread-1"},),
                retryable_thread_ids=(),
                retryable=False,
                visibility_lag=False,
            )

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    recovery_request = RecoverReplyJournalRequest(
        issue_number=7,
        pr_number=7,
        threads=FrozenJson.snapshot(threads),
    )
    recovery_job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=recovery_request,
        descr="recover journal",
    )

    recovered = runner.run(recovery_job)
    recovered_handoff = dict(handoff)
    recovered_handoff["reconciliation_only"] = True
    assert recovered == ReplyJournalRecovered(
        request=recovery_request,
        handoff=FrozenJson.snapshot(recovered_handoff),
    )
    assert recovered.handoff is not None
    delivery_request = DeliverReplyHandoffRequest(
        issue_number=3,
        pr_number=7,
        handoff=recovered.handoff,
        visibility_retries=0,
    )
    delivered = runner.run(
        GitHubJob(
            repo="example",
            repo_root=tmp_path.resolve(),
            request=delivery_request,
            descr="deliver replies",
        )
    )

    assert delivered == ReplyHandoffAttempted(
        request=delivery_request,
        status="blocked",
        remaining_handoff=None,
        visibility_retries=0,
        retry_delay_s=None,
    )
    assert delivery_calls == []


def test_runner_ensures_scope_expansion_children_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Child issues are created once and then recovered by marker on retry."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    expansion = ScopeExpansion(
        title="Extract shared helper",
        reason="Prerequisite work must ship first",
        source_path="hephaestus/automation/example.py",
        source_line=17,
        required_paths=(
            "hephaestus/automation/example.py",
            "tests/unit/automation/test_example.py",
        ),
        acceptance_criteria=("Helper exists", "Tests pass"),
    )
    events: list[tuple[str, tuple[object, ...]]] = []
    shared_state = {"issues": {}, "next_issue": 900}

    class FakePipelineGitHub:
        def __init__(
            self, *_args: object, repo: str | None = None, dry_run: bool = False, **_kwargs: object
        ) -> None:
            self.repo = repo or "org/repo"
            self.dry_run = dry_run

        def issue_with_marker(self, marker: str) -> dict[str, object] | None:
            matches = [
                {"number": issue_number, **issue}
                for issue_number, issue in shared_state["issues"].items()
                if str(issue.get("body") or "").startswith(marker)
            ]
            if not matches:
                return None
            return matches[0]

        def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
            del labels
            if self.dry_run:
                return 0
            shared_state["next_issue"] += 1
            issue_number = shared_state["next_issue"]
            shared_state["issues"][issue_number] = {"title": title, "body": body, "state": "OPEN"}
            events.append(("create_issue", (issue_number, title)))
            return issue_number

        def gh_issue_json(self, issue_number: int) -> dict[str, object]:
            issue = shared_state["issues"].get(
                issue_number, {"title": "child", "body": "", "state": "OPEN"}
            )
            return {
                "number": issue_number,
                "title": issue.get("title", "child"),
                "body": issue.get("body", ""),
                "state": issue.get("state", "OPEN"),
            }

        def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
            del issue_number
            return None

        def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
            if self.dry_run:
                return
            events.append(("upsert_issue_comment", (issue_number, marker, body)))

        def post_scope_expansion_blocking_review(
            self,
            pr_number: int,
            *,
            body: str,
            marker: str,
        ) -> str:
            if self.dry_run:
                return "review-dry-run"
            events.append(("post_scope_expansion_blocking_review", (pr_number, marker, body)))
            return f"review-{pr_number}"

        def mark_pr_implementation_no_go(self, pr_number: int) -> None:
            if self.dry_run:
                return
            events.append(("mark_pr_implementation_no_go", (pr_number,)))

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    request = EnsureScopeExpansionChildrenRequest(
        issue_number=17,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        scope_expansions=(expansion,),
    )
    job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=request,
        descr="ensure scope expansion children",
    )

    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    first = runner.run(job)
    second = runner.run(job)

    assert first == ScopeExpansionChildrenEnsured(
        request=request,
        status="blocked",
        child_issue_numbers=(901,),
    )
    assert second == ScopeExpansionChildrenEnsured(
        request=request,
        status="blocked",
        child_issue_numbers=(901,),
    )
    assert [event for event, _ in events].count("create_issue") == 1
    assert any(event == "post_scope_expansion_blocking_review" for event, _ in events)
    assert any(event == "mark_pr_implementation_no_go" for event, _ in events)


def test_runner_dry_run_reports_scope_expansion_split_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run reports the proposed split without creating GitHub objects."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    expansion = ScopeExpansion(
        title="Extract shared helper",
        reason="Prerequisite work must ship first",
        source_path="hephaestus/automation/example.py",
        source_line=17,
        required_paths=(
            "hephaestus/automation/example.py",
            "tests/unit/automation/test_example.py",
        ),
        acceptance_criteria=("Helper exists", "Tests pass"),
    )
    events: list[tuple[str, tuple[object, ...]]] = []

    class FakePipelineGitHub:
        def __init__(
            self, *_args: object, repo: str | None = None, dry_run: bool = False, **_kwargs: object
        ) -> None:
            self.repo = repo or "org/repo"
            self.dry_run = dry_run

        def issue_with_marker(self, marker: str) -> dict[str, object] | None:
            del marker
            return None

        def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
            del title, body, labels
            return 0

        def gh_issue_json(self, issue_number: int) -> dict[str, object]:
            del issue_number
            return {"number": 901, "title": "child", "body": "", "state": "OPEN"}

        def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
            del issue_number
            return None

        def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
            if self.dry_run:
                return
            events.append(("upsert_issue_comment", (issue_number, marker, body)))

        def post_scope_expansion_blocking_review(
            self,
            pr_number: int,
            *,
            body: str,
            marker: str,
        ) -> str:
            if self.dry_run:
                return "review-dry-run"
            events.append(("post_scope_expansion_blocking_review", (pr_number, marker, body)))
            return "review-1"

        def mark_pr_implementation_no_go(self, pr_number: int) -> None:
            if self.dry_run:
                return
            events.append(("mark_pr_implementation_no_go", (pr_number,)))

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    request = EnsureScopeExpansionChildrenRequest(
        issue_number=17,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        scope_expansions=(expansion,),
    )
    job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=request,
        descr="ensure scope expansion children",
    )

    receipt = module.PipelineGitHubJobRunner(org="example-org", dry_run=True).run(job)

    assert receipt == ScopeExpansionChildrenEnsured(
        request=request,
        status="dry_run",
        child_issue_numbers=(),
    )
    assert events == []


def test_pr_reconciliation_reads_back_late_threads_before_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A late thread is returned to the stage and therefore cannot permit GO."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    finding = {
        "path": "a.py",
        "line": 3,
        "side": "RIGHT",
        "severity": "major",
        "body": "guard None",
    }
    posted = {
        **finding,
        "id": "posted-thread",
        "comments": [{"id": "posted-comment", "body": "guard None"}],
    }
    late = {
        "id": "late-thread",
        "path": "b.py",
        "line": 4,
        "side": "RIGHT",
        "severity": "major",
        "body": "late race",
        "comments": [{"id": "late-comment", "body": "late race"}],
    }

    class FakePipelineGitHub:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.reads = 0

        def list_unresolved_review_threads(self, _pr: int) -> list[dict[str, object]]:
            self.reads += 1
            return [] if self.reads < 3 else [posted, late]

        def reviewer_validation_receipts(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

        def pr_review_context(self, _pr: int) -> dict[str, str]:
            return {
                "pr_head_sha": "a" * 40,
                "pr_title": "fix: example",
                "pr_description": "body",
            }

        def post_review_threads(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [posted]

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    request = ReconcilePrReviewRequest(
        pr_number=7,
        reviewed_head_sha="a" * 40,
        validated_receipt_fingerprints=None,
        validated_metadata_fingerprint=None,
        resolved_thread_ids=(),
        feedback=FrozenJson.snapshot({}),
        findings=FrozenJson.snapshot([finding]),
        review_diff="diff",
    )
    job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=request,
        descr="reconcile review",
    )

    receipt = module.PipelineGitHubJobRunner("org", False).run(job)

    assert isinstance(receipt, PrReviewReconciled)
    assert receipt.action == "apply"
    unresolved = cast(list[dict[str, object]], receipt.unresolved_threads.thaw())
    remediation = cast(list[dict[str, object]], receipt.remediation_threads.thaw())
    assert [thread["id"] for thread in unresolved] == [
        "posted-thread",
        "late-thread",
    ]
    assert len(remediation) == 2


def test_runner_dispatches_merge_cycle_as_a_typed_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The fifth closed operation is dispatched without a callable escape hatch."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    state_reads: list[int] = []

    class FakePipelineGitHub:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def gh_pr_state(self, pr: int) -> dict[str, object]:
            state_reads.append(pr)
            return {"state": "MERGED", "mergedAt": "2026-08-04T00:00:00Z"}

    monkeypatch.setattr(module, "PipelineGitHub", FakePipelineGitHub)
    request = RunMergeWaitCycleRequest(
        pr_number=7,
        reviewed_head_sha="a" * 40,
        proof_generation=2,
        declined_readiness_fingerprint=None,
    )

    receipt = module.PipelineGitHubJobRunner("org", False).run(
        GitHubJob(
            repo="example",
            repo_root=tmp_path.resolve(),
            request=request,
            descr="merge wait cycle",
        )
    )

    assert receipt == MergeWaitCycleCompleted(
        request=request,
        outcome="merged",
        attempted=False,
    )
    assert state_reads == [7]
