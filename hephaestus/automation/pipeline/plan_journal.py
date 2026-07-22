"""Transactional GitHub journal updates for implementation-plan revisions.

The canonical plan and review comments are mutable pointers to the current
revision.  Superseded plan/review pairs are immutable issue comments.  A plan
archive is written first because it contains the proposed next plan; that
recovery payload lets a restart finish the remaining review-archive and
canonical-plan writes without asking another agent to regenerate anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER, PLAN_REVIEW_PREFIX
from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    IssueComment,
    archive_plan_body,
    archive_review_body,
    archived_new_plan,
    archived_old_plan,
    is_pending_review,
    journal_snapshot,
    normalized_plan,
    plan_fingerprint,
    render_current_plan,
    render_pending_review,
)
from hephaestus.automation.state_labels import STATE_PLAN_NO_GO, is_exclusive_plan_state


class PlanJournalGitHub(Protocol):
    """Minimal GitHub mutation surface required by the journal transaction."""

    def issue_comments(self, issue_number: int) -> list[IssueComment]:
        """Return every issue comment in creation order."""
        pass

    def gh_issue_json(self, issue_number: int) -> dict[str, object]:
        """Return live issue metadata including the authoritative labels."""
        pass

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append one immutable, replay-safe issue comment."""
        pass

    def upsert_plan_comment(self, issue_number: int, plan: str) -> None:
        """Replace the actor-owned canonical plan comment."""
        pass

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Replace an actor-owned canonical issue comment."""
        pass


def _upsert_pending_review(
    issue_number: int,
    revision: int,
    github: PlanJournalGitHub,
) -> None:
    github.upsert_issue_comment(
        issue_number,
        PLAN_REVIEW_CANONICAL_MARKER,
        render_pending_review(revision=revision),
        legacy_marker=PLAN_REVIEW_PREFIX,
    )


def _confirm_publication(
    issue_number: int,
    expected_plan: str,
    expected_revision: int,
    github: PlanJournalGitHub,
) -> None:
    """Fail closed when a concurrent writer replaced the just-published pointer."""
    snapshot = journal_snapshot(github.issue_comments(issue_number))
    if (
        snapshot.revision != expected_revision
        or plan_fingerprint(snapshot.current_plan) != plan_fingerprint(expected_plan)
        or snapshot.current_review_revision != expected_revision
        or not is_pending_review(snapshot.current_review, revision=expected_revision)
    ):
        raise RuntimeError(
            f"concurrent plan journal write detected for revision {expected_revision}; "
            "manual recovery is required"
        )


@dataclass(frozen=True)
class PlanPublication:
    """Result of publishing or rejecting one proposed plan revision."""

    revision: int
    plan: str
    changed: bool
    no_progress_reason: str = ""

    @property
    def is_stuck(self) -> bool:
        """Return whether another automated planning iteration would not progress."""
        return bool(self.no_progress_reason)


def reconcile_plan_journal(issue_number: int, github: PlanJournalGitHub) -> list[IssueComment]:
    """Complete the newest interrupted plan-revision transaction, if possible."""
    comments = github.issue_comments(issue_number)
    snapshot = journal_snapshot(comments)
    plan_artifacts = [artifact for artifact in snapshot.history if artifact.kind == "plan"]
    if not plan_artifacts:
        if snapshot.current_plan and not snapshot.current_review:
            _upsert_pending_review(issue_number, snapshot.revision, github)
            return github.issue_comments(issue_number)
        return comments

    pending = plan_artifacts[-1]
    next_plan = archived_new_plan(pending.body)
    if not next_plan:
        return comments

    current_is_superseded = bool(snapshot.current_plan and snapshot.revision == pending.revision)
    current_is_missing = not snapshot.current_plan and snapshot.revision == pending.revision + 1
    current_is_next = bool(snapshot.current_plan and snapshot.revision == pending.revision + 1)
    if current_is_next:
        has_archived_review = any(
            artifact.kind == "review" and artifact.revision == pending.revision
            for artifact in snapshot.history
        )
        review_is_stale = snapshot.current_review_revision != snapshot.revision
        if has_archived_review and review_is_stale:
            _upsert_pending_review(issue_number, snapshot.revision, github)
            return github.issue_comments(issue_number)
        return comments
    if not (current_is_superseded or current_is_missing):
        return comments

    if snapshot.current_review_revision == pending.revision and snapshot.current_review:
        review_marker = HISTORY_MARKER.format(revision=pending.revision, kind="review")
        github.append_issue_comment(
            issue_number,
            review_marker,
            archive_review_body(pending.revision, snapshot.current_review),
        )
    elif not any(
        artifact.kind == "review" and artifact.revision == pending.revision
        for artifact in snapshot.history
    ):
        # Advancing without the paired review would erase the decision that
        # caused this revision, so leave the transaction visibly incomplete.
        return comments

    github.upsert_plan_comment(
        issue_number,
        render_current_plan(next_plan, revision=pending.revision + 1),
    )
    _upsert_pending_review(issue_number, pending.revision + 1, github)
    return github.issue_comments(issue_number)


def known_plan_fingerprints(comments: Sequence[IssueComment | str]) -> set[str]:
    """Return fingerprints for every current or historical plan in the journal."""
    snapshot = journal_snapshot(comments)
    plans = [snapshot.current_plan]
    for artifact in snapshot.history:
        if artifact.kind == "plan":
            plans.extend((archived_old_plan(artifact.body), archived_new_plan(artifact.body)))
    return {plan_fingerprint(plan) for plan in plans if plan.strip()}


def publish_plan_revision(
    issue_number: int,
    candidate: str,
    github: PlanJournalGitHub,
    *,
    require_change: bool,
) -> PlanPublication:
    """Publish a candidate using the append-pair-then-pointer transaction.

    Args:
        issue_number: Issue whose plan journal is updated.
        candidate: Newly generated plan text.
        github: Injected GitHub comment accessor.
        require_change: Whether equality with the current plan means the
            planner is stuck (true for amendments/replans) or an idempotent
            replay (false for initial publication/restart verification).

    Returns:
        The durable revision and whether the proposal made progress.

    Raises:
        RuntimeError: If a current plan would be superseded without its paired
            canonical review being available to archive.

    """
    comments = reconcile_plan_journal(issue_number, github)
    snapshot = journal_snapshot(comments)
    candidate_plan = normalized_plan(candidate)
    candidate_fingerprint = plan_fingerprint(candidate)
    current_fingerprint = plan_fingerprint(snapshot.current_plan)

    if not candidate_plan:
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan is empty; another automated planning iteration would not "
                "make progress. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if snapshot.current_plan and candidate_fingerprint == current_fingerprint:
        if not require_change:
            return PlanPublication(
                revision=snapshot.revision,
                plan=snapshot.current_plan,
                changed=False,
            )
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan is identical to the current plan "
                f"(fingerprint {candidate_fingerprint}); another automated planning iteration "
                "would repeat. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if candidate_fingerprint in known_plan_fingerprints(comments):
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan repeats a previous plan "
                f"(fingerprint {candidate_fingerprint}); another automated planning iteration "
                "would oscillate. An external actor must resolve the missing decision, "
                "requirement, or dependency and replace the blocked label to resume."
            ),
        )

    if not snapshot.current_plan:
        github.upsert_plan_comment(
            issue_number,
            render_current_plan(candidate_plan, revision=snapshot.revision),
        )
        _upsert_pending_review(issue_number, snapshot.revision, github)
        _confirm_publication(
            issue_number,
            candidate_plan,
            snapshot.revision,
            github,
        )
        return PlanPublication(
            revision=snapshot.revision,
            plan=candidate_plan,
            changed=True,
        )

    issue_data = github.gh_issue_json(issue_number)
    raw_labels = issue_data.get("labels", [])
    if not isinstance(raw_labels, list):
        raw_labels = []
    labels = {
        str(label.get("name")) if isinstance(label, dict) else str(label)
        for label in raw_labels
        if isinstance(label, (dict, str))
    }
    if not is_exclusive_plan_state(labels, STATE_PLAN_NO_GO):
        raise RuntimeError(
            f"cannot supersede plan revision {snapshot.revision} without an authoritative "
            f"exclusive {STATE_PLAN_NO_GO} label"
        )

    plan_marker = HISTORY_MARKER.format(revision=snapshot.revision, kind="plan")
    review_marker = HISTORY_MARKER.format(revision=snapshot.revision, kind="review")
    github.append_issue_comment(
        issue_number,
        plan_marker,
        archive_plan_body(snapshot.revision, snapshot.current_plan, candidate_plan),
    )
    github.append_issue_comment(
        issue_number,
        review_marker,
        archive_review_body(snapshot.revision, snapshot.current_review),
    )
    next_revision = snapshot.revision + 1
    github.upsert_plan_comment(
        issue_number,
        render_current_plan(candidate_plan, revision=next_revision),
    )
    _upsert_pending_review(issue_number, next_revision, github)
    _confirm_publication(issue_number, candidate_plan, next_revision, github)
    return PlanPublication(revision=next_revision, plan=candidate_plan, changed=True)
