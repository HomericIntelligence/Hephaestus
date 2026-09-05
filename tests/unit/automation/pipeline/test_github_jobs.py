"""Contracts for the pipeline's closed GitHub worker boundary."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    EnsureScopeExpansionChildrenRequest,
    FrozenJson,
    GitHubJob,
    MergeWaitCycleCompleted,
    PrReviewReconciled,
    ReconcilePrReviewRequest,
    ReconcileScopeExpansionDependenciesRequest,
    RecoverReplyJournalRequest,
    ReplyHandoffAttempted,
    ReplyJournalAppended,
    ReplyJournalRecovered,
    RunMergeWaitCycleRequest,
    ScopeExpansionChildrenEnsured,
    ScopeExpansionDependenciesReconciled,
)
from hephaestus.automation.pipeline.reply_handoff import (
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
)
from hephaestus.automation.pipeline.scope_expansion_records import (
    parse_scope_expansion_lifecycle_comment,
    render_scope_expansion_child_body,
    render_scope_expansion_lifecycle_comment,
)
from hephaestus.automation.review_journal import IssueComment
from hephaestus.automation.scope_expansion_domain import ScopeExpansion


class _ScopeExpansionFakeGitHub:
    """Small mutable GitHub double for child-issue idempotency tests."""

    issues: ClassVar[dict[int, dict[str, object]]] = {}
    events: ClassVar[list[tuple[str, tuple[object, ...]]]] = []
    next_issue_number: ClassVar[int] = 900
    implementation_no_go: ClassVar[bool] = False
    comments: ClassVar[dict[int, list[IssueComment]]] = {}
    blocking_reviews: ClassVar[set[str]] = set()

    @classmethod
    def reset(cls, events: list[tuple[str, tuple[object, ...]]]) -> None:
        """Reset the class-owned fake repository for one test."""
        cls.issues = {}
        cls.events = events
        cls.next_issue_number = 900
        cls.implementation_no_go = False
        cls.comments = {}
        cls.blocking_reviews = set()

    def __init__(
        self, *_args: object, repo: str | None = None, dry_run: bool = False, **_kwargs: object
    ) -> None:
        self.repo = repo or "org/repo"
        self.dry_run = dry_run

    def gh_pr_state(self, pr_number: int) -> dict[str, object]:
        """Return one exact open and unarmed source PR."""
        return {
            "number": pr_number,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "autoMergeRequest": None,
        }

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return the fake's exclusive implementation state."""
        del pr_number
        return False, type(self).implementation_no_go

    def issue_with_marker(self, marker: str) -> dict[str, object] | None:
        matches = [
            {"number": issue_number, **issue}
            for issue_number, issue in self.issues.items()
            if str(issue.get("body") or "").startswith(marker)
        ]
        return matches[0] if matches else None

    def issues_with_marker(self, marker: str) -> list[dict[str, object]]:
        """Return all issues with the exact leading child marker."""
        return [
            {"number": issue_number, **issue}
            for issue_number, issue in self.issues.items()
            if str(issue.get("body") or "").startswith(marker)
        ]

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return the actor-owned comments for one issue or pull request."""
        return list(self.comments.get(issue_number, ()))

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        del labels
        if self.dry_run:
            return 0
        type(self).next_issue_number += 1
        issue_number = type(self).next_issue_number
        self.issues[issue_number] = {"title": title, "body": body, "state": "OPEN"}
        self.events.append(("create_issue", (issue_number, title)))
        return issue_number

    def gh_issue_json(self, issue_number: int) -> dict[str, object]:
        issue = self.issues.get(issue_number, {"title": "child", "body": "", "state": "OPEN"})
        return {"number": issue_number, **issue}

    @staticmethod
    def find_merged_pr_for_issue(issue_number: int) -> int | None:
        del issue_number
        return None

    @staticmethod
    def merged_scope_expansion_pr(
        issue_number: int, *, source_pr_number: int | None = None
    ) -> dict[str, str] | None:
        del issue_number, source_pr_number
        return None

    @staticmethod
    def commit_is_ancestor(ancestor: str, descendant: str) -> bool:
        del ancestor, descendant
        return False

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        if not self.dry_run:
            retained = [
                comment
                for comment in self.comments.get(issue_number, ())
                if not comment.body.startswith(marker)
            ]
            self.comments[issue_number] = [
                *retained,
                IssueComment(body=body, viewer_did_author=True),
            ]
            self.events.append(("upsert_issue_comment", (issue_number, marker, body)))

    def post_scope_expansion_blocking_review(
        self, pr_number: int, *, body: str, marker: str
    ) -> str:
        if self.dry_run:
            return "review-dry-run"
        if marker in self.blocking_reviews:
            return f"review-{pr_number}"
        self.blocking_reviews.add(marker)
        self.events.append(("post_scope_expansion_blocking_review", (pr_number, marker, body)))
        return f"review-{pr_number}"

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        if not self.dry_run:
            type(self).implementation_no_go = True
            self.events.append(("mark_pr_implementation_no_go", (pr_number,)))


def _dependency_fake(  # noqa: C901
    *,
    child_state: str,
    merged: bool,
    in_source: bool,
    pending_retraction: bool = False,
    unbound_projection: bool = False,
    recover_projection: bool = False,
    lifecycle_state: str = "blocked",
    lifecycle_head: str = "a" * 40,
    lifecycle_merge_sha: str | None = None,
) -> object:
    """Build one complete lifecycle double for dependency classification."""
    expansion = ScopeExpansion(
        title="Extract helper",
        reason="The helper must merge first",
        source_path="hephaestus/example.py",
        source_line=4,
        required_paths=("hephaestus/example.py",),
        acceptance_criteria=("The helper exists",),
    )
    child_body = render_scope_expansion_child_body(
        repository="org/repo",
        parent_issue=7,
        pr_number=11,
        reviewed_head_sha=lifecycle_head,
        expansion=expansion,
    )
    projected_finding = {
        "path": "extra.py",
        "line": 1,
        "side": "RIGHT",
        "body": (
            "<!-- hephaestus-severity: major -->\n"
            '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
            "Remove the out-of-scope file."
        ),
    }
    lifecycle = render_scope_expansion_lifecycle_comment(
        repository="org/repo",
        parent_issue=7,
        pr_number=11,
        reviewed_head_sha=lifecycle_head,
        expansion=expansion,
        state=cast(Any, lifecycle_state),
        child_issue_number=None if lifecycle_state == "pending-child" else 901,
        merge_sha=lifecycle_merge_sha,
        retraction_findings=(projected_finding,)
        if unbound_projection or recover_projection
        else (),
        review_diff="diff --git a/extra.py b/extra.py\n"
        if unbound_projection or recover_projection
        else "",
    )

    class Fake:
        _repo_slug = "org/repo"
        dry_run = False
        posted_projection = False

        @staticmethod
        def mark_pr_implementation_no_go(_pr: int) -> None:
            return None

        @staticmethod
        def gh_pr_state(_pr: int) -> dict[str, object]:
            return {
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "baseRefName": "main",
                "autoMergeRequest": None,
            }

        @staticmethod
        def issue_comments(_issue: int) -> list[IssueComment]:
            return [IssueComment(body=lifecycle, viewer_did_author=True)]

        @staticmethod
        def pr_has_implementation_state_label(_pr: int) -> tuple[bool, bool]:
            return False, True

        def list_unresolved_review_threads(self, _pr: int) -> list[dict[str, object]]:
            if unbound_projection:
                raise AssertionError("projection was read before child identity")
            if not pending_retraction and not self.posted_projection:
                return []
            return [
                {
                    "id": "thread-1",
                    "path": "extra.py",
                    "line": 1,
                    "side": "RIGHT",
                    "body": (
                        "<!-- hephaestus-severity: major -->\n"
                        '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
                        "Remove the out-of-scope file."
                    ),
                    "isResolved": False,
                    "pr_state": {"state": "OPEN", "headRefOid": "a" * 40},
                    "comments": [
                        {
                            "id": "comment-1",
                            "body": "Remove it.",
                            "viewer_did_author": True,
                            "review_id": "review-1",
                            "review_state": "COMMENTED",
                            "review_commit_sha": "a" * 40,
                        }
                    ],
                }
            ]

        def post_review_threads(
            self,
            _pr: int,
            findings: list[dict[str, object]],
            *,
            expected_head_sha: str,
            review_diff: str,
        ) -> list[dict[str, object]]:
            assert recover_projection
            assert findings == [projected_finding]
            assert expected_head_sha == "a" * 40
            assert review_diff
            self.posted_projection = True
            return [{"id": "thread-1"}]

        @staticmethod
        def issues_with_marker(_marker: str) -> list[dict[str, object]]:
            return []

        @staticmethod
        def reviewer_validation_receipts(
            _pr: int, *, reviewed_head_sha: str, threads: list[dict[str, object]]
        ) -> list[dict[str, object]]:
            assert reviewed_head_sha == "a" * 40
            assert bool(threads) is (pending_retraction or recover_projection)
            return []

        @staticmethod
        def gh_issue_json(_issue: int) -> dict[str, object]:
            return {"state": child_state, "body": child_body}

        @staticmethod
        def find_merged_pr_for_issue(_issue: int) -> int | None:
            return 44 if merged else None

        @staticmethod
        def merged_scope_expansion_pr(
            _pr: int, *, source_pr_number: int | None = None
        ) -> dict[str, str] | None:
            assert source_pr_number == 11
            return {"merge_sha": "b" * 40, "base_branch": "main"} if merged else None

        @staticmethod
        def commit_is_ancestor(ancestor: str, descendant: str) -> bool:
            assert ancestor == "b" * 40
            return descendant == "main" or in_source

        @staticmethod
        def upsert_issue_comment(_issue: int, _marker: str, _body: str) -> None:
            return None

    return Fake()


@pytest.mark.parametrize(
    ("child_state", "merged", "in_source", "expected"),
    [
        ("OPEN", False, False, "parked"),
        ("CLOSED", False, False, "operator_required"),
        ("CLOSED", True, False, "sync_required"),
        ("CLOSED", True, True, "fresh_review"),
    ],
)
def test_scope_dependency_lifecycle_routes_before_review(
    child_state: str, merged: bool, in_source: bool, expected: str
) -> None:
    """Open, abandoned, merged, and synchronized children have distinct routes."""
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )

    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request,
        _dependency_fake(
            child_state=child_state,
            merged=merged,
            in_source=in_source,
        ),
    )

    assert isinstance(receipt, ScopeExpansionDependenciesReconciled)
    assert receipt.status == expected
    assert receipt.child_issue_numbers == (901,)


def test_scope_dependency_recovers_only_durable_retraction_work() -> None:
    """A restart routes a durable retraction thread before it parks the child."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )

    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request,
        _dependency_fake(
            child_state="OPEN",
            merged=False,
            in_source=False,
            pending_retraction=True,
        ),
    )

    assert receipt.status == "retraction_required"
    assert receipt.retraction_threads.thaw() == [
        {
            "body": (
                "<!-- hephaestus-severity: major -->\n"
                '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
                "Remove the out-of-scope file."
            ),
            "line": 1,
            "path": "extra.py",
            "thread_id": "thread-1",
        }
    ]


def test_dependency_recovery_binds_child_identity_before_projection() -> None:
    """An unbound intent fails closed before it reads or posts retraction work."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )

    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request,
        _dependency_fake(
            child_state="OPEN",
            merged=False,
            in_source=False,
            unbound_projection=True,
            lifecycle_state="pending-child",
        ),
    )

    assert receipt.status == "operator_required"
    assert receipt.child_issue_numbers == ()


def test_incomplete_dependency_transaction_rejects_source_head_drift() -> None:
    """A pending review cannot resume after an unrecorded source-head change."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )

    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request,
        _dependency_fake(
            child_state="OPEN",
            merged=False,
            in_source=False,
            lifecycle_state="pending-review",
            lifecycle_head="b" * 40,
        ),
    )

    assert receipt.status == "operator_required"
    assert receipt.child_issue_numbers == (901,)


def test_restart_restores_projected_retraction_before_parking() -> None:
    """A crash before thread publication restores only the durable retraction."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )
    github = _dependency_fake(
        child_state="OPEN",
        merged=False,
        in_source=False,
        recover_projection=True,
    )

    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request, github
    )

    assert receipt.status == "retraction_required"
    assert cast(Any, github).posted_projection is True
    assert len(cast(list[object], receipt.retraction_threads.thaw())) == 1


def test_synchronized_merged_dependency_allows_reviewed_head_drift() -> None:
    """A completed merge record permits the post-sync head and requests fresh review."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    request = ReconcileScopeExpansionDependenciesRequest(
        issue_number=7,
        pr_number=11,
        source_head_sha="a" * 40,
    )

    receipt = module.PipelineGitHubJobRunner._reconcile_scope_expansion_dependencies(
        request,
        _dependency_fake(
            child_state="CLOSED",
            merged=True,
            in_source=True,
            lifecycle_state="pending-review",
            lifecycle_head="c" * 40,
            lifecycle_merge_sha="b" * 40,
        ),
    )

    assert receipt.status == "fresh_review"
    assert receipt.merge_shas == ("b" * 40,)


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


def test_scope_request_accepts_a_nonempty_valid_json_projection() -> None:
    """The closed request uses the decoded JSON root for pair validation."""
    finding = {
        "path": "extra.py",
        "line": 1,
        "side": "RIGHT",
        "body": (
            "<!-- hephaestus-severity: major -->\n"
            '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
            "Remove the out-of-scope file."
        ),
    }
    request = EnsureScopeExpansionChildrenRequest(
        issue_number=7,
        pr_number=11,
        reviewed_head_sha="a" * 40,
        scope_expansions=(
            ScopeExpansion(
                title="Extract helper",
                reason="The helper must merge first",
                source_path="hephaestus/example.py",
                source_line=4,
                required_paths=("hephaestus/example.py",),
                acceptance_criteria=("The helper exists",),
            ),
        ),
        retraction_findings=FrozenJson.snapshot([finding]),
        review_diff="diff --git a/extra.py b/extra.py\n",
    )

    assert request.retraction_findings.thaw() == [finding]


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
    _ScopeExpansionFakeGitHub.reset(events)
    monkeypatch.setattr(module, "PipelineGitHub", _ScopeExpansionFakeGitHub)
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
    assert [event for event, _ in events].index("mark_pr_implementation_no_go") < [
        event for event, _ in events
    ].index("create_issue")
    assert any(event == "post_scope_expansion_blocking_review" for event, _ in events)
    assert any(event == "mark_pr_implementation_no_go" for event, _ in events)


def _child_request() -> EnsureScopeExpansionChildrenRequest:
    """Return one canonical child ensure request for crash recovery tests."""
    return EnsureScopeExpansionChildrenRequest(
        issue_number=17,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        scope_expansions=(
            ScopeExpansion(
                title="Extract shared helper",
                reason="Prerequisite work must ship first",
                source_path="hephaestus/automation/example.py",
                source_line=17,
                required_paths=("hephaestus/automation/example.py",),
                acceptance_criteria=("Helper exists",),
            ),
        ),
    )


def _scope_job(request: EnsureScopeExpansionChildrenRequest, root: Path) -> GitHubJob:
    """Wrap one child request in its closed worker job."""
    return GitHubJob(
        repo="example",
        repo_root=root.resolve(),
        request=request,
        descr="ensure scope expansion children",
    )


def test_child_creation_failure_before_post_does_not_blindly_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A durable intent without child evidence fails closed on restart."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    events: list[tuple[str, tuple[object, ...]]] = []

    class BeforeChildPost(_ScopeExpansionFakeGitHub):
        attempts: ClassVar[int] = 0

        def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
            del title, body, labels
            type(self).attempts += 1
            raise RuntimeError("failed before child POST")

    BeforeChildPost.reset(events)
    monkeypatch.setattr(module, "PipelineGitHub", BeforeChildPost)
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    job = _scope_job(_child_request(), tmp_path)

    with pytest.raises(RuntimeError, match="before child POST"):
        runner.run(job)
    receipt = runner.run(job)

    assert receipt.status == "operator_required"
    assert BeforeChildPost.attempts == 1


def test_child_creation_outcome_unknown_recovers_exact_owned_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart adopts the exact child after an unknown POST outcome."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    events: list[tuple[str, tuple[object, ...]]] = []

    class AfterChildPost(_ScopeExpansionFakeGitHub):
        fail_once: ClassVar[bool] = True

        def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
            issue_number = super().create_issue(title, body, labels)
            if type(self).fail_once:
                type(self).fail_once = False
                raise RuntimeError("child POST outcome unknown")
            return issue_number

    AfterChildPost.reset(events)
    monkeypatch.setattr(module, "PipelineGitHub", AfterChildPost)
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    job = _scope_job(_child_request(), tmp_path)

    with pytest.raises(RuntimeError, match="outcome unknown"):
        runner.run(job)
    receipt = runner.run(job)

    assert receipt.status == "blocked"
    assert receipt.child_issue_numbers == (901,)
    assert [name for name, _ in events].count("create_issue") == 1


@pytest.mark.parametrize("outcome_unknown", [False, True])
def test_blocking_review_post_restart_finishes_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome_unknown: bool,
) -> None:
    """A restart resumes before or after the blocking-review mutation."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    events: list[tuple[str, tuple[object, ...]]] = []

    class InterruptedReview(_ScopeExpansionFakeGitHub):
        fail_once: ClassVar[bool] = True

        def post_scope_expansion_blocking_review(
            self, pr_number: int, *, body: str, marker: str
        ) -> str:
            if type(self).fail_once:
                type(self).fail_once = False
                if outcome_unknown:
                    super().post_scope_expansion_blocking_review(
                        pr_number, body=body, marker=marker
                    )
                raise RuntimeError("blocking review interrupted")
            return super().post_scope_expansion_blocking_review(pr_number, body=body, marker=marker)

    InterruptedReview.reset(events)
    monkeypatch.setattr(module, "PipelineGitHub", InterruptedReview)
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    job = _scope_job(_child_request(), tmp_path)

    with pytest.raises(RuntimeError, match="blocking review interrupted"):
        runner.run(job)
    receipt = runner.run(job)

    assert receipt.status == "blocked"
    assert [name for name, _ in events].count("post_scope_expansion_blocking_review") == 1


def test_existing_blocked_child_replaces_projection_before_thread_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later same-child audit durably replaces its pending retraction projection."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    events: list[tuple[str, tuple[object, ...]]] = []
    _ScopeExpansionFakeGitHub.reset(events)
    monkeypatch.setattr(module, "PipelineGitHub", _ScopeExpansionFakeGitHub)
    runner = module.PipelineGitHubJobRunner(org="example-org", dry_run=False)
    original = _child_request()
    runner.run(_scope_job(original, tmp_path))
    finding = {
        "path": "extra.py",
        "line": 1,
        "side": "RIGHT",
        "body": (
            '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
            "Remove the out-of-scope file."
        ),
    }
    updated = EnsureScopeExpansionChildrenRequest(
        issue_number=original.issue_number,
        pr_number=original.pr_number,
        reviewed_head_sha=original.reviewed_head_sha,
        scope_expansions=original.scope_expansions,
        retraction_findings=FrozenJson.snapshot([finding]),
        review_diff="diff --git a/extra.py b/extra.py\n",
    )

    receipt = runner.run(_scope_job(updated, tmp_path))
    lifecycle = parse_scope_expansion_lifecycle_comment(
        _ScopeExpansionFakeGitHub.comments[7][0].body
    )

    assert receipt.status == "blocked"
    assert lifecycle is not None
    assert lifecycle.retraction_findings == (finding,)
    assert lifecycle.review_diff == updated.review_diff


def test_runner_rejects_scope_expansion_when_the_source_head_drifted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale review cannot create a child issue or change source state."""
    module = importlib.import_module("hephaestus.automation.pipeline_github_jobs")
    events: list[tuple[str, tuple[object, ...]]] = []
    _ScopeExpansionFakeGitHub.reset(events)

    class DriftedGitHub(_ScopeExpansionFakeGitHub):
        def gh_pr_state(self, pr_number: int) -> dict[str, object]:
            state = super().gh_pr_state(pr_number)
            state["headRefOid"] = "b" * 40
            return state

    monkeypatch.setattr(module, "PipelineGitHub", DriftedGitHub)
    expansion = ScopeExpansion(
        title="Extract shared helper",
        reason="Prerequisite work must ship first",
        source_path="hephaestus/automation/example.py",
        source_line=17,
        required_paths=("hephaestus/automation/example.py",),
        acceptance_criteria=("Helper exists",),
    )
    request = EnsureScopeExpansionChildrenRequest(
        issue_number=17,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        scope_expansions=(expansion,),
    )

    with pytest.raises(RuntimeError, match="reviewed head changed"):
        module.PipelineGitHubJobRunner(org="example-org", dry_run=False).run(
            GitHubJob(
                repo="example",
                repo_root=tmp_path.resolve(),
                request=request,
                descr="ensure scope expansion children",
            )
        )

    assert events == []


def test_runner_dry_run_reports_scope_expansion_split_without_mutation(  # noqa: C901
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

        def issue_comments(self, issue_number: int) -> list[IssueComment]:
            del issue_number
            return []

        def gh_pr_state(self, pr_number: int) -> dict[str, object]:
            return {
                "number": pr_number,
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "autoMergeRequest": None,
            }

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
        "isResolved": False,
        "pr_state": {"state": "OPEN", "headRefOid": "a" * 40},
        "comments": [
            {
                "id": "posted-comment",
                "body": "guard None",
                "viewer_did_author": True,
                "review_id": "posted-review",
                "review_state": "COMMENTED",
                "review_commit_sha": "a" * 40,
            }
        ],
    }
    late = {
        "id": "late-thread",
        "path": "b.py",
        "line": 4,
        "side": "RIGHT",
        "severity": "major",
        "body": "late race",
        "isResolved": False,
        "pr_state": {"state": "OPEN", "headRefOid": "a" * 40},
        "comments": [
            {
                "id": "late-comment",
                "body": "late race",
                "viewer_did_author": True,
                "review_id": "late-review",
                "review_state": "COMMENTED",
                "review_commit_sha": "a" * 40,
            }
        ],
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
