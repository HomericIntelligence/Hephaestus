"""Pure durable plan-review journal behavior."""

from __future__ import annotations

import pytest

from hephaestus.automation.protocol import PLAN_REVIEW_PREFIX
from hephaestus.automation.review_journal import (
    HISTORY_MARKER,
    MAX_CURRENT_REVISION_CONTEXT_CHARS,
    CommentJournalReadError,
    IssueComment,
    PlanDiscoveryResult,
    PlanDiscoveryStatus,
    archive_plan_body,
    archive_review_body,
    blocked_audit_recovery_body,
    current_plan_context,
    current_revision_context,
    discover_plan_from_comments,
    is_journal_comment,
    is_plan_review_comment,
    journal_snapshot,
    normalize_issue_comments,
    render_current_plan,
    render_current_review,
    top_level_marker_occurrences,
)


def _owned(body: str) -> IssueComment:
    return IssueComment(body=body, author_login="hephaestus[bot]", viewer_did_author=True)


def test_plan_discovery_distinguishes_found_absent_and_read_error() -> None:
    """The result contract keeps successful absence separate from read failure."""
    found = discover_plan_from_comments([_owned(render_current_plan("Plan"))])
    absent = discover_plan_from_comments([])
    failed = PlanDiscoveryResult.read_error("offline")

    assert found.status is PlanDiscoveryStatus.FOUND
    assert found.plan_text is not None
    assert absent.status is PlanDiscoveryStatus.ABSENT
    assert failed.status is PlanDiscoveryStatus.READ_ERROR


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- HomericIntelligence:plan-issue -->",
        "<!-- hephaestus-plan:canonical -->",
        "<!-- athena:plan-issue -->",
    ],
)
def test_plan_discovery_accepts_shared_and_legacy_plan_markers(marker: str) -> None:
    """The shared marker works now while old actor-owned plans can migrate."""
    body = f"{marker}\n# Implementation Plan\n\nPlan"

    result = discover_plan_from_comments([_owned(body)])

    assert result.status is PlanDiscoveryStatus.FOUND
    assert result.plan_text == body


@pytest.mark.parametrize(
    ("body", "expected_status", "expected_plan"),
    [
        (
            "# Implementation Plan\n\nExisting plan context.\n"
            "<!-- HomericIntelligence:plan-issue -->\n\nPlan",
            PlanDiscoveryStatus.FOUND,
            "Existing plan context.\n\nPlan",
        ),
        (
            "# Implementation Plan\n\nExisting plan context.\n<!-- athena:plan-issue -->\n\nPlan",
            PlanDiscoveryStatus.FOUND,
            "Existing plan context.\n\nPlan",
        ),
        (
            "# Implementation Plan\r\n\r\nExisting plan context.\r\n"
            "<!-- athena:plan-issue -->\r\n\r\nPlan",
            PlanDiscoveryStatus.FOUND,
            "Existing plan context.\r\n\r\nPlan",
        ),
        (
            "```markdown\n<!-- HomericIntelligence:plan-issue -->\n```",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "> <!-- athena:plan-issue -->",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "- <!-- HomericIntelligence:plan-issue -->",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "    <!-- athena:plan-issue -->",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "Plan text <!-- HomericIntelligence:plan-issue -->",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "`<!-- athena:plan-issue -->`",
            PlanDiscoveryStatus.ABSENT,
            "",
        ),
        (
            "\n<!-- HomericIntelligence:plan-issue -->\n\nPlan",
            PlanDiscoveryStatus.FOUND,
            "Plan",
        ),
    ],
)
def test_plan_discovery_uses_top_level_markdown_marker_grammar(
    body: str,
    expected_status: PlanDiscoveryStatus,
    expected_plan: str,
) -> None:
    """Only exact top-level marker lines can identify plans during migration."""
    comments = [_owned(body)]
    result = discover_plan_from_comments(comments)

    assert result.status is expected_status
    assert journal_snapshot(comments).current_plan == expected_plan


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- HomericIntelligence:issue-review -->",
        "<!-- athena:issue-review -->",
    ],
)
def test_plan_review_detection_accepts_top_level_shared_and_legacy_markers(marker: str) -> None:
    """Top-level review markers keep the same migration grammar as plans."""
    body = f"{PLAN_REVIEW_PREFIX}\n\nPrior review context.\n{marker}\n\nReview"

    snapshot = journal_snapshot([_owned(body)])

    assert snapshot.current_review == "Prior review context.\n\nReview"


def test_top_level_marker_occurrences_preserve_repeated_marker_claims() -> None:
    """Identity resolution can detect repeated markers rather than select one."""
    marker = "<!-- HomericIntelligence:plan-issue -->"
    body = f"```markdown\n{marker}\n```\n\n{marker}\n\n{marker}"

    assert top_level_marker_occurrences(body, (marker,)) == (marker, marker)


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- HomericIntelligence:issue-review -->",
        "<!-- hephaestus-plan-review:canonical -->",
        "<!-- athena:issue-review -->",
    ],
)
def test_plan_review_detection_accepts_shared_and_legacy_markers(marker: str) -> None:
    """Review identity uses the same migration rule as plan identity."""
    assert is_plan_review_comment(f"{marker}\n## Review")


@pytest.mark.parametrize(
    "body",
    [
        f" \t{render_current_plan('spoofed plan')}",
        f"\n{render_current_review('spoofed review', revision=1)}",
    ],
)
def test_plan_discovery_rejects_whitespace_prefixed_journal_markers(body: str) -> None:
    """Only a marker at raw byte zero can identify a journal artifact."""
    result = discover_plan_from_comments([_owned(body)])

    assert result.status is PlanDiscoveryStatus.ABSENT


def test_normalization_derives_ownership_from_validated_logins() -> None:
    """Ownership comes from REST author metadata and the authenticated viewer."""
    comments = normalize_issue_comments(
        [
            {"body": render_current_plan("foreign"), "user": {"login": "other"}},
            {"body": render_current_plan("owned"), "user": {"login": "BOT"}},
        ],
        viewer_login="bot",
    )

    result = discover_plan_from_comments(comments)

    assert result.status is PlanDiscoveryStatus.FOUND
    assert result.plan_text is not None
    assert "owned" in result.plan_text


@pytest.mark.parametrize("body", [None, 1, {}, []])
def test_normalization_rejects_non_string_body(body: object) -> None:
    """Malformed comment bodies cannot become successful plan absence."""
    with pytest.raises(CommentJournalReadError, match="body was not a string"):
        normalize_issue_comments(
            [{"body": body, "user": {"login": "bot"}}],
            viewer_login="bot",
        )


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


def test_snapshot_rejects_whitespace_prefixed_markers_but_accepts_blank_lines() -> None:
    """Blank lines retain top-level identity, but marker-line whitespace is inert."""
    snapshot = journal_snapshot(
        [
            _owned(f" \t{render_current_plan('spoofed', revision=8)}"),
            _owned(f"\n{render_current_review('spoofed', revision=8)}"),
            _owned(f" {archive_plan_body(7, 'old', 'new')}"),
        ]
    )

    assert snapshot.revision == 1
    assert snapshot.current_plan == ""
    assert snapshot.current_review == "spoofed"
    assert snapshot.history == ()


def test_history_marker_requires_an_exact_first_line_boundary() -> None:
    """A same-line suffix cannot turn actor-owned prose into legacy state."""
    lookalike = archive_plan_body(7, "old", "new").replace(" -->\n", " -->suffix\n", 1)

    snapshot = journal_snapshot([_owned(lookalike)])

    assert is_journal_comment(lookalike) is False
    assert snapshot.revision == 1
    assert snapshot.history == ()


def test_crlf_canonical_and_history_markers_are_recognized() -> None:
    """Windows line endings preserve canonical and recovery identities."""
    plan = render_current_plan("Plan v2", revision=2).replace("\n", "\r\n")
    review = render_current_review("Review v2", revision=2).replace("\n", "\r\n")
    history = archive_plan_body(1, "Plan v1", "Plan v2").replace("\n", "\r\n")

    discovered = discover_plan_from_comments([_owned(plan)])
    snapshot = journal_snapshot([_owned(history), _owned(plan), _owned(review)])

    assert discovered.status is PlanDiscoveryStatus.FOUND
    assert snapshot.current_plan == "Plan v2"
    assert snapshot.current_review == "Review v2"
    assert len(snapshot.history) == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f" {render_current_plan('spoofed')}", False),
        (f"\n{render_current_review('spoofed', revision=1)}", True),
        (f"\t{archive_plan_body(1, 'old', 'new')}", False),
    ],
)
def test_journal_comment_respects_markdown_marker_placement(body: str, expected: bool) -> None:
    """Only exact top-level marker lines establish mutable journal identity."""
    assert is_journal_comment(body) is expected


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
