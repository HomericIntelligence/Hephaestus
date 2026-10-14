"""Behavioral tests for durable scope-expansion records."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.scope_expansion_records import (
    ScopeExpansionBlockingReviewRecord,
    ScopeExpansionChildIssueRecord,
    ScopeExpansionLifecycleRecord,
    parse_scope_expansion_blocking_review,
    parse_scope_expansion_child_body,
    parse_scope_expansion_lifecycle_comment,
    render_scope_expansion_blocking_review,
    render_scope_expansion_child_body,
    render_scope_expansion_lifecycle_comment,
)
from hephaestus.automation.scope_expansion_domain import ScopeExpansion, scope_expansion_digest

REPOSITORY = "org/repo"
PARENT_ISSUE = 17
PR_NUMBER = 29
HEAD_SHA = "a" * 40
CHILD_ISSUE = 31


def _expansion() -> ScopeExpansion:
    """Return one normalized scope expansion."""
    return ScopeExpansion(
        title="Extract shared helper",
        reason="The prerequisite must ship first",
        source_path="hephaestus/automation/example.py",
        source_line=17,
        required_paths=("hephaestus/automation/example.py",),
        acceptance_criteria=("The helper has behavior tests",),
    )


def test_child_body_round_trip_preserves_the_durable_contract() -> None:
    """A rendered child body can reconstruct its immutable record."""
    expansion = _expansion()
    body = render_scope_expansion_child_body(
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        expansion=expansion,
        child_issue_number=CHILD_ISSUE,
    )

    assert parse_scope_expansion_child_body(body) == ScopeExpansionChildIssueRecord(
        version=1,
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        digest=scope_expansion_digest(REPOSITORY, PARENT_ISSUE, expansion),
        expansion=expansion,
        child_issue_number=CHILD_ISSUE,
    )


def test_lifecycle_comment_round_trip_preserves_the_durable_contract() -> None:
    """A rendered lifecycle comment can reconstruct its immutable record."""
    expansion = _expansion()
    body = render_scope_expansion_lifecycle_comment(
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        expansion=expansion,
        state="blocked",
        child_issue_number=CHILD_ISSUE,
    )

    assert parse_scope_expansion_lifecycle_comment(body) == ScopeExpansionLifecycleRecord(
        version=1,
        state="blocked",
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        digest=scope_expansion_digest(REPOSITORY, PARENT_ISSUE, expansion),
        child_issue_number=CHILD_ISSUE,
    )


def test_blocking_review_round_trip_preserves_the_durable_contract() -> None:
    """A rendered blocking review can reconstruct its immutable record."""
    expansion = _expansion()
    body = render_scope_expansion_blocking_review(
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        child_issue_number=CHILD_ISSUE,
        expansion=expansion,
    )

    assert parse_scope_expansion_blocking_review(body) == ScopeExpansionBlockingReviewRecord(
        version=1,
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        child_issue_number=CHILD_ISSUE,
        digest=scope_expansion_digest(REPOSITORY, PARENT_ISSUE, expansion),
    )


@pytest.mark.parametrize(
    ("rendered", "parser"),
    [
        (
            lambda: render_scope_expansion_child_body(
                repository=REPOSITORY,
                parent_issue=PARENT_ISSUE,
                pr_number=PR_NUMBER,
                reviewed_head_sha=HEAD_SHA,
                expansion=_expansion(),
            ),
            parse_scope_expansion_child_body,
        ),
        (
            lambda: render_scope_expansion_lifecycle_comment(
                repository=REPOSITORY,
                parent_issue=PARENT_ISSUE,
                pr_number=PR_NUMBER,
                reviewed_head_sha=HEAD_SHA,
                expansion=_expansion(),
                state="pending-child",
            ),
            parse_scope_expansion_lifecycle_comment,
        ),
        (
            lambda: render_scope_expansion_blocking_review(
                repository=REPOSITORY,
                parent_issue=PARENT_ISSUE,
                pr_number=PR_NUMBER,
                reviewed_head_sha=HEAD_SHA,
                child_issue_number=CHILD_ISSUE,
                expansion=_expansion(),
            ),
            parse_scope_expansion_blocking_review,
        ),
    ],
)
def test_record_parsers_reject_unknown_fields(rendered: object, parser: object) -> None:
    """A record with an unowned field cannot enter the lifecycle."""
    assert callable(rendered)
    assert callable(parser)
    assert parser(f"{rendered()}\nUnexpected field: value") is None


def test_lifecycle_parser_rejects_incomplete_pending_review() -> None:
    """A pending-review record must bind one child."""
    body = render_scope_expansion_lifecycle_comment(
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        expansion=_expansion(),
        state="pending-review",
    )

    assert parse_scope_expansion_lifecycle_comment(body) is None


def test_lifecycle_projection_round_trip_is_bounded_and_exact() -> None:
    """A lifecycle record preserves the bounded retraction recovery input."""
    finding = {
        "path": "extra.py",
        "line": 1,
        "side": "RIGHT",
        "body": (
            "<!-- hephaestus-severity: major -->\n"
            '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->\n'
            "Remove the file."
        ),
    }
    body = render_scope_expansion_lifecycle_comment(
        repository=REPOSITORY,
        parent_issue=PARENT_ISSUE,
        pr_number=PR_NUMBER,
        reviewed_head_sha=HEAD_SHA,
        expansion=_expansion(),
        state="pending-child",
        retraction_findings=(finding,),
        review_diff="diff --git a/extra.py b/extra.py\n",
    )

    record = parse_scope_expansion_lifecycle_comment(body)

    assert record is not None
    assert record.retraction_findings == (finding,)
    assert record.review_diff == "diff --git a/extra.py b/extra.py\n"
    assert len(body.encode()) < 60_000


def test_lifecycle_projection_rejects_raw_data_that_would_overflow_after_encoding() -> None:
    """A large diff cannot produce an oversized GitHub lifecycle comment."""
    finding = {
        "path": "extra.py",
        "line": 1,
        "body": '<!-- hephaestus-scope-retraction-paths: ["extra.py"] -->',
    }

    with pytest.raises(ValueError, match="too large"):
        render_scope_expansion_lifecycle_comment(
            repository=REPOSITORY,
            parent_issue=PARENT_ISSUE,
            pr_number=PR_NUMBER,
            reviewed_head_sha=HEAD_SHA,
            expansion=_expansion(),
            state="pending-child",
            retraction_findings=(finding,),
            review_diff="x" * 40_000,
        )
