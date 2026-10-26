"""Contracts for the pipeline's closed GitHub worker boundary."""

from __future__ import annotations

import importlib
import math
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
)
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.review_journal import IssueComment


class _DeadlineAccessor:
    """Keep service fixtures inside an explicit operation deadline."""

    @contextmanager
    def operation_deadline(
        self, deadline_s: float, *, shutdown: threading.Event | None = None
    ) -> Iterator[_DeadlineAccessor]:
        """Require a finite positive deadline and the current cancellation signal."""
        assert math.isfinite(deadline_s) and deadline_s > 0
        if shutdown is not None and shutdown.is_set():
            raise InterruptedError("test GitHub operation cancelled")
        yield self


class _ScopeExpansionFakeGitHub(_DeadlineAccessor):
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

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        if not self.dry_run:
            type(self).implementation_no_go = True
            self.events.append(("mark_pr_implementation_no_go", (pr_number,)))


class _ScopeDependencyRecoveryGitHub(_ScopeExpansionFakeGitHub):
    """Retain the remote transaction across separate worker requests."""

    _repo_slug = "example-org/example"

    def gh_pr_state(self, pr_number: int) -> dict[str, object]:
        """Read the current source head before recovery can write."""
        state = super().gh_pr_state(pr_number)
        state["baseRefName"] = "main"
        self.events.append(("read_source_head", (pr_number, state["headRefOid"])))
        return state

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, object]]:
        """Return no retraction work for this pending child."""
        self.events.append(("read_unresolved_threads", (pr_number,)))
        return []


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


@pytest.mark.parametrize(
    "branch",
    [
        "foreign",
        "6-auto-impl-direct-" + "a" * 32,
        "../branch",
        "--all",
        "5-auto-impl-direct-" + "g" * 32,
    ],
)
def test_dirty_direct_pr_request_rejects_foreign_branch(branch: str) -> None:
    """A closed read cannot select a different or malformed writer branch."""
    from hephaestus.automation.pipeline.github_jobs import InspectDirtyDirectPrStateRequest

    with pytest.raises(ValueError, match="branch"):
        InspectDirtyDirectPrStateRequest("org/repo", 5, branch)


def test_dirty_direct_pr_receipt_keeps_immutable_complete_evidence() -> None:
    """Only complete empty evidence supplies absence."""
    from dataclasses import FrozenInstanceError

    from hephaestus.automation.pipeline.github_jobs import DirtyDirectPrStateRead

    branch = "5-auto-impl-direct-" + "a" * 32
    empty = DirtyDirectPrStateRead("org/repo", 5, branch, (), None)
    assert empty.absent
    assert not DirtyDirectPrStateRead("org/repo", 5, branch, (), None, complete=False).absent
    assert not DirtyDirectPrStateRead("org/repo", 5, branch, ((7, "release"),), None).absent
    assert not DirtyDirectPrStateRead("org/repo", 5, branch, (), 8).absent
    with pytest.raises(FrozenInstanceError):
        cast(Any, empty).complete = False
    with pytest.raises(ValueError):
        DirtyDirectPrStateRead("org/repo", 5, branch, ((7, "main"), (7, "release")), None)


def test_dirty_direct_pr_runner_uses_fresh_repo_bound_accessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each locked worker read gets an independent repository-scoped accessor."""
    from hephaestus.automation import pipeline_github_jobs as module
    from hephaestus.automation.pipeline.github_jobs import (
        DirtyDirectPrStateRead,
        InspectDirtyDirectPrStateRequest,
    )

    accessors: list[object] = []
    calls: list[object] = []

    class Accessor(_DeadlineAccessor):
        def __init__(self, org: str, *, repo: str, **kwargs: object) -> None:
            accessors.append(self)
            calls.append((org, repo))

        def open_prs_for_branch(self, branch: str) -> list[tuple[int, str]]:
            calls.append(branch)
            return []

        def find_pr_for_issue(self, issue_number: int) -> int | None:
            calls.append(issue_number)
            return None

        def issue_comments(self, issue_number: int) -> list[object]:
            return []

        def gh_issue_json(self, issue_number: int) -> dict[str, object]:
            return {
                "number": issue_number,
                "state": "OPEN",
                "labels": [{"name": "state:plan-go"}],
            }

    monkeypatch.setattr(module, "PipelineGitHub", Accessor)
    branch = "5-auto-impl-direct-" + "a" * 32
    request = InspectDirtyDirectPrStateRequest("org/repo", 5, branch)
    job = GitHubJob(repo="repo", repo_root=tmp_path, request=request, descr="Read direct PR state")
    runner = module.PipelineGitHubJobRunner(org="org", dry_run=False)
    for _ in range(2):
        receipt = runner.run(job)
        assert isinstance(receipt, DirtyDirectPrStateRead)
        assert receipt.repository == "org/repo"
        assert receipt.branch == branch
        assert receipt.issue_number == 5
        assert receipt.complete and receipt.absent
    assert len(accessors) == 2 and accessors[0] is not accessors[1]
    assert calls == [("org", "repo"), branch, 5] * 2
    with pytest.raises(ValueError, match="repository"):
        module.PipelineGitHubJobRunner(org="foreign", dry_run=False).run(job)
    assert len(accessors) == 2
    with pytest.raises(ValueError, match="repository"):
        GitHubJob(repo="foreign", repo_root=tmp_path, request=request, descr="Read direct PR state")


def test_dirty_direct_pr_runner_rejects_wrong_issue_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response for a different issue cannot supply plan authority."""
    from hephaestus.automation import pipeline_github_jobs as module
    from hephaestus.automation.pipeline.github_jobs import InspectDirtyDirectPrStateRequest

    class WrongIssueAccessor(_DeadlineAccessor):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def open_prs_for_branch(self, branch: str) -> list[tuple[int, str]]:
            return []

        def find_pr_for_issue(self, issue_number: int) -> int | None:
            return None

        def issue_comments(self, issue_number: int) -> list[object]:
            return []

        def gh_issue_json(self, issue_number: int) -> dict[str, object]:
            return {
                "number": issue_number + 1,
                "state": "OPEN",
                "labels": [{"name": "state:plan-go"}],
            }

    monkeypatch.setattr(module, "PipelineGitHub", WrongIssueAccessor)
    branch = "5-auto-impl-direct-" + "a" * 32
    request = InspectDirtyDirectPrStateRequest("org/repo", 5, branch)
    job = GitHubJob(repo="repo", repo_root=tmp_path, request=request, descr="Read direct PR state")

    with pytest.raises(RuntimeError, match="identity"):
        module.PipelineGitHubJobRunner(org="org", dry_run=False).run(job)


def test_dirty_direct_read_bounds_plan_journal() -> None:
    """Reject unbounded plan evidence before it reaches the worker."""
    from hephaestus.automation.pipeline.github_jobs import DirtyDirectPrStateRead, FrozenJson

    with pytest.raises(ValueError, match="journal"):
        DirtyDirectPrStateRead(
            "org/repo",
            5,
            "5-auto-impl-direct-" + "a" * 32,
            (),
            None,
            plan_journal=FrozenJson.snapshot(["x" * (1024 * 1024)]),
        )


def _adopted_read_threads() -> list[dict[str, Any]]:
    """Return two complete unresolved threads for the adopted read contract."""
    return [
        {
            "id": f"thread-{index}",
            "isResolved": False,
            "path": "a.py",
            "line": index,
            "side": "RIGHT",
            "comments": [
                {"id": f"comment-{index}", "author": "reviewer", "body": "Correct the test."}
            ],
        }
        for index in (1, 2)
    ]


class _AdoptedReadAccessor(_DeadlineAccessor):
    def __init__(
        self, org: str, *, repo: str, calls: list[list[str]], fault: str | None, **kwargs: object
    ) -> None:
        assert (org, repo) == ("org", "repo")
        self.fault = fault
        self.events: list[str] = []
        self.state_reads = 0
        calls.append(self.events)

    def gh_pr_state(self, number: int) -> object:
        assert number == 10
        self.events.append("state")
        self.state_reads += 1
        if self.fault == "missing-state":
            return None
        if self.fault == "malformed-state":
            return {"state": "OPEN"}
        state = "CLOSED" if self.fault == "closed" else "OPEN"
        head = (
            "b" * 40
            if self.fault == "head" or (self.fault == "head-drift" and self.state_reads == 2)
            else "a" * 40
        )
        return {"number": 10, "state": state, "headRefOid": head}

    def get_pr_head_branch(self, number: int) -> str:
        self.events.append("branch")
        return "other" if self.fault == "branch" else "writer-branch"

    def pr_head_is_writable(self, number: int) -> object:
        self.events.append("writable")
        if self.fault in {"fork", "unwritable"}:
            return False
        return 1 if self.fault == "malformed-writable" else True

    def find_pr_for_issue(self, number: int) -> object:
        assert number == 9
        self.events.append("carrier")
        if self.fault == "carrier":
            return 11
        return 10.0 if self.fault == "malformed-carrier" else 10

    def list_unresolved_review_threads(self, number: int) -> object:
        assert number == 10
        self.events.append("threads")
        if self.fault == "partial-error":
            raise RuntimeError("review thread page is incomplete")
        if self.fault == "malformed-threads":
            return {"nodes": []}
        threads = _adopted_read_threads()
        if self.fault == "partial-threads":
            return threads[:1]
        if self.fault == "thread-drift":
            threads[0]["comments"][0]["body"] = "Changed review request."
        return threads

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Unexpected accessor operation: {name}")


def _adopted_read_accessor(
    monkeypatch: pytest.MonkeyPatch, *, fault: str | None = None
) -> list[list[str]]:
    """Install a read-only accessor that records every attempted operation."""
    from functools import partial

    from hephaestus.automation import pipeline_github_jobs as module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        module, "PipelineGitHub", partial(_AdoptedReadAccessor, calls=calls, fault=fault)
    )
    return calls


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "enabled", "already"),
    [
        ("", "! Pull request owner/repo#7 is already queued to merge\n", 1, True, True),
        ("", "auto-merge already enabled\n", 1, True, True),
        ("✓ auto-merge enabled\n", "", 0, True, False),
        ("", "! The merge strategy for main is set by the merge queue\n", 0, True, False),
        ("", "fatal: auto-merge is not supported\n", 1, False, False),
    ],
)
def test_enable_auto_merge_reads_the_gh_report(
    stdout: str, stderr: str, returncode: int, enabled: bool, already: bool
) -> None:
    """The auto-merge adapter reads the report that GitHub actually writes."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    github = PipelineGitHub("acme", repo="repo", command_runner=run)

    result = github.enable_auto_merge(7)

    assert result.enabled is enabled
    assert result.already_enabled is already
    assert calls == [["pr", "merge", "7", "--squash", "--auto"]]
