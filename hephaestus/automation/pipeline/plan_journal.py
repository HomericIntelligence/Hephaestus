"""Transactional canonical comments for implementation-plan revisions.

An issue has one mutable plan pointer and one mutable review pointer. Older
revisions are represented only by bounded plan fingerprints in hidden metadata;
legacy append-only artifacts are read solely to migrate interrupted runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    IssueComment,
    archived_new_plan,
    archived_old_plan,
    contains_raw_patch,
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

    def upsert_plan_comment(self, issue_number: int, plan: str) -> None:
        """Replace the actor-owned canonical plan comment."""
        pass

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
    ) -> None:
        """Replace an actor-owned canonical issue comment."""
        pass


class PlanRevisionOwnershipError(RuntimeError):
    """A different pipeline item owns the authoritative plan revision."""


def _upsert_pending_review(
    issue_number: int,
    revision: int,
    github: PlanJournalGitHub,
) -> None:
    github.upsert_issue_comment(
        issue_number,
        PLAN_REVIEW_CANONICAL_MARKER,
        render_pending_review(revision=revision),
    )


def _confirm_publication(
    issue_number: int,
    expected_plan: str,
    expected_revision: int,
    github: PlanJournalGitHub,
    *,
    forced_planning_epoch: bool,
    recovery_source_digest: str | None,
) -> None:
    """Fail closed when a concurrent writer replaced the just-published pointer."""
    snapshot = journal_snapshot(github.issue_comments(issue_number))
    if (
        snapshot.revision != expected_revision
        or plan_fingerprint(snapshot.current_plan) != plan_fingerprint(expected_plan)
        or snapshot.current_review_revision != expected_revision
        or not is_pending_review(snapshot.current_review, revision=expected_revision)
        or snapshot.forced_planning_epoch is not forced_planning_epoch
        or snapshot.recovery_source_digest != recovery_source_digest
    ):
        raise PlanRevisionOwnershipError(
            f"concurrent plan journal write detected for revision {expected_revision}; "
            "another pipeline item owns the published revision"
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
        if snapshot.current_plan and snapshot.current_review_revision != snapshot.revision:
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
        review_is_stale = snapshot.current_review_revision != snapshot.revision
        if review_is_stale:
            _upsert_pending_review(issue_number, snapshot.revision, github)
            return github.issue_comments(issue_number)
        return comments
    if not (current_is_superseded or current_is_missing):
        return comments

    prior_fingerprints = tuple(sorted(known_plan_fingerprints(comments)))
    github.upsert_plan_comment(
        issue_number,
        render_current_plan(
            next_plan,
            revision=pending.revision + 1,
            prior_fingerprints=prior_fingerprints,
            forced_planning_epoch=snapshot.forced_planning_epoch,
            recovery_source_digest=snapshot.recovery_source_digest,
        ),
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
    return {
        *snapshot.prior_plan_fingerprints,
        *(plan_fingerprint(plan) for plan in plans if plan.strip()),
    }


def publish_plan_revision(
    issue_number: int,
    candidate: str,
    github: PlanJournalGitHub,
    *,
    require_change: bool,
    forced_planning_epoch: bool = False,
    recovery_source_digest: str | None = None,
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
    recovery_epoch_changed = bool(
        recovery_source_digest and recovery_source_digest != snapshot.recovery_source_digest
    )

    if contains_raw_patch(candidate):
        return PlanPublication(
            revision=snapshot.revision,
            plan=snapshot.current_plan,
            changed=False,
            no_progress_reason=(
                "The proposed plan contains a raw patch or diff hunk. Public plans must "
                "describe intended changes without embedding source diffs; regenerate the "
                "complete plan before publication."
            ),
        )

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

    if (
        snapshot.current_plan
        and candidate_fingerprint == current_fingerprint
        and not recovery_epoch_changed
    ):
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

    if candidate_fingerprint in known_plan_fingerprints(comments) and not recovery_epoch_changed:
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
            render_current_plan(
                candidate_plan,
                revision=snapshot.revision,
                forced_planning_epoch=forced_planning_epoch,
                recovery_source_digest=recovery_source_digest,
            ),
        )
        _upsert_pending_review(issue_number, snapshot.revision, github)
        _confirm_publication(
            issue_number,
            candidate_plan,
            snapshot.revision,
            github,
            forced_planning_epoch=forced_planning_epoch,
            recovery_source_digest=recovery_source_digest,
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
        raise PlanRevisionOwnershipError(
            f"cannot supersede plan revision {snapshot.revision} without an authoritative "
            f"exclusive {STATE_PLAN_NO_GO} label"
        )

    next_revision = snapshot.revision + 1
    prior_fingerprints = (
        *snapshot.prior_plan_fingerprints,
        current_fingerprint,
    )
    github.upsert_plan_comment(
        issue_number,
        render_current_plan(
            candidate_plan,
            revision=next_revision,
            prior_fingerprints=prior_fingerprints,
            forced_planning_epoch=forced_planning_epoch,
            recovery_source_digest=recovery_source_digest,
        ),
    )
    _upsert_pending_review(issue_number, next_revision, github)
    _confirm_publication(
        issue_number,
        candidate_plan,
        next_revision,
        github,
        forced_planning_epoch=forced_planning_epoch,
        recovery_source_digest=recovery_source_digest,
    )
    return PlanPublication(revision=next_revision, plan=candidate_plan, changed=True)
