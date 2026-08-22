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


def test_restart_repairs_a_plan_written_before_pending_review() -> None:
    """A restart replaces a stale review after the revised plan became durable."""
    github = _CrashOnceJournalGitHub("pending_review")
    github.comments[7] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]

    with pytest.raises(RuntimeError, match="pending_review"):
        publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    reconcile_plan_journal(7, github)
    snapshot = journal_snapshot(github.issue_comments(7))

    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2 with rollback"
    assert snapshot.current_review_revision == 2
    assert snapshot.history == ()
    mutations_after_recovery = list(github.mutation_log)

    reconcile_plan_journal(7, github)

    assert github.mutation_log == mutations_after_recovery


def test_forced_publication_persists_restart_provenance() -> None:
    """The canonical plan records that its pending review belongs to force."""
    github = FakeStageGitHub()

    publish_plan_revision(
        10,
        "Fresh forced plan",
        github,
        require_change=False,
        forced_planning_epoch=True,
    )

    snapshot = journal_snapshot(github.issue_comments(10))
    assert snapshot.current_plan == "Fresh forced plan"
    assert snapshot.forced_planning_epoch is True


def test_recovery_publication_persists_source_epoch() -> None:
    """A canonical plan records which recovered source authorized its epoch."""
    github = FakeStageGitHub()
    source_digest = "a" * 64

    publish_plan_revision(
        11,
        "Fresh recovered plan",
        github,
        require_change=False,
        recovery_source_digest=source_digest,
    )

    snapshot = journal_snapshot(github.issue_comments(11))
    assert snapshot.current_plan == "Fresh recovered plan"
    assert snapshot.recovery_source_digest == source_digest


def test_recovery_epoch_can_republish_identical_plan_text() -> None:
    """New recovery provenance is progress even when the plan prose is unchanged."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[12] = [
        render_current_plan("Same valid plan", revision=1),
        render_current_review("Old source.\n\nstate:plan-no-go", revision=1),
    ]
    source_digest = "b" * 64

    publication = publish_plan_revision(
        12,
        "Same valid plan",
        github,
        require_change=True,
        recovery_source_digest=source_digest,
    )

    snapshot = journal_snapshot(github.issue_comments(12))
    assert publication.changed is True
    assert publication.revision == 2
    assert snapshot.current_plan == "Same valid plan"
    assert snapshot.recovery_source_digest == source_digest


def test_recovery_epoch_digest_change_is_progress_for_identical_plan_text() -> None:
    """A distinct recovered source starts a distinct plan epoch."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[13] = [
        render_current_plan("Same valid plan", revision=1, recovery_source_digest="a" * 64),
        render_current_review("Old source.\n\nstate:plan-no-go", revision=1),
    ]

    publication = publish_plan_revision(
        13,
        "Same valid plan",
        github,
        require_change=True,
        recovery_source_digest="b" * 64,
    )

    assert publication.changed is True
    assert publication.revision == 2
    assert journal_snapshot(github.issue_comments(13)).recovery_source_digest == "b" * 64


def test_same_recovery_epoch_digest_keeps_identical_plan_as_no_progress() -> None:
    """Identical prose in the same recovery epoch remains stuck."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[14] = [
        render_current_plan("Same valid plan", revision=1, recovery_source_digest="a" * 64),
        render_current_review("Needs work.\n\nstate:plan-no-go", revision=1),
    ]

    publication = publish_plan_revision(
        14,
        "Same valid plan",
        github,
        require_change=True,
        recovery_source_digest="a" * 64,
    )

    assert publication.changed is False
    assert publication.is_stuck is True


def test_epoch_marker_examples_in_plan_prose_are_not_metadata() -> None:
    """Only fixed host header positions carry restart authority."""
    recovery_example = f"<!-- hephaestus-recovery-source-epoch: {'c' * 64} -->"
    payload = (
        "Keep these examples literal:\n\n"
        "<!-- hephaestus-forced-planning-epoch -->\n\n"
        "```markdown\n"
        f"{recovery_example}\n"
        "```"
    )

    rendered = render_current_plan(payload, revision=3)
    snapshot = journal_snapshot([rendered])

    assert snapshot.current_plan == payload
    assert snapshot.forced_planning_epoch is False
    assert snapshot.recovery_source_digest is None


def test_failed_canonical_plan_write_preserves_the_previous_revision_for_retry() -> None:
    """A failed first write loses no public artifact and a clean retry can publish."""
    github = _CrashOnceJournalGitHub("canonical_plan")
    github.comments[7] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]

    with pytest.raises(RuntimeError, match="canonical_plan"):
        publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    preserved = journal_snapshot(github.issue_comments(7))
    assert preserved.revision == 1
    assert preserved.current_plan == "Plan v1"
    assert preserved.current_review == "Needs rollback."

    published = publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    assert published.revision == 2
    assert len(github.comments[7]) == 2


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


@pytest.mark.parametrize(
    "candidate",
    [
        "# Implementation Plan\n\n```diff\n-old\n+new\n```",
        "# Implementation Plan\n\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@",
    ],
)
def test_publication_rejects_raw_patch_content(candidate: str) -> None:
    """A generated patch can never become a public canonical plan comment."""
    github = FakeStageGitHub()

    result = publish_plan_revision(8, candidate, github, require_change=False)

    assert result.is_stuck
    assert "raw patch" in result.no_progress_reason
    assert github.comments.get(8, []) == []


def test_amendment_replaces_canonical_comments_without_public_history() -> None:
    """A revised plan leaves only the latest plan and latest review on the issue."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[8] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.\n\nstate:plan-no-go", revision=1),
    ]

    result = publish_plan_revision(8, "Plan v2 with rollback", github, require_change=True)

    assert result.changed
    assert len(github.comments[8]) == 2
    snapshot = journal_snapshot(github.issue_comments(8))
    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2 with rollback"
    assert snapshot.current_review_revision == 2
    assert snapshot.history == ()
    public_text = "\n".join(str(comment) for comment in github.comments[8])
    assert "Previous Implementation Plan" not in public_text
    assert "Review of Previous Plan" not in public_text
    assert "```diff" not in public_text


def test_legacy_interrupted_archive_recovers_without_appending_more_history() -> None:
    """Legacy crash recovery converges canonical pointers without new archives."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    legacy_archive = archive_plan_body(1, "Plan v1", "Plan v2")
    github.comments[8] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.\n\nstate:plan-no-go", revision=1),
        legacy_archive,
    ]

    reconcile_plan_journal(8, github)

    snapshot = journal_snapshot(github.issue_comments(8))
    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2"
    assert snapshot.current_review_revision == 2
    assert not any(name == "gh_issue_comment" for name, _args in github.mutation_log)
    assert not any(name == "append_issue_comment" for name, _args in github.mutation_log)


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

    with pytest.raises(RuntimeError, match=r"concurrent plan journal write.*another pipeline item"):
        publish_plan_revision(9, "Expected plan", github, require_change=False)
