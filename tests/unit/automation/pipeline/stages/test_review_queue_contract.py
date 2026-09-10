"""Review admission and session ownership across queue retries."""

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.pipeline.jobs import CompactJob
from hephaestus.automation.pipeline.reply_handoff import (
    PENDING_IMPLEMENTATION_REPLY_HANDOFF,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES,
    PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
    journaled_implementation_reply_handoff,
    retry_pending_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import Continue, JobRequest, StageOutcome
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.review_audit import ReviewAudit
from hephaestus.automation.review_journal import IssueComment
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def clean_audit() -> ReviewAudit:
    """Return the completed source audit used by these queue cases."""
    return ReviewAudit(
        grade="A",
        verdict="GO",
        summary="Clean source review",
        findings=(),
        raw_feedback="",
        valid=True,
    )


@pytest.mark.parametrize("state", ["ENTER", "GO_AUDIT_RECEIPT", "GO_AUDIT_PUBLISH"])
def test_recovered_publication_requires_fresh_review(
    state: str, make_ctx: Any, make_work_item: Any
) -> None:
    """A recovered publication record cannot restore active review evidence."""
    github = FakeStageGitHub(unresolved=[(0, 0)], pr_impl_state=(True, False))
    item = make_work_item(issue=1, pr=1001, state=state)
    item.payload.update(
        pending_implementation_go_audit=clean_audit(),
        pending_implementation_go_audit_head="a" * 40,
        pending_implementation_go_label_confirmed=True,
    )

    result = PrReviewStage().step(item, make_ctx(github=github))

    assert result == Continue(next_state="ENTER")
    assert "reviewed_pr_head_sha" not in item.payload
    assert "pending_implementation_go_audit" not in item.payload
    assert "pending_implementation_go_label_confirmed" not in item.payload
    assert github.mutation_log == []


def test_review_retry_compacts_only_the_reviewer(make_ctx: Any, make_work_item: Any) -> None:
    """A review retry cannot dispatch an implementation session operation."""
    item = make_work_item(issue=1, pr=1001, state="COMPACT_REVIEWER_WAIT")
    item.worktree = "/tmp/review-worktree"
    item.session_ids["pr-reviewer"] = "review-session"
    item.session_ids["implementer"] = "writer-session"

    result = PrReviewStage().step(item, make_ctx())

    assert isinstance(result, JobRequest)
    assert isinstance(result.job, CompactJob)
    assert result.job.session_agent == "pr-reviewer"
    assert result.job.session_id == "review-session"
    assert result.on_done_state == "REVIEW_WAIT"


@pytest.mark.parametrize(
    "state", ["PUSH_WAIT", "ADDRESS_WAIT", "RECOVERY_REPLY_WAIT", "COMPACT_WRITER_WAIT"]
)
def test_retired_review_states_are_rejected(state: str, make_ctx: Any, make_work_item: Any) -> None:
    """Removed writer states cannot dispatch a job or enter another queue."""
    github = FakeStageGitHub()
    item = make_work_item(issue=1, pr=1001, state=state)

    result = PrReviewStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_FAIL, f"unknown state: {state}")
    assert github.mutation_log == []


@pytest.mark.parametrize("journal_format", [1, 2])
def test_reply_recovery_requires_the_current_armed_journal(journal_format: int) -> None:
    """Retired journals cannot restore reply work; current records remain readable."""
    threads = [
        {
            "id": "thread-1",
            "path": "a.py",
            "line": 3,
            "side": "RIGHT",
            "comments": [{"id": "comment-1", "author": "reviewer", "body": "Fix the guard."}],
        }
    ]
    handoff = implementation_reply_handoff(
        "a" * 40, threads, {"thread-1": "The guard is fixed."}, "b" * 32
    )
    entry = implementation_reply_handoff_journal_entry(1001, handoff)
    assert entry is not None
    marker, body = entry
    payload = json.loads(body.split("\n", 1)[1].removeprefix("<!-- ").removesuffix(" -->"))
    payload["format"] = journal_format
    if journal_format == 1:
        payload.pop("armed")
    comment = IssueComment(body=f"{marker}\n<!-- {json.dumps(payload)} -->", viewer_did_author=True)

    if journal_format == 1:
        with pytest.raises(ValueError, match="journal identity is invalid"):
            journaled_implementation_reply_handoff([comment], pr_number=1001, threads=threads)
    else:
        recovered = journaled_implementation_reply_handoff(
            [comment], pr_number=1001, threads=threads
        )
        assert recovered is not None
        assert recovered["reconciliation_only"] is True


def test_reply_visibility_retries_stop_without_losing_the_journal() -> None:
    """A repeated visibility delay stops replay and keeps the durable journal."""
    handoff = implementation_reply_handoff(
        "a" * 40,
        [{"id": "thread-1"}],
        {"thread-1": "The guard is fixed."},
        "b" * 32,
    )
    assert handoff is not None
    journal = {"marker": "accepted reply intent", "body": "retained journal"}
    payload: dict[str, Any] = {
        PENDING_IMPLEMENTATION_REPLY_HANDOFF: handoff,
        PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL: journal,
    }
    github = SimpleNamespace(
        gh_pr_state=lambda _pr: {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None},
        post_implementation_thread_replies=lambda *_args, **_kwargs: SimpleNamespace(
            visibility_lag=True, replied_thread_ids=(), receipts=()
        ),
    )

    results = [
        retry_pending_implementation_reply_handoff(
            payload,
            pr_number=1001,
            issue_number=1,
            github=github,
            logger=logging.getLogger(__name__),
        )
        for _ in range(3)
    ]

    assert results == ["visibility_wait", "visibility_wait", "stale"]
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF not in payload
    assert payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL] is journal


def test_review_publication_requires_the_checkout_diff() -> None:
    """A caller must supply its reviewed snapshot, including during a dry run."""
    github = PipelineGitHub("org", repo="repo", dry_run=True)

    with pytest.raises(TypeError, match="review_diff"):
        github.post_review_threads(1001, [], expected_head_sha="a" * 40)  # type: ignore[call-arg]


@pytest.mark.parametrize("outcome", ["completed", "stale", "blocked"])
def test_reply_settlement_clears_all_actionable_retry_state(outcome: str) -> None:
    """A settled batch cannot give its visibility budget to a later batch."""
    handoff = implementation_reply_handoff(
        "a" * 40, [{"id": "thread-1"}], {"thread-1": "The guard is fixed."}, "b" * 32
    )
    assert handoff is not None
    journal = {"marker": "accepted reply intent", "body": "retained journal"}
    payload: dict[str, Any] = {
        PENDING_IMPLEMENTATION_REPLY_HANDOFF: handoff,
        PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL: journal,
    }
    replies = ("thread-1",) if outcome == "completed" else ()
    receipts = ({"id": "comment-1"},) if outcome == "completed" else ()
    results = iter(
        [
            SimpleNamespace(visibility_lag=True, replied_thread_ids=(), receipts=()),
            SimpleNamespace(
                replied_thread_ids=replies,
                receipts=receipts,
                outcome_unknown=outcome == "blocked",
            ),
        ]
    )
    github = SimpleNamespace(
        gh_pr_state=lambda _pr: {
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "autoMergeRequest": None,
        },
        post_implementation_thread_replies=lambda *_args, **_kwargs: next(results),
    )

    actual = [
        retry_pending_implementation_reply_handoff(
            payload,
            pr_number=1001,
            issue_number=1,
            github=github,
            logger=logging.getLogger(__name__),
        )
        for _ in range(2)
    ]

    assert actual == ["visibility_wait", outcome]
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF not in payload
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF_RETRIES not in payload
    assert PENDING_IMPLEMENTATION_REPLY_HANDOFF_VISIBILITY_RETRIES not in payload
    assert payload[PENDING_IMPLEMENTATION_REPLY_HANDOFF_JOURNAL] is journal
