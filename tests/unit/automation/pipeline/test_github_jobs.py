"""Contracts for the pipeline's closed GitHub worker boundary."""

from __future__ import annotations

import importlib
import json
import math
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    DecompositionChildrenRequest,
    GitHubJob,
)
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.plan_decomposition import WorkUnit, normalize_work_unit
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


class _DecompositionFakeGitHub:
    """Mutable per-test GitHub double for the decomposition worker handlers."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.events: list[tuple[str, tuple[Any, ...]]] = []
        self._next_issue = 900

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
        self._next_issue += 1
        self.issues[self._next_issue] = {
            "title": title,
            "body": body,
            "state": "OPEN",
            "labels": sorted(labels or []),
        }
        self.events.append(("create_issue", (self._next_issue, title)))
        return self._next_issue

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        issue = self.issues.get(
            issue_number, {"title": "parent", "body": "", "state": "OPEN", "labels": []}
        )
        return {"number": issue_number, **issue}

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        del issue_number
        return []

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        issue = self.issues.setdefault(
            issue_number, {"title": "parent", "body": "", "state": "OPEN", "labels": []}
        )
        labels = set(issue.get("labels") or [])
        labels.update(add)
        labels.difference_update(remove)
        issue["labels"] = sorted(labels)
        self.events.append(("edit_labels", (issue_number, tuple(add), tuple(remove))))

    def _owner_name(self) -> tuple[str, str]:
        return "org", "repo"

    def _gh(self, args: list[str]) -> Any:
        assert args[:2] == ["issue", "list"]
        payload = [
            {
                "number": number,
                "title": issue["title"],
                "body": issue["body"],
                "state": issue["state"],
            }
            for number, issue in sorted(self.issues.items())
            if issue["state"] == "OPEN"
        ]
        return SimpleNamespace(stdout=json.dumps(payload))


def _unit_payload(node_id: str = "node-a", **overrides: Any) -> dict[str, Any]:
    """Return one valid closed-schema work-unit payload."""
    payload: dict[str, Any] = {
        "node_id": node_id,
        "title": f"Implement {node_id}",
        "owned_behavior": f"Node {node_id} owns one parser behavior.",
        "repository_paths": ("hephaestus/utils/parser.py",),
        "acceptance_criteria": ("The parser rejects malformed payloads.",),
        "failure_behavior": "The parser raises ValueError.",
        "verification_commands": ("uv run pytest tests/unit/utils/test_parser.py",),
        "expected_evidence": "The focused parser test passes.",
        "prerequisites": (),
        "requirement_ids": ("req-" + "0" * 12,),
        "finding_ids": (),
        "single_pr": True,
    }
    payload.update(overrides)
    return payload


def _unit_work_unit(node_id: str = "node-a") -> WorkUnit:
    unit = normalize_work_unit(_unit_payload(node_id))
    assert unit is not None
    return unit


def _child_request(
    tree: list[dict[str, Any]] | None = None,
    *,
    repo: str = "org/repo",
    issue: int = 8,
    epoch: str = "e" * 64,
) -> Any:
    from hephaestus.automation.pipeline.github_jobs import FrozenJson

    nodes = tree or [_unit_work_unit().as_dict()]
    layers = [[unit["node_id"] for unit in nodes]]
    return DecompositionChildrenRequest(
        repository=repo,
        issue_number=issue,
        deadline_s=30.0,
        tree=FrozenJson.snapshot(nodes),
        requirements_epoch=epoch,
        layers=FrozenJson.snapshot(layers),
    )


def _child_body(unit: WorkUnit, *, epoch: str = "e" * 64) -> str:
    from hephaestus.automation.plan_decomposition import child_recovery_marker, render_child_body

    return render_child_body(
        parent_issue=8,
        unit=unit,
        prerequisite_issues={},
        plan_revision=1,
        requirements_epoch=epoch,
        marker=child_recovery_marker(
            repository="org/repo",
            parent_issue=8,
            requirements_epoch=epoch,
            generation=1,
            node_id=unit.node_id,
        ),
    )


def test_ambiguous_child_create_recovers_exact_marker() -> None:
    """The exact recovery marker adopts one child; two matches fail closed."""
    from hephaestus.automation.pipeline_github_jobs import _create_decomposition_children

    epoch = "e" * 64
    unit = _unit_work_unit()
    body = _child_body(unit, epoch=epoch)
    request = _child_request(epoch=epoch)

    fake = _DecompositionFakeGitHub()
    fake.issues[41] = {"title": unit.title, "body": body, "state": "OPEN", "labels": []}
    receipt = _create_decomposition_children(request, cast(PipelineGitHub, fake))
    assert receipt.failures == ()
    entries = cast(list[dict[str, Any]], receipt.children.thaw())
    assert [(c["node_id"], c["issue"], c["adopted"]) for c in entries] == [("node-a", 41, True)]
    # The exact marker recovery creates no duplicate child.
    assert not any(entry[0] == "create_issue" for entry in fake.events)

    duplicated = _DecompositionFakeGitHub()
    duplicated.issues[41] = {"title": unit.title, "body": body, "state": "OPEN", "labels": []}
    duplicated.issues[42] = {"title": unit.title, "body": body, "state": "OPEN", "labels": []}
    failed = _create_decomposition_children(request, cast(PipelineGitHub, duplicated))
    assert failed.failures and "ambiguous child marker" in failed.failures[0]
    assert cast(list[dict[str, Any]], failed.children.thaw()) == []


def test_duplicate_marker_fails_closed() -> None:
    """Two open issues carrying one child marker stop the creation job."""
    from hephaestus.automation.pipeline_github_jobs import _create_decomposition_children

    epoch = "e" * 64
    unit = _unit_work_unit()
    body = _child_body(unit, epoch=epoch)
    fake = _DecompositionFakeGitHub()
    fake.issues[41] = {"title": unit.title, "body": body, "state": "OPEN", "labels": []}
    fake.issues[42] = {"title": unit.title, "body": body, "state": "OPEN", "labels": []}

    receipt = _create_decomposition_children(
        _child_request(epoch=epoch), cast(PipelineGitHub, fake)
    )

    assert receipt.failures == ("ambiguous child marker for node node-a",)
    assert cast(list[dict[str, Any]], receipt.children.thaw()) == []
    assert not any(entry[0] == "create_issue" for entry in fake.events)


def test_partial_children_resume() -> None:
    """A re-run adopts the durable children and creates the missing ones."""
    from hephaestus.automation.pipeline_github_jobs import _create_decomposition_children

    epoch = "e" * 64
    first_unit = _unit_work_unit("node-a")
    second_unit = _unit_work_unit("node-b")
    request = _child_request([first_unit.as_dict(), second_unit.as_dict()], epoch=epoch)

    class OneCreateFails(_DecompositionFakeGitHub):
        def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int:
            if title == first_unit.title:
                raise RuntimeError("child creation rejected")
            return super().create_issue(title, body)

    flaky = OneCreateFails()
    receipt = _create_decomposition_children(request, cast(PipelineGitHub, flaky))
    assert receipt.failures and "creation failed" in receipt.failures[0]
    flaky_children = cast(list[dict[str, Any]], receipt.children.thaw())
    assert [(c["node_id"], c["adopted"]) for c in flaky_children] == [("node-b", False)]

    # The re-run sees the durable child issue and adopts it; child-a is
    # created exactly once and both children verify against the seal.
    resumed = _DecompositionFakeGitHub()
    resumed.issues.update(flaky.issues)
    resumed._next_issue = flaky._next_issue
    resumed_receipt = _create_decomposition_children(request, cast(PipelineGitHub, resumed))
    assert resumed_receipt.failures == ()
    resumed_children = cast(list[dict[str, Any]], resumed_receipt.children.thaw())
    entries = [(c["node_id"], c["adopted"]) for c in resumed_children]
    assert entries == [("node-a", False), ("node-b", True)]
    assert [entry[0] for entry in resumed.events].count("create_issue") == 1


def test_decomposition_preserves_parent_title_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The epic transition confirms that the parent identity text survived."""
    from hephaestus.automation import github_api
    from hephaestus.automation.pipeline.github_jobs import DecompositionParentTransitionRequest
    from hephaestus.automation.pipeline_github_jobs import _transition_decomposition_parent

    fake = _DecompositionFakeGitHub()
    fake.issues[8] = {
        "title": "Epic: parser overhaul",
        "body": "The complete epic body.\n\nstate:needs-plan",
        "state": "OPEN",
        "labels": ["state:needs-plan"],
    }
    request = DecompositionParentTransitionRequest(
        repository="org/repo",
        issue_number=8,
        deadline_s=30.0,
        summary_marker=f"<!-- hephaestus-plan-decomposition-summary:v1:{'e' * 64} -->",
        summary_body="The complete decomposition summary.",
        labels_to_add=("epic", "state:skip"),
        labels_to_remove=("state:needs-plan",),
    )
    upserts: list[tuple[int, str, str]] = []

    def record_upsert(
        issue_number: int, marker: str, body: str, repo: tuple[str, str] | None = None
    ) -> int | None:
        upserts.append((issue_number, marker, body))
        return 1

    monkeypatch.setattr(github_api.issues, "gh_issue_upsert_comment", record_upsert)

    receipt = _transition_decomposition_parent(request, cast(PipelineGitHub, fake))

    assert receipt.failures == ()
    assert receipt.title_and_body_preserved
    assert receipt.title_digest_before == receipt.title_digest_after
    assert receipt.body_digest_before == receipt.body_digest_after
    assert upserts == [(8, request.summary_marker, request.summary_body)]
    assert set(cast(list[str], receipt.labels_after.thaw())) == {"epic", "state:skip"}


def test_parent_epic_transition_is_exclusive_and_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The epic labels replace every planning state in one confirmed write."""
    from hephaestus.automation import github_api
    from hephaestus.automation.pipeline.github_jobs import DecompositionParentTransitionRequest
    from hephaestus.automation.pipeline_github_jobs import _transition_decomposition_parent

    fake = _DecompositionFakeGitHub()
    fake.issues[8] = {
        "title": "Epic: parser overhaul",
        "body": "The complete epic body.",
        "state": "OPEN",
        "labels": ["state:needs-plan", "state:plan-no-go", "state:plan-go", "severity:high"],
    }
    request = DecompositionParentTransitionRequest(
        repository="org/repo",
        issue_number=8,
        deadline_s=30.0,
        summary_marker=f"<!-- hephaestus-plan-decomposition-summary:v1:{'e' * 64} -->",
        summary_body="The complete decomposition summary.",
        labels_to_add=("epic", "state:skip"),
        labels_to_remove=("state:needs-plan", "state:plan-no-go", "state:plan-go"),
    )
    monkeypatch.setattr(
        github_api.issues,
        "gh_issue_upsert_comment",
        lambda issue_number, marker, body, repo=None: 1,
    )

    receipt = _transition_decomposition_parent(request, cast(PipelineGitHub, fake))

    assert receipt.failures == ()
    labels = set(cast(list[str], receipt.labels_after.thaw()))
    assert {"epic", "state:skip", "severity:high"}.issubset(labels)
    assert labels.isdisjoint({"state:needs-plan", "state:plan-no-go", "state:plan-go"})
    assert (
        "edit_labels",
        (
            8,
            ("epic", "state:skip"),
            ("state:needs-plan", "state:plan-no-go", "state:plan-go"),
        ),
    ) in fake.events


def test_child_labels_are_added_and_confirmed() -> None:
    """Each child receives the confirmed labels and one exact readback."""
    from hephaestus.automation.pipeline.github_jobs import (
        DecompositionChildLabelsRequest,
        FrozenJson,
    )
    from hephaestus.automation.pipeline_github_jobs import _apply_decomposition_child_labels

    fake = _DecompositionFakeGitHub()
    fake.issues[41] = {"title": "child a", "body": "body", "state": "OPEN", "labels": []}
    fake.issues[42] = {"title": "child b", "body": "body", "state": "OPEN", "labels": []}
    request = DecompositionChildLabelsRequest(
        repository="org/repo",
        issue_number=8,
        deadline_s=30.0,
        children=FrozenJson.snapshot(
            [{"node_id": "node-a", "issue": 41}, {"node_id": "node-b", "issue": 42}]
        ),
        labels_to_add=("state:needs-plan", "severity:high"),
    )

    receipt = _apply_decomposition_child_labels(request, cast(PipelineGitHub, fake))

    assert receipt.failures == ()
    confirmed = cast(list[dict[str, Any]], receipt.confirmed.thaw())
    assert [(c["node_id"], c["issue"]) for c in confirmed] == [
        ("node-a", 41),
        ("node-b", 42),
    ]
    assert set(fake.gh_issue_json(41)["labels"]) == {"state:needs-plan", "severity:high"}
    assert set(fake.gh_issue_json(42)["labels"]) == {"state:needs-plan", "severity:high"}


def test_child_label_failure_is_reported_not_guessed() -> None:
    """A child whose labels are not confirmed lands in the failure manifest."""
    from hephaestus.automation.pipeline.github_jobs import (
        DecompositionChildLabelsRequest,
        FrozenJson,
    )
    from hephaestus.automation.pipeline_github_jobs import _apply_decomposition_child_labels

    class NoLabelWrite(_DecompositionFakeGitHub):
        def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
            del add, remove
            self.events.append(("edit_labels", (issue_number, (), ())))

    fake = NoLabelWrite()
    fake.issues[41] = {"title": "child a", "body": "body", "state": "OPEN", "labels": []}
    request = DecompositionChildLabelsRequest(
        repository="org/repo",
        issue_number=8,
        deadline_s=30.0,
        children=FrozenJson.snapshot([{"node_id": "node-a", "issue": 41}]),
        labels_to_add=("state:needs-plan",),
    )

    receipt = _apply_decomposition_child_labels(request, cast(PipelineGitHub, fake))

    assert cast(list[dict[str, Any]], receipt.confirmed.thaw()) == []
    assert receipt.failures and "were not confirmed" in receipt.failures[0]
