"""Cross-tool plan reuse over the shared marker contract (#3307).

Either tool must read and reuse one valid plan/review pair written by the
other tool, and new writes must use only the current shared markers. These
tests pin that contract for the Athena and Hephaestus legacy aliases without
any network access.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.comment_identity import validate_planning_body_for_write
from hephaestus.automation.plan_consolidation import planning_role_inventory
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    PlanDiscoveryStatus,
    discover_plan_from_comments,
    extract_current_plan,
    is_plan_comment,
    journal_snapshot,
    render_current_plan,
)

ATHENA_PLAN_MARKER = "<!-- athena:plan-issue -->"
ATHENA_REVIEW_MARKER = "<!-- athena:issue-review -->"
HEPHAESTUS_PLAN_MARKER = "<!-- hephaestus-plan:canonical -->"
HEPHAESTUS_REVIEW_MARKER = "<!-- hephaestus-plan-review:canonical -->"


def _comment(body: str, comment_id: int, *, login: str = "mvillmow") -> IssueComment:
    return IssueComment(
        body=body,
        author_login=login,
        viewer_did_author=True,
        database_id=comment_id,
    )


def test_cross_alias_pair_discovers_the_plan() -> None:
    """An Athena-alias pair is a valid current plan for the shared reader."""
    comments = [
        _comment(
            f"{ATHENA_PLAN_MARKER}\n# Implementation Plan\n<!-- revision: 2 -->\n\nAthena plan\n",
            5444284559,
        ),
        _comment(
            f"{ATHENA_REVIEW_MARKER}\n{PLAN_REVIEW_PREFIX}\n"
            "<!-- revision: 2 -->\n\nstate:plan-go\n",
            5444310901,
        ),
    ]

    result = discover_plan_from_comments(comments)

    assert result.status is PlanDiscoveryStatus.FOUND
    assert result.plan_text is not None and "Athena plan" in result.plan_text


def test_journal_snapshot_pairs_cross_alias_plan_and_review() -> None:
    """The snapshot pairs plan and review comments written under any alias."""
    comments = [
        _comment(
            f"{ATHENA_PLAN_MARKER}\n# Implementation Plan\n\nCross plan\n",
            5444284559,
        ),
        _comment(
            f"{HEPHAESTUS_REVIEW_MARKER}\n{PLAN_REVIEW_PREFIX}\n"
            "<!-- revision: 2 -->\n\nCross review\n",
            5446247883,
        ),
    ]

    snapshot = journal_snapshot(comments)

    assert snapshot.current_plan == "Cross plan"
    assert snapshot.current_review == "Cross review"
    assert snapshot.current_review_revision == 2


def test_render_upgrades_a_legacy_plan_to_the_shared_marker() -> None:
    """Re-rendering a legacy plan body upgrades only the marker line."""
    legacy = f"{ATHENA_PLAN_MARKER}\n# Implementation Plan\n\nLegacy payload\n"

    rendered = render_current_plan(extract_current_plan(legacy), revision=2)

    assert rendered.startswith(PLAN_CANONICAL_MARKER)
    assert "Legacy payload" in rendered
    assert ATHENA_PLAN_MARKER not in rendered


@pytest.mark.parametrize(
    "body",
    [
        "```markdown\n<!-- athena:plan-issue -->\n```\n\nAfter the fence\n",
        "> <!-- athena:plan-issue -->\n\nQuoted lookalike\n",
        "Indented lookalike:\n\n    <!-- hephaestus-plan:canonical -->\n",
    ],
)
def test_quoted_and_fenced_markers_never_register(body: str) -> None:
    """A marker inside a fence, quote, or indent is inert audit text."""
    assert not is_plan_comment(body)
    assert planning_role_inventory([_comment(body, 1)]) == ()


def test_new_writes_reject_legacy_marker_bodies() -> None:
    """A write that carries only a legacy marker fails closed."""
    body = f"{ATHENA_PLAN_MARKER}\n# Implementation Plan\n\nBody\n"

    with pytest.raises(RuntimeError, match="shared marker"):
        validate_planning_body_for_write(ATHENA_PLAN_MARKER, body)


def test_new_writes_accept_only_the_shared_marker_body() -> None:
    """The shared marker round-trips through the write guard."""
    body = render_current_plan("Shared payload", revision=1)
    validate_planning_body_for_write(PLAN_CANONICAL_MARKER, body)
    review = (
        f"{PLAN_REVIEW_CANONICAL_MARKER}\n{PLAN_REVIEW_PREFIX}\n"
        "<!-- revision: 1 -->\n\nstate:plan-go\n"
    )
    validate_planning_body_for_write(PLAN_REVIEW_CANONICAL_MARKER, review)
