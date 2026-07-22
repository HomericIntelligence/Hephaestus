"""Crash-recovery and no-progress tests for the durable plan journal."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.plan_journal import (
    publish_plan_revision,
    reconcile_plan_journal,
)
from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    IssueComment,
    archive_plan_body,
    journal_snapshot,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import STATE_PLAN_NO_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _CrashOnceJournalGitHub(FakeStageGitHub):
    """Inject one crash at a selected write in a plan-revision transaction."""

    def __init__(self, crash_on: str) -> None:
        super().__init__(labels=[STATE_PLAN_NO_GO])
        self.crash_on = crash_on
        self.crashed = False

    def _crash(self, target: str) -> None:
        if not self.crashed and self.crash_on == target:
            self.crashed = True
            raise RuntimeError(f"injected {target} crash")

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        if marker == HISTORY_MARKER.format(revision=1, kind="review"):
            self._crash("review_archive")
        super().append_issue_comment(issue_number, marker, body)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        if "<!-- revision: 2 -->" in body:
            self._crash("canonical_plan")
        super().upsert_plan_comment(issue_number, body)

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        if marker == PLAN_REVIEW_CANONICAL_MARKER and "revision 2" in body:
            self._crash("pending_review")
        super().upsert_issue_comment(
            issue_number,
            marker,
            body,
            legacy_marker=legacy_marker,
        )


@pytest.mark.parametrize("crash_on", ["review_archive", "canonical_plan", "pending_review"])
def test_restart_completes_each_interrupted_revision_write(crash_on: str) -> None:
    """Every write-success/next-write-failure window converges on restart."""
    github = _CrashOnceJournalGitHub(crash_on)
    github.comments[7] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]

    with pytest.raises(RuntimeError, match=crash_on):
        publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    reconcile_plan_journal(7, github)
    snapshot = journal_snapshot(github.issue_comments(7))

    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2 with rollback"
    assert snapshot.current_review_revision == 2
    assert [artifact.kind for artifact in snapshot.history] == ["plan", "review"]
    mutations_after_recovery = list(github.mutation_log)

    reconcile_plan_journal(7, github)

    assert github.mutation_log == mutations_after_recovery


def test_plan_oscillation_is_blocked_before_v1_is_republished() -> None:
    """A v1 -> v2 -> v1 cycle is detected from immutable journal history."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[8] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]
    publish_plan_revision(8, "Plan v2", github, require_change=True)
    before = list(github.comments[8])

    result = publish_plan_revision(8, "Plan v1", github, require_change=True)

    assert result.is_stuck
    assert "repeats a previous plan" in result.no_progress_reason
    assert github.comments[8] == before


def test_restart_rejects_conflicting_bodies_for_one_history_identity() -> None:
    """Divergent concurrent history creates require explicit manual recovery."""
    marker = HISTORY_MARKER.format(revision=1, kind="plan")
    comments = [
        IssueComment(
            body=archive_plan_body(1, "Plan v1", "Plan v2-A"),
            viewer_did_author=True,
        ),
        IssueComment(
            body=archive_plan_body(1, "Plan v1", "Plan v2-B"),
            viewer_did_author=True,
        ),
    ]

    with pytest.raises(RuntimeError, match=r"conflicting immutable.*manual recovery"):
        journal_snapshot(comments)

    assert all(comment.body.startswith(marker) for comment in comments)


def test_publication_rejects_concurrent_canonical_pointer_overwrite() -> None:
    """A writer cannot report success after another writer replaces its plan."""

    class CompetingWriterGitHub(FakeStageGitHub):
        def upsert_plan_comment(self, issue_number: int, body: str) -> None:
            super().upsert_plan_comment(issue_number, body)
            super().upsert_plan_comment(
                issue_number,
                render_current_plan("Competing plan", revision=1),
            )

    github = CompetingWriterGitHub()

    with pytest.raises(RuntimeError, match=r"concurrent plan journal write.*manual recovery"):
        publish_plan_revision(9, "Expected plan", github, require_change=False)
