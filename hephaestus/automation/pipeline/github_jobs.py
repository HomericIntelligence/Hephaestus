"""Closed immutable requests and receipts for worker-owned GitHub I/O.

Only the value objects in this module may cross the coordinator/worker GitHub
boundary.  Nested service data is represented as canonical JSON so neither a
stage nor a worker can retain a shared mutable response object.
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, Self

from hephaestus.automation.pipeline.repository_validation import (
    RepositoryValidationGap,
    RepositoryValidationInvocation,
    RepositoryValidationPlan,
    RepositoryValidationReceipt,
)
from hephaestus.automation.pipeline.scope_retraction import (
    is_safe_scope_retraction_path,
)

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_JOURNAL_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-reply-handoff:"
    r"pr=[1-9][0-9]*:head=[0-9a-f]{40}:batch=[0-9a-f]{32} -->"
)
_REMEDIATION_JOURNAL_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-remediation-reply-handoff:"
    r"pr=[1-9][0-9]*:head=[0-9a-f]{40}(?:[0-9a-f]{24})?:"
    r"batch=[0-9a-f]{32}:seq=(?:0|[1-9][0-9]*) -->"
)


@dataclass(frozen=True)
class FrozenJson:
    """Canonical immutable snapshot of JSON-compatible service data."""

    encoded: str

    @classmethod
    def snapshot(cls, value: object) -> Self:
        """Deep-copy *value* into a deterministic immutable representation."""
        return cls(
            encoded=json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def __post_init__(self) -> None:
        """Reject invalid or non-canonical encoded values."""
        try:
            value = json.loads(self.encoded)
            canonical = json.dumps(
                value,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("FrozenJson must contain canonical JSON") from error
        if canonical != self.encoded:
            raise ValueError("FrozenJson must contain canonical JSON")

    def thaw(self) -> object:
        """Return a fresh mutable decode without exposing shared state."""
        return json.loads(self.encoded)


def _positive_identifier(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _full_sha(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase commit SHA")


def _deadline(value: float | None) -> None:
    """Reject an invalid absolute monotonic operation deadline."""
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("deadline_s must be a finite positive monotonic time")


def _json_root(value: FrozenJson, expected: type[object], field_name: str) -> object:
    if not isinstance(value, FrozenJson):
        raise ValueError(f"{field_name} must contain a JSON {expected.__name__}")
    root = value.thaw()
    if not isinstance(root, expected):
        raise ValueError(f"{field_name} must contain a JSON {expected.__name__}")
    return root


@dataclass(frozen=True)
class ReadCurrentPlanScopeRequest:
    """Read the current plan scope for one repository and issue."""

    repository: str
    issue_number: int
    deadline_s: float

    def __post_init__(self) -> None:
        """Require a complete target identity and operation deadline."""
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("repository must be an owner/name identity")
        _positive_identifier(self.issue_number, "issue_number")
        if self.deadline_s is None:
            raise ValueError("deadline_s is required for a plan scope read")
        _deadline(self.deadline_s)


@dataclass(frozen=True)
class CurrentPlanScopeRead:
    """Freeze the exact plan digest and declared paths for one completed read."""

    request: ReadCurrentPlanScopeRequest
    paths: tuple[str, ...]
    plan_sha256: str

    def __post_init__(self) -> None:
        """Reject incomplete or noncanonical scope evidence."""
        if not isinstance(self.request, ReadCurrentPlanScopeRequest):
            raise ValueError("request must be a current plan scope read")
        if (
            not isinstance(self.paths, tuple)
            or not all(
                isinstance(path, str) and is_safe_scope_retraction_path(path) for path in self.paths
            )
            or tuple(sorted(set(self.paths))) != self.paths
        ):
            raise ValueError("paths must contain sorted unique source paths")
        if (
            not isinstance(self.plan_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.plan_sha256) is None
        ):
            raise ValueError("plan_sha256 must be a full SHA256 digest")


@dataclass(frozen=True)
class ReadRateBudgetRequest:
    """Read quota facts within one absolute monotonic deadline."""

    deadline_s: float

    def __post_init__(self) -> None:
        """Require a finite, positive operation deadline."""
        if self.deadline_s is None:
            raise ValueError("deadline_s is required for a quota read")
        _deadline(self.deadline_s)


@dataclass(frozen=True)
class RateBudgetRead:
    """Return quota facts, or an explicit unavailable result."""

    request: ReadRateBudgetRequest
    remaining: int | None
    reset_epoch: int | None

    def __post_init__(self) -> None:
        """Reject partial or invalid quota facts."""
        if not isinstance(self.request, ReadRateBudgetRequest):
            raise ValueError("request must be a quota read")
        if self.remaining is None and self.reset_epoch is None:
            return
        if any(type(value) is not int or value < 0 for value in (self.remaining, self.reset_epoch)):
            raise ValueError("quota facts must be non-negative integers or both unavailable")


def _empty_frozen_list() -> FrozenJson:
    """Create an empty immutable JSON list for optional receipt data."""
    return FrozenJson.snapshot([])


@dataclass(frozen=True)
class ReconcilePrReviewRequest:
    """Apply the reviewer's thread decisions, post its findings, and read the open threads."""

    pr_number: int
    findings: FrozenJson
    review_diff: str
    deadline_s: float
    resolved_thread_ids: tuple[str, ...] = ()
    feedback: FrozenJson = field(default_factory=lambda: FrozenJson.snapshot({}))
    issue_number: int | None = None
    summary: str = ""

    def __post_init__(self) -> None:
        """Validate the request."""
        _positive_identifier(self.pr_number, "pr_number")
        _json_root(self.findings, list, "findings")
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        _json_root(self.feedback, dict, "feedback")
        if not isinstance(self.review_diff, str):
            raise ValueError("review_diff must be a string")
        if not isinstance(self.resolved_thread_ids, tuple) or not all(
            isinstance(thread_id, str) and thread_id for thread_id in self.resolved_thread_ids
        ):
            raise ValueError("resolved_thread_ids must be a tuple of non-empty strings")
        _deadline(self.deadline_s)
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")


@dataclass(frozen=True)
class RunMergeWaitCycleRequest:
    """Run one merge admission cycle: arm native auto-merge and observe the PR."""

    pr_number: int
    deadline_s: float
    cancellation: threading.Event
    issue_number: int | None = None
    auto_merge_armed: bool = False

    def __post_init__(self) -> None:
        """Validate the merge request."""
        _positive_identifier(self.pr_number, "pr_number")
        if type(self.auto_merge_armed) is not bool:
            raise ValueError("auto_merge_armed must be a boolean")
        if (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic deadline")
        if not isinstance(self.cancellation, threading.Event):
            raise ValueError("cancellation must be a threading.Event")
        if self.issue_number is not None:
            _positive_identifier(self.issue_number, "issue_number")


@dataclass(frozen=True)
class InspectDirtyDirectPrStateRequest:
    """Select the exact repository, issue, and interrupted direct branch."""

    repository: str
    issue_number: int
    branch: str

    def __post_init__(self) -> None:
        """Reject identities outside the closed direct-writer read."""
        _positive_identifier(self.issue_number, "issue_number")
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("repository must be OWNER/REPOSITORY")
        # The branch is read from this writer's own lane, so this only bounds
        # the character set. The arm validator owns the direct-writer identity:
        # a renamed lane is rejected there with a clean failure instead of an
        # escaping error, and the version 1 receipt stays retryable (#3466).
        if (
            not isinstance(self.branch, str)
            or not self.branch
            or self.branch != self.branch.strip()
            or re.fullmatch(r"[A-Za-z0-9._/-]+", self.branch) is None
        ):
            raise ValueError("branch must name the direct issue writer")


@dataclass(frozen=True)
class DirtyDirectPrStateRead:
    """Retain the complete immutable open-PR evidence for one direct writer."""

    repository: str
    issue_number: int
    branch: str
    branch_prs: tuple[tuple[int, str], ...]
    issue_pr_number: int | None
    complete: bool = True
    plan_journal: FrozenJson | None = None
    issue_state: str = ""
    issue_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate identity, immutable rows, and complete-read classification."""
        InspectDirtyDirectPrStateRequest(self.repository, self.issue_number, self.branch)
        if self.plan_journal is not None and (
            not isinstance(self.plan_journal, FrozenJson)
            or len(self.plan_journal.encoded.encode("utf-8")) > 1024 * 1024
            or not isinstance(self.plan_journal.thaw(), list)
        ):
            raise ValueError("dirty direct plan journal is invalid or exceeds its bound")
        if not isinstance(self.issue_state, str) or not isinstance(self.issue_labels, tuple):
            raise ValueError("dirty direct issue state must be immutable")
        if any(not isinstance(label, str) for label in self.issue_labels):
            raise ValueError("dirty direct issue labels must be strings")
        if type(self.complete) is not bool:
            raise ValueError("complete must be a boolean")
        if not isinstance(self.branch_prs, tuple) or len(self.branch_prs) >= 1000:
            raise ValueError("branch PRs must be a bounded immutable tuple")
        numbers: set[int] = set()
        for row in self.branch_prs:
            if not isinstance(row, tuple) or len(row) != 2:
                raise ValueError("branch PR rows must be immutable number/base pairs")
            number, base = row
            _positive_identifier(number, "branch PR number")
            if not isinstance(base, str) or not base or base != base.strip():
                raise ValueError("branch PR base must be a nonblank exact string")
            if number in numbers:
                raise ValueError("branch PR numbers must be unique")
            numbers.add(number)
        if self.issue_pr_number is not None:
            _positive_identifier(self.issue_pr_number, "issue_pr_number")

    @property
    def absent(self) -> bool:
        """Return absence only from a complete read with no open PR."""
        return self.complete and not self.branch_prs and self.issue_pr_number is None


@dataclass(frozen=True)
class InspectRebaseConflictRequest:
    """Read GO and conflict state for the exact replay inputs."""

    repository: str
    pr_number: int
    reviewed_head_sha: str
    base_sha: str

    def __post_init__(self) -> None:
        """Validate the repository, PR, and exact commits."""
        if not isinstance(self.repository, str) or len(self.repository.split("/")) != 2:
            raise ValueError("rebase repository must be OWNER/REPOSITORY")
        if not all(self.repository.split("/")):
            raise ValueError("rebase repository must be OWNER/REPOSITORY")
        _positive_identifier(self.pr_number, "pr_number")
        _full_sha(self.reviewed_head_sha, "reviewed_head_sha")
        _full_sha(self.base_sha, "base_sha")


@dataclass(frozen=True)
class RebaseConflictInspected:
    """Return read-only admission for one exact head and base."""

    request: InspectRebaseConflictRequest
    admitted: bool
    reason: str

    def __post_init__(self) -> None:
        """Require a typed admission result."""
        if not isinstance(self.request, InspectRebaseConflictRequest):
            raise TypeError("rebase admission request is invalid")
        if type(self.admitted) is not bool or not isinstance(self.reason, str) or not self.reason:
            raise ValueError("rebase admission result is invalid")


@dataclass(frozen=True)
class ReadRepositoryValidationCIRequest:
    """Bind a CI read to one immutable validation invocation and deadline."""

    invocation: RepositoryValidationInvocation
    head_branch: str
    deadline_s: float

    def __post_init__(self) -> None:
        """Reject an invalid invocation, source branch, or missing deadline."""
        if (
            type(self.invocation) is not RepositoryValidationInvocation
            or replace(self.invocation) != self.invocation
            or self.invocation.evidence_kind != "ci"
            or not self.invocation.plan.execution_allowed
        ):
            raise ValueError("The CI invocation is invalid.")
        if (
            type(self.head_branch) is not str
            or not 0 < len(self.head_branch) <= 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in self.head_branch)
        ):
            raise ValueError("The CI source branch is invalid.")
        self.head_branch.encode("utf-8", errors="strict")
        if self.deadline_s is None:
            raise ValueError("The CI request requires a deadline.")
        _deadline(self.deadline_s)

    @property
    def plan(self) -> RepositoryValidationPlan:
        """Return the immutable plan from this invocation."""
        return self.invocation.plan

    @property
    def repository(self) -> str:
        """Return the complete repository identity from the bound plan."""
        return self.invocation.plan.repository

    @property
    def issue_number(self) -> int | None:
        """Return the issue that owns this validation attempt."""
        return self.invocation.plan.issue_number

    @property
    def pr_number(self) -> int:
        """Return the pull request from the bound plan."""
        return self.invocation.plan.pr_number


@dataclass(frozen=True)
class RepositoryValidationCIRead:
    """Keep admitted receipts or terminal gaps with their exact request."""

    request: ReadRepositoryValidationCIRequest
    receipts: tuple[RepositoryValidationReceipt, ...] = ()
    gaps: tuple[RepositoryValidationGap, ...] = ()

    def __post_init__(self) -> None:
        """Reject mutable, unrelated, repeated, or malformed evidence."""
        if (
            type(self.request) is not ReadRepositoryValidationCIRequest
            or replace(self.request) != self.request
        ):
            raise ValueError("The CI result request is invalid.")
        if type(self.receipts) is not tuple or len(self.receipts) > 32:
            raise ValueError("The CI result receipt inventory is invalid.")
        plan = self.request.invocation.plan
        checks = {check.check_id: check for check in plan.checks}
        seen: set[str] = set()
        for receipt in self.receipts:
            if (
                type(receipt) is not RepositoryValidationReceipt
                or receipt.check_id not in self.request.invocation.check_ids
                or receipt.check_id in seen
            ):
                raise ValueError("The CI receipt was not uniquely requested.")
            check = checks[receipt.check_id]
            expected = RepositoryValidationReceipt(
                repository=plan.repository,
                pr_number=plan.pr_number,
                plan_id=plan.plan_id,
                check_id=check.check_id,
                reviewed_head=plan.reviewed_head,
                reviewed_base=plan.reviewed_base,
                argv=check.argv,
                source_digests=check.source_digests,
                evidence_kind="ci",
                status=receipt.status,
            )
            if receipt != expected:
                raise ValueError("The CI receipt does not match its request.")
            seen.add(receipt.check_id)
        if type(self.gaps) is not tuple or len(self.gaps) > 64:
            raise ValueError("The CI result gap inventory is invalid.")
        for gap in self.gaps:
            if (
                type(gap) is not RepositoryValidationGap
                or type(gap.check_id) is not str
                or gap.check_id not in {"*", *checks}
                or type(gap.reason) is not str
                or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", gap.reason) is None
            ):
                raise ValueError("The CI result gap is invalid.")


type GitHubRequest = (
    ReadRateBudgetRequest
    | ReadCurrentPlanScopeRequest
    | ReadRepositoryValidationCIRequest
    | InspectDirtyDirectPrStateRequest
    | InspectRebaseConflictRequest
    | ReconcilePrReviewRequest
    | RunMergeWaitCycleRequest
)


@dataclass(frozen=True)
class GitHubJob:
    """One closed GitHub operation submitted to the worker pool."""

    repo: str
    repo_root: Path
    request: GitHubRequest
    descr: str

    def __post_init__(self) -> None:
        """Validate the closed job envelope."""
        if not isinstance(self.repo, str) or not self.repo:
            raise ValueError("repo must be a non-empty string")
        if not isinstance(self.repo_root, Path) or not self.repo_root.is_absolute():
            raise ValueError("repo_root must be an absolute Path")
        if not isinstance(
            self.request,
            (
                ReadRateBudgetRequest,
                ReadCurrentPlanScopeRequest,
                ReadRepositoryValidationCIRequest,
                InspectDirtyDirectPrStateRequest,
                InspectRebaseConflictRequest,
                ReconcilePrReviewRequest,
                RunMergeWaitCycleRequest,
            ),
        ):
            raise TypeError("request must be a supported GitHub request")
        if isinstance(
            self.request,
            (
                InspectDirtyDirectPrStateRequest,
                InspectRebaseConflictRequest,
                ReadCurrentPlanScopeRequest,
                ReadRepositoryValidationCIRequest,
            ),
        ) and (self.request.repository.rsplit("/", 1)[-1].casefold() != self.repo.casefold()):
            raise ValueError("dirty direct request repository does not match the job")
        if not isinstance(self.descr, str) or not self.descr:
            raise ValueError("descr must be a non-empty string")


def github_request_issue(request: GitHubRequest) -> int | None:
    """Return the issue target carried by a closed request, if present."""
    value = getattr(request, "issue_number", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def github_request_pr(request: GitHubRequest) -> int | None:
    """Return the pull-request target carried by a closed request, if present."""
    value = getattr(request, "pr_number", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class PrReviewReconciled:
    """Result of one review reconciliation.

    ``unresolved_threads`` is the live GitHub thread list after the review was
    posted. ``body_only_findings`` are the findings that the review body carries
    because the diff cannot place them inline.
    """

    request: ReconcilePrReviewRequest
    unresolved_threads: FrozenJson
    body_only_findings: FrozenJson = field(default_factory=_empty_frozen_list)

    def __post_init__(self) -> None:
        """Validate the snapshots."""
        _json_root(self.unresolved_threads, list, "unresolved_threads")
        _json_root(self.body_only_findings, list, "body_only_findings")


@dataclass(frozen=True)
class MergeWaitCycleCompleted:
    """Immutable outcome of one merge admission/request cycle."""

    request: RunMergeWaitCycleRequest
    outcome: str
    attempted: bool
    retryable: bool = False
    merge_sha: str | None = None

    def __post_init__(self) -> None:
        """Validate merge-cycle outcome metadata."""
        if not isinstance(self.outcome, str) or not self.outcome:
            raise ValueError("outcome must be a non-empty string")
        if not isinstance(self.attempted, bool) or not isinstance(self.retryable, bool):
            raise ValueError("attempted and retryable must be booleans")
        if self.merge_sha is not None and (
            not isinstance(self.merge_sha, str)
            or len(self.merge_sha) not in (40, 64)
            or any(character not in "0123456789abcdef" for character in self.merge_sha)
        ):
            raise ValueError("merge_sha must be a full commit SHA or None")


type GitHubReceipt = (
    RateBudgetRead
    | CurrentPlanScopeRead
    | RepositoryValidationCIRead
    | DirtyDirectPrStateRead
    | RebaseConflictInspected
    | PrReviewReconciled
    | MergeWaitCycleCompleted
)


class GitHubJobRunner(Protocol):
    """Executes closed GitHub requests with job-scoped accessors."""

    @property
    def gh_timeout(self) -> int:
        """Return the operation timeout supplied by the runner."""
        raise NotImplementedError

    def run(
        self,
        job: GitHubJob,
        *,
        shutdown: threading.Event | None = None,
        deadline_s: float | None = None,
    ) -> GitHubReceipt:
        """Execute one closed GitHub request and return its immutable receipt."""
        raise NotImplementedError
