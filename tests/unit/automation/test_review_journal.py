"""Pure durable plan-review journal behavior."""

from __future__ import annotations

from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    MAX_CURRENT_REVISION_CONTEXT_CHARS,
    IssueComment,
    archive_plan_body,
    archive_review_body,
    blocked_audit_recovery_body,
    current_plan_context,
    current_revision_context,
    journal_snapshot,
    render_current_plan,
    render_current_review,
)


def _owned(body: str) -> IssueComment:
    return IssueComment(body=body, author_login="hephaestus[bot]", viewer_did_author=True)


def test_snapshot_ignores_foreign_marker_spoofing() -> None:
    """Only comments proven to be actor-owned reconstruct canonical state."""
    comments = [
        IssueComment(body=render_current_plan("foreign"), author_login="attacker"),
        _owned(render_current_plan("owned", revision=2)),
        _owned(render_current_review("Looks good.\n\nstate:plan-go", revision=2)),
    ]

    snapshot = journal_snapshot(comments)

    assert snapshot.revision == 2
    assert snapshot.current_plan == "owned"
    assert snapshot.current_review.endswith("state:plan-go")


def test_current_revision_context_excludes_superseded_plan_and_review_artifacts() -> None:
    """Restart context keeps only the current rejected revision's instructions."""
    comments = [
        _owned(archive_plan_body(1, "Plan v1", "Plan v2")),
        _owned(archive_review_body(1, "Review v1\n\nstate:plan-no-go")),
        _owned(render_current_plan("Plan v2", revision=2)),
        _owned(render_current_review("Review v2\n\nstate:plan-no-go", revision=2)),
    ]

    context = current_revision_context(comments)

    assert "Plan v2" in context
    assert "Review v2" in context
    assert "Plan v1" not in context
    assert "Review v1" not in context


def test_current_revision_context_preserves_latest_review_when_plan_is_oversized() -> None:
    """A long plan cannot crowd out the immediate reviewer critique."""
    comments = [
        _owned(render_current_plan("x" * 50_000, revision=7)),
        _owned(render_current_review("Latest finding\n\nstate:plan-no-go", revision=7)),
    ]

    context = current_revision_context(comments)

    assert len(context) <= MAX_CURRENT_REVISION_CONTEXT_CHARS
    assert "Latest finding" in context
    assert "state:plan-no-go" in context


def test_current_plan_context_excludes_current_review_and_superseded_revisions() -> None:
    """Amendments receive the last plan separately from the direct critique."""
    comments = [
        _owned(archive_plan_body(1, "Plan v1", "Plan v2")),
        _owned(archive_review_body(1, "Review v1\n\nstate:plan-no-go")),
        _owned(render_current_plan("Plan v2", revision=2)),
        _owned(render_current_review("Review v2\n\nstate:plan-no-go", revision=2)),
    ]

    context = current_plan_context(comments)

    assert "Plan v2" in context
    assert "Plan v1" not in context
    assert "Review v1" not in context
    assert "Review v2" not in context


def test_current_plan_context_honors_its_explicit_size_budget() -> None:
    """An oversized previous plan cannot expand an amendment prompt unboundedly."""
    context = current_plan_context(
        [_owned(render_current_plan("x" * 2_000, revision=2))],
        max_chars=128,
    )

    assert len(context) <= 128
    assert "artifact excerpt truncated" in context


def test_history_markers_are_revision_and_kind_specific() -> None:
    """One revision's plan and review use distinct append-once keys."""
    assert HISTORY_MARKER.format(revision=4, kind="plan") != HISTORY_MARKER.format(
        revision=4,
        kind="review",
    )


def test_blocked_audit_recovery_repairs_missing_current_explanation() -> None:
    """A durable BLOCKED label can recover audit context without agent output."""
    body = blocked_audit_recovery_body([_owned(render_current_plan("Plan", revision=3))])

    assert body is not None
    assert "revision: 3" in body
    assert "interrupted audit write" in body
    assert body.endswith("state:plan-blocked")


def test_blocked_audit_recovery_preserves_existing_detailed_explanation() -> None:
    """Recovery never overwrites an already valid actor-owned BLOCKED review."""
    comments = [
        _owned(render_current_plan("Plan", revision=3)),
        _owned(
            render_current_review(
                "Waiting for the API owner to choose REST or GraphQL.\n\nstate:plan-blocked",
                revision=3,
            )
        ),
    ]

    assert blocked_audit_recovery_body(comments) is None
