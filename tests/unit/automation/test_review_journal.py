"""Pure durable plan-review journal behavior."""

from __future__ import annotations

from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    MAX_AGENT_HISTORY_CHARS,
    MAX_CURRENT_REVISION_CONTEXT_CHARS,
    IssueComment,
    archive_plan_body,
    archive_review_body,
    blocked_audit_recovery_body,
    current_plan_context,
    current_revision_context,
    history_projection,
    journal_snapshot,
    plan_fingerprint,
    render_current_plan,
    render_current_review,
    review_state,
)


def test_review_state_rejects_legacy_github_verdicts() -> None:
    """Legacy free-text verdicts cannot influence the label-backed workflow."""
    assert review_state("analysis\n\nVerdict: GO") == "unparseable"
    assert review_state("analysis\n\n**Verdict: NO-GO** — revise") == "unparseable"


def test_review_state_uses_the_final_plan_token_not_a_forged_textual_decision() -> None:
    """Only the final plan-state token controls the label-backed outcome."""
    assert review_state("analysis\nVerdict: GO\nstate:plan-no-go") == "state:plan-no-go"


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


def test_projection_compacts_many_revision_reasons_without_raising() -> None:
    """The metadata index is bounded even after many verbose review rounds."""
    comments = [_owned(render_current_plan("Current plan " + "x" * 50_000, revision=81))]
    for revision in range(1, 81):
        comments.extend(
            [
                _owned(archive_plan_body(revision, f"Plan {revision}", f"Plan {revision + 1}")),
                _owned(
                    archive_review_body(
                        revision,
                        f"Review {revision}: {'reason ' * 200}\n\nstate:plan-no-go",
                    )
                ),
            ]
        )

    projection = history_projection(comments)

    assert len(projection) <= MAX_AGENT_HISTORY_CHARS
    assert "Revision 1" in projection
    assert "Revision 80" in projection
    assert "Current plan excerpt" in projection


def test_projection_indexes_the_plan_belonging_to_each_revision() -> None:
    """Revision N fingerprints its superseded plan, not revision N+1's recovery payload."""
    comments = [
        _owned(archive_plan_body(1, "Plan one", "Plan two")),
        _owned(archive_review_body(1, "Needs work.\n\nstate:plan-no-go")),
        _owned(render_current_plan("Plan two " + "x" * 2_000, revision=2)),
    ]

    projection = history_projection(comments, max_chars=1_000)

    revision_one = next(line for line in projection.splitlines() if "Revision 1:" in line)
    assert f"plan_sha={plan_fingerprint('Plan one')}" in revision_one


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


def test_projection_respects_tiny_explicit_budget() -> None:
    """Even an operator-supplied tiny budget returns a bounded projection."""
    projection = history_projection(
        [_owned(render_current_plan("x" * 2_000, revision=1))],
        max_chars=64,
    )

    assert len(projection) <= 64


def test_oversized_current_plan_keeps_index_fingerprint_and_explicit_excerpt() -> None:
    """A single oversized current artifact cannot tail-slice away its index."""
    comments = [_owned(render_current_plan("x" * 50_000, revision=7))]

    projection = history_projection(comments)

    assert len(projection) <= MAX_AGENT_HISTORY_CHARS
    assert "Ordered revision index" in projection
    assert "Revision 7" in projection
    assert "plan_sha=" in projection
    assert "Current plan excerpt" in projection


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
