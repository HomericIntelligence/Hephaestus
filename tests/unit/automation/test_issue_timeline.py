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
from hephaestus.automation.review_journal import (
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


def test_compaction_never_claims_or_deletes_foreign_marker_comments() -> None:
    """Marker text alone cannot authorize mutation of another actor's comment."""
    comments = [
        _comment(1, render_current_plan("Foreign plan", revision=1), owned=False),
        _comment(2, render_current_review("Foreign review", revision=1), owned=False),
    ]

    assert plan_issue_timeline_compaction(comments).delete_comment_ids == ()


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
