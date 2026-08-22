"""Behavior tests for strict automation-owned issue timeline compaction."""

from __future__ import annotations

import pytest

from hephaestus.automation.issue_timeline import (
    issue_comments_from_metadata,
    plan_issue_timeline_compaction,
)
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.requirements_recovery import (
    OBSOLETE_EXPLANATION_MARKER,
    render_obsolete_explanation,
    render_recovered_requirements,
)
from hephaestus.automation.review_journal import (
    FORCED_PLANNING_EPOCH_MARKER,
    HISTORY_MARKER,
    IssueComment,
    archive_plan_body,
    archive_review_body,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import SKIP_REASON_MARKER


def _comment(
    database_id: int,
    body: str,
    *,
    owned: bool = True,
) -> IssueComment:
    return IssueComment(
        body=body,
        viewer_did_author=owned,
        database_id=database_id,
    )


def test_compaction_keeps_only_latest_owned_plan_and_review() -> None:
    """Human text is inert while all obsolete automation artifacts are removed."""
    handoff = "<!-- hephaestus-implementation-reply-handoff:pr=22:head=" + "a" * 40
    handoff += ":batch=" + "b" * 32 + " -->\n<!-- {} -->"
    comments = [
        _comment(1, "Human clarification", owned=False),
        _comment(2, render_current_plan("Plan v1", revision=1)),
        _comment(3, render_current_review("NOGO", revision=1)),
        _comment(4, archive_plan_body(1, "Plan v1", "Plan v2")),
        _comment(5, archive_review_body(1, "NOGO")),
        _comment(6, render_current_plan("Plan v2", revision=2)),
        _comment(7, render_current_review("GO", revision=2)),
        _comment(8, f"{SKIP_REASON_MARKER}\nold reason"),
        _comment(9, handoff),
        _comment(10, f"{HISTORY_MARKER.format(revision=9, kind='review')}\nforeign", owned=False),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.plan_body is not None
    assert result.plan_body.startswith(PLAN_CANONICAL_MARKER)
    assert "Plan v2" in result.plan_body
    assert result.review_body is not None
    assert result.review_body.startswith(PLAN_REVIEW_CANONICAL_MARKER)
    assert "GO" in result.review_body
    assert result.delete_comment_ids == (2, 3, 4, 5, 8, 9)


def test_compaction_preserves_forced_planning_epoch_marker() -> None:
    """A compacted forced plan remains restart authority for --force."""
    comments = [
        _comment(1, render_current_plan("Old plan", revision=1)),
        _comment(
            2,
            render_current_plan(
                "Forced replacement",
                revision=2,
                forced_planning_epoch=True,
            ),
        ),
        _comment(3, render_current_review("Pending", revision=2)),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.plan_body is not None
    assert FORCED_PLANNING_EPOCH_MARKER in result.plan_body


def test_compaction_preserves_recovery_source_epoch_marker() -> None:
    """Compaction cannot make a recovered plan look older than its source."""
    source_digest = "c" * 64
    comments = [
        _comment(
            1,
            render_current_plan(
                "Recovered plan",
                revision=2,
                recovery_source_digest=source_digest,
            ),
        ),
        _comment(2, render_current_review("Pending", revision=2)),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.plan_body is not None
    assert source_digest in result.plan_body


def test_compaction_never_claims_or_deletes_foreign_marker_comments() -> None:
    """Marker text alone cannot authorize mutation of another actor's comment."""
    comments = [
        _comment(1, render_current_plan("Foreign plan", revision=1), owned=False),
        _comment(2, render_current_review("Foreign review", revision=1), owned=False),
    ]

    assert plan_issue_timeline_compaction(comments).delete_comment_ids == ()


def test_compaction_keeps_same_line_history_marker_lookalikes() -> None:
    """A valid marker with a same-line suffix is actor-owned prose, not history."""
    lookalike = archive_plan_body(1, "old", "new").replace(" -->\n", " -->suffix\n", 1)
    comments = [
        _comment(1, render_current_plan("Plan", revision=1)),
        _comment(2, lookalike),
    ]

    assert 2 not in plan_issue_timeline_compaction(comments).delete_comment_ids


def test_compaction_keeps_same_line_skip_marker_lookalikes() -> None:
    """A skip marker with a same-line suffix is inert operator text."""
    comments = [
        _comment(1, render_current_plan("Plan", revision=1)),
        _comment(2, f"{SKIP_REASON_MARKER}suffix\nkeep me"),
    ]

    assert 2 not in plan_issue_timeline_compaction(comments).delete_comment_ids


def test_metadata_ownership_requires_viewer_proof_or_exact_login() -> None:
    """REST metadata cannot turn a foreign marker into an owned deletion."""
    metadata = [
        {"id": 1, "body": "mine", "viewerDidAuthor": True, "user": {"login": "else"}},
        {"id": 2, "body": "also mine", "user": {"login": "HephaestusBot"}},
        {"id": 3, "body": "foreign", "viewerDidAuthor": False, "user": {"login": "me"}},
    ]

    comments = issue_comments_from_metadata(metadata, viewer_login="hephaestusbot")

    assert [comment.viewer_did_author for comment in comments] == [True, True, False]
    assert [comment.database_id for comment in comments] == [1, 2, 3]


def test_malformed_legacy_marker_fails_before_planning_deletion() -> None:
    """A prefix collision is not enough evidence to delete an owned comment."""
    comments = [
        _comment(1, render_current_plan("Plan", revision=1)),
        _comment(2, "<!-- hephaestus-plan-history:not-valid -->\nkeep me"),
    ]

    with pytest.raises(RuntimeError, match="malformed legacy automation marker"):
        plan_issue_timeline_compaction(comments)


def test_existing_canonical_pointer_ignores_newer_legacy_comment() -> None:
    """Historical heading-only text remains inert beside the canonical pointer."""
    comments = [
        _comment(1, render_current_plan("Older canonical", revision=1)),
        _comment(2, f"{PLAN_COMMENT_MARKER}\n\nLatest legacy plan"),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.plan_body is not None
    assert "Older canonical" in result.plan_body
    assert result.plan_needs_update is False
    assert result.delete_comment_ids == ()


def test_compaction_retains_history_without_a_canonical_pointer() -> None:
    """A sole recoverable archive is never deleted before a pointer exists."""
    comments = [
        _comment(1, archive_plan_body(1, "Plan v1", "Plan v2")),
        _comment(2, f"{PLAN_COMMENT_MARKER}\n\nHeading-only text"),
        _comment(3, "Unrelated operator note"),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.plan_body is None
    assert result.review_body is None
    assert result.delete_comment_ids == ()


def test_compaction_retains_plan_archive_until_its_exact_successor_is_canonical() -> None:
    """A rev-1 pointer cannot replace recovery data for its missing rev-2 successor."""
    comments = [
        _comment(1, render_current_plan("Plan v1", revision=1)),
        _comment(2, render_current_review("Review v1", revision=1)),
        _comment(3, archive_plan_body(1, "Plan v1", "Plan v2")),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.delete_comment_ids == ()


def test_compaction_retains_archive_pair_until_both_revision_successors_exist() -> None:
    """A first pass cannot strand review history by deleting its plan evidence."""
    comments = [
        _comment(1, render_current_plan("Plan v2", revision=2)),
        _comment(2, render_current_review("Review v1", revision=1)),
        _comment(3, archive_plan_body(1, "Plan v1", "Plan v2")),
        _comment(4, archive_review_body(1, "Review v1")),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.delete_comment_ids == ()

    comments[1] = _comment(2, render_current_review("Review v2", revision=2))

    recovered = plan_issue_timeline_compaction(comments)

    assert recovered.delete_comment_ids == (3, 4)


def test_compaction_deletes_verified_plan_and_review_history_together() -> None:
    """Both archives compact only after the matching successor pair is durable."""
    comments = [
        _comment(1, render_current_plan("Plan v2", revision=2)),
        _comment(2, render_current_review("Review v2", revision=2)),
        _comment(3, archive_plan_body(1, "Plan v1", "Plan v2")),
        _comment(4, archive_review_body(1, "Review v1")),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.delete_comment_ids == (3, 4)


def test_compaction_deletes_complete_three_revision_history_chain() -> None:
    """A fully proven chain compacts atomically instead of stranding rev 1."""
    comments = [
        _comment(1, render_current_plan("Plan v3", revision=3)),
        _comment(2, render_current_review("Review v3", revision=3)),
        _comment(3, archive_plan_body(1, "Plan v1", "Plan v2")),
        _comment(4, archive_review_body(1, "Review v1")),
        _comment(5, archive_plan_body(2, "Plan v2", "Plan v3")),
        _comment(6, archive_review_body(2, "Review v2")),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.delete_comment_ids == (3, 4, 5, 6)


def test_compaction_keeps_one_valid_recovery_or_obsolete_role_per_issue() -> None:
    """Actor-owned recovery roles stay bounded without touching foreign comments."""
    recovery = render_recovered_requirements("derived body", "Recovered requirements", "a" * 64)
    obsolete = render_obsolete_explanation("Superseded by #42")
    comments = [
        _comment(1, recovery),
        _comment(2, recovery),
        _comment(3, obsolete),
        _comment(4, obsolete),
        _comment(5, recovery, owned=False),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert result.delete_comment_ids == (1, 3)
    assert OBSOLETE_EXPLANATION_MARKER in comments[3].body


def test_malformed_owned_recovery_provenance_fails_before_deletion() -> None:
    """A bad owned recovery marker requires manual review rather than deletion."""
    comments = [
        _comment(1, "<!-- hephaestus-recovered-requirements:v=1:source=bad -->\ntext"),
        _comment(2, render_obsolete_explanation("Superseded by #42")),
    ]

    with pytest.raises(RuntimeError, match="malformed recovered requirements marker"):
        plan_issue_timeline_compaction(comments)
