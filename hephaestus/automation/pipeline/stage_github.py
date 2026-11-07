"""GitHub seam and auto-merge result types for pipeline stages.

This module defines :class:`StageGitHub`, the single-owner GitHub accessor
protocol every stage call receives through ``StageContext.github``, and
:class:`AutoMergeEnableResult`, the outcome of one request to enable GitHub
native auto-merge. Both moved here from the stage package's former base
module (issue #3516) so
the ``pipeline_github_transport`` and ``pipeline_github_jobs`` modules import
the seam from the pipeline layer instead of the stage package.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from hephaestus.automation.review_journal import IssueComment, PlanDiscoveryResult

from .github_jobs import ReadRepositoryValidationCIRequest, RepositoryValidationCIRead

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hephaestus.automation.dependency_parser import DependencyFact

__all__ = ["AutoMergeEnableResult", "StageGitHub"]


@dataclass(frozen=True)
class AutoMergeEnableResult:
    """Outcome of one request to enable GitHub native auto-merge.

    ``enabled`` is True only when the server confirmed the auto-merge request.
    ``already_enabled`` means the pull request already carried one, which is a
    success for the caller. ``transport_error`` means the outcome is unknown and
    ``malformed`` means the response arrived but could not be interpreted. The
    adapter never retries this mutation itself.
    """

    enabled: bool
    already_enabled: bool = False
    transport_error: bool = False
    malformed: bool = False
    dry_run: bool = False


@runtime_checkable
class StageGitHub(Protocol):
    """Single-owner GitHub accessor.

    ``StageContext.github`` is coordinator-thread-only and must never cross a
    worker boundary. ``GitHubJob`` workers create a fresh structurally
    conforming accessor per job and serialize same-repository operations.

    The single seam through which stages read GitHub facts and request
    durable mutations. Dry-run is honored INSIDE the accessor implementation
    (#1817): when the coordinator runs with ``--dry-run``, the mutator
    methods below log-and-skip the underlying ``gh`` calls, so stages never
    branch on ``ctx.dry_run`` around a write.

    Read surface mirrors the existing helper names; the mutator surface uses
    coordinator-neutral names the coordinator maps onto ``github_api``
    mutators (the pipeline architecture guard forbids ``github_api`` mutator
    names inside pipeline modules).
    """

    def operation_deadline(
        self, deadline_s: float, *, shutdown: threading.Event | None = None
    ) -> AbstractContextManager[None]:
        """Apply one deadline and cancellation signal to a service operation."""
        pass

    def read_repository_validation_ci(
        self, request: ReadRepositoryValidationCIRequest
    ) -> RepositoryValidationCIRead:
        """Collect bound repository CI evidence through the GitHub worker."""
        pass

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (mirrors ``github_api.issues.gh_issue_json``)."""
        pass

    def batch_dependency_facts(
        self,
        issue_numbers: Sequence[int],
        *,
        deadline_s: float,
        shutdown: threading.Event | None = None,
    ) -> tuple[DependencyFact, ...]:
        """Read complete dependency lifecycles in one repository-scoped batch.

        The accessor must reject duplicate, missing, mismatched, or
        contradictory nodes. It must not turn a partial or failed response
        into an empty successful result.
        """
        pass

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Return the merged PR closing this issue, if any (``_review_utils``)."""
        pass

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the merged PR for this issue, if any (tri-state seeding lookup)."""
        pass

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return an open PR covering this issue, if any (``_review_utils``)."""
        pass

    def find_issue_for_pr(self, pr_number: int) -> int | None:
        """Return the linked issue for this PR, if its body has ``Closes #N``."""
        pass

    def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
        """Return title/body/head metadata; checkout later derives the review diff."""
        pass

    def discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Return the tri-state actor-owned plan-discovery outcome."""
        pass

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return issue comments with ownership metadata in creation order."""
        pass

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (coordinator maps to ``gh_issue_add_labels``)."""
        pass

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (coordinator maps to ``gh_issue_remove_labels``)."""
        pass

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Durably add and remove labels in ONE atomic ``gh issue edit`` call.

        The single-transition primitive the skill mandates (one HTTP call so
        the issue never has zero or two state labels mid-window). The
        coordinator maps this onto a single
        ``gh issue edit --add-label ... --remove-label ...``. Prefer this over
        paired :meth:`add_labels`/:meth:`remove_labels` for any state:* swap.
        """
        pass

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        pass

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
    ) -> None:
        """Upsert an automation-owned comment keyed on an opaque marker."""
        pass

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append one immutable, replay-safe automation-owned journal comment."""
        pass

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the actor-owned plan comment using its opaque canonical marker.

        Durable plan-comment channel (doc section 2: "plan comment = durable
        artifact"). The coordinator maps this onto
        The human-readable heading remains for display, but heading-only
        historical comments are inert. Callers pass a body beginning with the
        opaque marker.
        """
        pass

    # -- implementation / pr_review surface (#1815) ------------------------

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Return the PR's head branch name (``_review_utils.get_pr_head_branch``)."""
        pass

    def pr_head_is_writable(self, pr_number: int) -> bool:
        """Return whether this loop can safely publish to the PR head branch.

        A fork head may be fetched for read-only review, but it must never be
        addressed by pushing a same-named branch to the base repository.
        """
        pass

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return ``(has_go, has_no_go)`` for the PR's implementation state labels.

        Mirrors ``pr_manager.pr_has_implementation_state_label`` — the
        existing-PR fast-path read the implementation GATE uses.
        """
        pass

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Return complete fresh snapshots of every unresolved review thread."""
        pass

    def open_prs_for_branch(self, branch_name: str) -> list[tuple[int, str]]:
        """Return every open branch PR, or fail when the read is incomplete."""
        pass

    def create_pr(
        self,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        *,
        strict_absence: bool = False,
    ) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        Backing (#1817): ``_review_utils.find_pr_for_issue`` first (reuse an
        existing open PR — the idempotence), then ``github_api.gh_pr_create``
        with the *given* ``title``/``body``. NOT ``pr_manager
        .ensure_pr_created``, which generates its own PR body and would
        discard the ``get_pr_description`` body the stage composed. PR
        creation is the implementation stage's journal entry (doc section 4:
        "Owned labels: PR creation is the journal entry").
        """
        pass

    def get_pr_body(self, pr_number: int) -> str:
        """Return the pull request's current body text.

        The caller reads the body to edit one section of it. A read failure
        raises so the caller can warn and continue without a guessed body.
        """
        pass

    def update_pr_body(self, pr_number: int, body: str) -> None:
        """Replace one pull request's body text.

        The edit keeps the pull request's exact ``Closes #N`` line; a body
        without it raises before any GitHub write.
        """
        pass

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Durably apply ``state:implementation-no-go`` to the PR.

        Mirrors ``pr_manager.mark_pr_implementation_no_go`` (adds the no-go
        label, removes any stale go label). Doc section 5 owned label:
        written on every non-authorizing review round before retry/regress.
        """
        pass

    def post_review(
        self,
        pr_number: int,
        inline: list[dict[str, Any]],
        body_only: list[dict[str, Any]],
        summary: str = "",
    ) -> None:
        """Post one review: inline comments on the diff, the rest in the review body.

        ``summary`` is the informational verdict and grade line for the body.
        """
        pass

    def apply_review_thread_decisions(
        self,
        pr_number: int,
        *,
        resolved_thread_ids: tuple[str, ...],
        feedback: dict[str, str],
    ) -> None:
        """Resolve the threads the reviewer accepted and answer the others."""
        pass

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Durably apply ``state:implementation-go`` to the PR.

        The PR review stage requests this only after complete structural audit
        facts, live thread facts, and an exact reviewed open/unarmed head pass
        its mutation guard. A post-write readback must still prove the same
        head and exclusive label. Audit prose, grades, CI, and secondary
        artifacts are informational rather than label authority.
        """
        pass

    # -- merge_wait surface (#1816) ------------------------------------------

    def reviewed_pr_state(self, pull_request_id: str) -> dict[str, Any] | None:
        """Read terminal state by the captured review node identity."""
        pass

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Read shared PR state for seed, stage, and merge decisions.

        Returns a PR lifecycle record (including ``state``, ``headRefOid``,
        ``mergedAt``, ``autoMergeRequest``, and ``baseRefName``), or ``None``
        on a transient read failure. The repo seed
        path and the implementation stage boundary use this read for
        merged/closed terminal-state checks before branch adoption or further
        routing. The merge_wait path uses the same contract to capture the
        head OID and classify merged, closed, and open lifecycle states.
        """
        pass

    def gh_pr_merge_readiness(self, pr_number: int) -> dict[str, Any] | None:
        """Read operational normal-merge readiness without granting authorization."""
        pass

    # -- repo-stage surface (#1817) -----------------------------------------

    def ensure_state_labels(self) -> None:
        """Durably ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1 ``ensure_state_labels``): the
        coordinator maps this onto ``github_api._ensure_labels_exist`` over
        the full ``state_labels`` vocabulary. Idempotent by construction.
        """
        pass
