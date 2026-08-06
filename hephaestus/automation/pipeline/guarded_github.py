"""Target-bound GitHub access for issue-bearing pipeline work items."""

# The proxy methods intentionally mirror the StageGitHub protocol signatures.
# ruff: noqa: D102, D105

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from hephaestus.automation.issue_guard import (
    GuardCredential,
    GuardError,
    GuardStore,
    IssueGuard,
    normalize_repository,
)
from hephaestus.automation.pipeline.stages.base import (
    ConditionalMergeResult,
    ImplementationThreadReplyResult,
    ReviewerThreadReconciliationResult,
    StageGitHub,
)
from hephaestus.automation.state_labels import STATE_IN_PROGRESS


class GuardTargetError(GuardError):
    """A worker attempted to use a credential for another target."""


@dataclass
class GuardedStageGitHub:
    """Proxy that confirms the issue guard immediately before each mutation."""

    raw: StageGitHub
    guard_store: GuardStore
    credential: GuardCredential

    def __post_init__(self) -> None:
        self.credential = GuardCredential(
            normalize_repository(self.credential.repository),
            self.credential.issue,
            self.credential.claim_id,
            self.credential.run_id,
        )

    def _issue(self, issue_number: int) -> None:
        if issue_number != self.credential.issue:
            raise GuardTargetError("GitHub issue target differs from guard credential")
        try:
            IssueGuard(self.guard_store).confirm(self.credential, timedelta(0))
        except GuardError:
            raise
        except Exception as exc:
            raise GuardTargetError("issue guard confirmation failed") from exc

    def _pr(self, pr_number: int) -> int:
        issue = self.raw.find_issue_for_pr(pr_number)
        if issue != self.credential.issue:
            raise GuardTargetError("PR-to-issue association differs from guard credential")
        self._issue(issue)
        return issue

    def __getattr__(self, name: str) -> Any:
        """Delegate read-only methods while keeping explicit mutator gates below."""
        return getattr(self.raw, name)

    def ensure_blocked_audit(self, issue_number: int) -> None:
        self._issue(issue_number)
        self.raw.ensure_blocked_audit(issue_number)

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        self._issue(issue_number)
        if STATE_IN_PROGRESS in labels:
            raise GuardTargetError("state:in-progress is owned exclusively by IssueGuard")
        self.raw.add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        self._issue(issue_number)
        if STATE_IN_PROGRESS in labels:
            raise GuardTargetError("state:in-progress is owned exclusively by IssueGuard")
        self.raw.remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        self._issue(issue_number)
        if STATE_IN_PROGRESS in {*add, *remove}:
            raise GuardTargetError("state:in-progress is owned exclusively by IssueGuard")
        self.raw.edit_labels(issue_number, add=add, remove=remove)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        self._issue(issue_number)
        self._pr(pr_number)
        self.raw.close_issue_as_covered(issue_number, pr_number)

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        self._issue(issue_number)
        self.raw.upsert_issue_comment(issue_number, marker, body, legacy_marker=legacy_marker)

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        self._issue(issue_number)
        self.raw.append_issue_comment(issue_number, marker, body)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        self._issue(issue_number)
        self.raw.upsert_plan_comment(issue_number, body)

    def post_implementation_thread_replies(
        self,
        pr_number: int,
        *,
        expected_head_sha: str,
        threads: list[dict[str, Any]],
        replies: dict[str, str],
        batch_nonce: str,
    ) -> ImplementationThreadReplyResult:
        self._pr(pr_number)
        return self.raw.post_implementation_thread_replies(
            pr_number,
            expected_head_sha=expected_head_sha,
            threads=threads,
            replies=replies,
            batch_nonce=batch_nonce,
        )

    def reconcile_reviewer_validated_threads(
        self,
        pr_number: int,
        *,
        reviewed_head_sha: str,
        receipts: list[dict[str, Any]],
        resolved_thread_ids: set[str],
        feedback: dict[str, str],
    ) -> ReviewerThreadReconciliationResult:
        self._pr(pr_number)
        return self.raw.reconcile_reviewer_validated_threads(
            pr_number,
            reviewed_head_sha=reviewed_head_sha,
            receipts=receipts,
            resolved_thread_ids=resolved_thread_ids,
            feedback=feedback,
        )

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        self._issue(issue_number)
        pr_number = self.raw.create_pr(issue_number, branch, title, body)
        linked_issue = self.raw.find_issue_for_pr(pr_number)
        if linked_issue != issue_number:
            raise GuardTargetError("created PR is not linked to the guarded issue")
        return pr_number

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        self._pr(pr_number)
        self.raw.mark_pr_implementation_no_go(pr_number)

    def post_review_threads(
        self,
        pr_number: int,
        threads: list[dict[str, Any]],
        *,
        expected_head_sha: str,
        review_diff: str | None = None,
    ) -> list[dict[str, Any]]:
        self._pr(pr_number)
        return self.raw.post_review_threads(
            pr_number,
            threads,
            expected_head_sha=expected_head_sha,
            review_diff=review_diff,
        )

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        self._pr(pr_number)
        self.raw.mark_pr_implementation_go(pr_number)

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        self._pr(pr_number)
        return self.raw.merge_pr_if_head(pr_number, reviewed_sha)

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        self._issue(issue_number)
        return self.raw.drive_green_learn_terminal(issue_number)

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        self._issue(issue_number)
        return self.raw.drive_green_learn_inflight(issue_number)

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        self._issue(issue_number)
        self._pr(pr_number)
        return self.raw.claim_drive_green_learn(issue_number, pr_number)

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        self._issue(issue_number)
        self.raw.mark_drive_green_learn_result(issue_number, succeeded=succeeded)

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        if set(epics_labels) != {self.credential.issue}:
            raise GuardTargetError("skip_epics batch must contain exactly the guarded issue")
        self._issue(self.credential.issue)
        self.raw.skip_epics(epics_labels)

    def ensure_state_labels(self) -> None:
        """Reject repository-wide provisioning through an issue guard."""
        raise GuardTargetError("ensure_state_labels is only available on the repo-stage accessor")


__all__ = ["GuardTargetError", "GuardedStageGitHub"]
