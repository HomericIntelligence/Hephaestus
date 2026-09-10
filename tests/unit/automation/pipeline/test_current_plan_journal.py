"""Test current journal authority without retired archive recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.issue_timeline import plan_issue_timeline_compaction
from hephaestus.automation.pipeline.plan_journal import reconcile_plan_journal
from hephaestus.automation.plan_review_session import (
    PlanReviewSessionLostError,
    PlanReviewSessionStore,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    journal_snapshot,
    render_current_plan,
    render_current_review,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_ARCHIVE = (
    "<!-- hephaestus-plan-history:revision=1:kind=plan -->\n"
    "<!-- hephaestus-plan-history:old-plan -->\nPlan v1\n"
    "<!-- hephaestus-plan-history:new-plan -->\nPlan v2"
)


def _owned(body: str, database_id: int) -> IssueComment:
    """Return a comment with explicit host ownership."""
    return IssueComment(body=body, viewer_did_author=True, database_id=database_id)


def test_retired_archive_cannot_replace_a_current_plan() -> None:
    """An old archive supplies no current publication authority."""
    github = FakeStageGitHub()
    github.comments[601] = [
        _owned(render_current_plan("Plan v1"), 1),
        _owned(render_current_review("Review v1", revision=1), 2),
        _owned(_ARCHIVE, 3),
    ]

    comments = reconcile_plan_journal(601, github)

    assert journal_snapshot(comments).current_plan == "Plan v1"
    assert github.mutation_log == []


def test_retired_archive_conflicts_do_not_override_current_comments() -> None:
    """Only current canonical comments can conflict as journal authority."""
    comments = [
        _owned(render_current_plan("Current plan", revision=3), 1),
        _owned(render_current_review("Current review", revision=3), 2),
        _owned(_ARCHIVE, 3),
        _owned(_ARCHIVE.replace("Plan v2", "Other old plan"), 4),
    ]

    snapshot = journal_snapshot(comments)

    assert snapshot.current_plan == "Current plan"
    assert snapshot.revision == 3


def test_timeline_does_not_delete_or_import_retired_archives() -> None:
    """Old archive text remains stored and grants no current fingerprint."""
    comments = [
        _owned(render_current_plan("Plan v2", revision=2), 1),
        _owned(render_current_review("Current review", revision=2), 2),
        _owned(_ARCHIVE, 3),
    ]

    result = plan_issue_timeline_compaction(comments)

    assert 3 not in result.delete_comment_ids
    assert result.plan_body == render_current_plan("Plan v2", revision=2)


def test_raw_text_cannot_supply_comment_ownership() -> None:
    """A comment read must include observed ownership metadata."""
    with pytest.raises(TypeError):
        journal_snapshot([render_current_plan("Unattributed plan")])  # type: ignore[list-item]


def test_direct_provider_rejects_unversioned_model_metadata(tmp_path: Path) -> None:
    """An old provider record cannot act as a current session record."""
    store = PlanReviewSessionStore(lambda: tmp_path)
    store.start_cycle(
        repo="org/repo",
        issue=602,
        provider="codex",
        model="reviewer",
        reviewer_config={"reasoning_effort": "medium"},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="current-plan",
    )

    with pytest.raises(PlanReviewSessionLostError):
        store.recover_active(repo="org/repo", issue=602)
