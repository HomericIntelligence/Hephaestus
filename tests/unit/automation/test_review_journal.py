"""Pure durable plan-review journal behavior."""

from __future__ import annotations

from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    MAX_AGENT_HISTORY_CHARS,
    IssueComment,
    archive_plan_body,
    archive_review_body,
    history_projection,
    journal_snapshot,
    render_current_plan,
    render_current_review,
    review_state,
    trusted_feedback_after_block,
)


def test_review_state_rejects_legacy_github_verdicts() -> None:
    """Legacy free-text verdicts cannot influence the label-backed workflow."""
    assert review_state("analysis\n\nVerdict: GO") == "unparseable"
    assert review_state("analysis\n\n**Verdict: NO-GO** — revise") == "unparseable"


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


def test_projection_preserves_plan_review_chronology() -> None:
    """Archived plan-review pairs precede the current actionable revision."""
    comments = [
        _owned(render_current_plan("Plan 3", revision=3)),
        _owned(render_current_review("Review 3\n\nstate:plan-no-go", revision=3)),
    ]
    for revision in (1, 2):
        comments.extend(
            [
                _owned(archive_plan_body(revision, f"Plan {revision}", f"Plan {revision + 1}")),
                _owned(
                    archive_review_body(
                        revision,
                        f"Review {revision}\n\nstate:plan-no-go",
                    )
                ),
            ]
        )

    projection = history_projection(comments)

    assert projection.index("Previous Implementation Plan — Revision 1") < projection.index(
        "Review of Previous Plan — Revision 1"
    )
    assert projection.index("Review of Previous Plan — Revision 1") < projection.index(
        "Previous Implementation Plan — Revision 2"
    )
    assert projection.index("Review of Previous Plan — Revision 2") < projection.rindex("Plan 3")


def test_projection_bounds_prompt_without_truncating_github_journal() -> None:
    """Large journals become a bounded index while source comments remain intact."""
    large = "x" * 8_000
    comments = [_owned(render_current_plan("Current actionable plan", revision=10))]
    for revision in range(1, 10):
        comments.extend(
            [
                _owned(archive_plan_body(revision, large, f"{large}{revision}")),
                _owned(
                    archive_review_body(
                        revision,
                        f"Review {revision}: {large}\n\nstate:plan-no-go",
                    )
                ),
            ]
        )

    projection = history_projection(comments)

    assert len(projection) <= MAX_AGENT_HISTORY_CHARS
    assert "hephaestus-history-projection:truncated" in projection
    assert "Ordered revision index" in projection
    assert "Current actionable plan" in projection
    assert len(comments) == 19


def test_oversized_current_plan_keeps_index_fingerprint_and_explicit_excerpt() -> None:
    """A single oversized current artifact cannot tail-slice away its index."""
    comments = [_owned(render_current_plan("x" * 50_000, revision=7))]

    projection = history_projection(comments)

    assert len(projection) <= MAX_AGENT_HISTORY_CHARS
    assert "Ordered revision index" in projection
    assert "Revision 7" in projection
    assert "plan_sha=" in projection
    assert "Current plan excerpt" in projection


def test_only_trusted_human_feedback_resumes_a_blocked_plan() -> None:
    """Bots and outsiders cannot satisfy the blocked-plan feedback gate."""
    blocked = _owned(
        render_current_review(
            "Need an API decision.\n\nstate:plan-blocked",
            revision=1,
        )
    )
    bot = IssueComment(
        body="automated status",
        author_login="ci[bot]",
        author_association="MEMBER",
    )
    outsider = IssueComment(body="untrusted suggestion", author_login="stranger")
    maintainer = IssueComment(
        body="Use REST.",
        author_login="maintainer",
        author_association="MEMBER",
    )

    assert trusted_feedback_after_block([blocked, bot, outsider]) == ()
    assert trusted_feedback_after_block([blocked, bot, outsider, maintainer]) == (maintainer,)


def test_history_markers_are_revision_and_kind_specific() -> None:
    """One revision's plan and review use distinct append-once keys."""
    assert HISTORY_MARKER.format(revision=4, kind="plan") != HISTORY_MARKER.format(
        revision=4,
        kind="review",
    )
