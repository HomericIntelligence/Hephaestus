"""Keep the visibility retry budget local to one reply batch."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.pipeline.github_jobs import DeliverReplyHandoffRequest, FrozenJson
from hephaestus.automation.pipeline.reply_handoff import (
    attempt_reply_handoff,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.pipeline.stages.base import ImplementationThreadReplyResult
from hephaestus.automation.review_journal import IssueComment

_THREADS = [
    {
        "id": "thread-1",
        "path": "a.py",
        "line": 1,
        "body": "Check the boundary.",
        "comments": [{"id": "comment-1", "body": "Check the boundary."}],
    }
]


def _handoff(head: str, nonce: str, *, recovered: bool = False) -> dict[str, Any]:
    """Build an active batch or recover its actual format-2 journal."""
    handoff = implementation_reply_handoff(
        head, _THREADS, {"thread-1": "[Response] The boundary is correct."}, nonce
    )
    assert handoff is not None
    if recovered:
        entry = implementation_reply_handoff_journal_entry(1001, handoff)
        assert entry is not None
        handoff = journaled_implementation_reply_handoff(
            [IssueComment(entry[1], viewer_did_author=True)],
            pr_number=1001,
            threads=_THREADS,
        )
        assert handoff is not None
        assert handoff["reconciliation_only"] is True
    return handoff


def _request(handoff: dict[str, Any], retries: int) -> DeliverReplyHandoffRequest:
    """Carry the previous receipt count into the next queue request."""
    return DeliverReplyHandoffRequest(1, 1001, FrozenJson.snapshot(handoff), retries)


class _ReplyGitHub:
    """Supply exact heads and reply outcomes without external calls."""

    def __init__(self, terminal: str = "completed") -> None:
        self.head = "a" * 40
        self.terminal = terminal
        self.reconciliations = 0
        self.posts = 0

    def gh_pr_state(self, pr_number: int) -> dict[str, object]:
        """Return complete live head evidence."""
        assert pr_number == 1001
        return {"state": "OPEN", "headRefOid": self.head, "autoMergeRequest": None}

    def reconcile_implementation_thread_replies(
        self, pr_number: int, **kwargs: Any
    ) -> ImplementationThreadReplyResult:
        """Classify the armed journal through its read-only recovery path."""
        assert pr_number == 1001
        assert kwargs["expected_head_sha"] == self.head
        self.reconciliations += 1
        if self.terminal == "blocked":
            return ImplementationThreadReplyResult(blocked_thread_ids=("thread-1",))
        return ImplementationThreadReplyResult(replied_thread_ids=("thread-1",))

    def post_implementation_thread_replies(
        self, pr_number: int, **kwargs: Any
    ) -> ImplementationThreadReplyResult:
        """Report a later thread read that has not seen the current head."""
        assert pr_number == 1001
        assert kwargs["expected_head_sha"] == self.head
        self.posts += 1
        return ImplementationThreadReplyResult(visibility_lag=True)


@pytest.mark.parametrize("terminal", ["completed", "blocked"])
def test_recovered_batch_does_not_spend_the_next_batch_visibility_budget(terminal: str) -> None:
    """A completed recovery leaves the next batch its full visibility budget."""
    github = _ReplyGitHub(terminal)
    recovered = _handoff("b" * 40, "1" * 32, recovered=True)
    retries = 0
    for expected in (1, 2):
        receipt = attempt_reply_handoff(_request(recovered, retries), github)
        assert receipt.status == "visibility_wait"
        assert receipt.visibility_retries == expected
        assert receipt.remaining_handoff is not None
        retries = receipt.visibility_retries

    github.head = "b" * 40
    settled = attempt_reply_handoff(_request(recovered, retries), github)
    assert settled.status == terminal
    assert settled.remaining_handoff is None
    assert github.reconciliations == 1
    assert github.posts == 0

    fresh = _handoff("c" * 40, "2" * 32)
    first_wait = attempt_reply_handoff(_request(fresh, settled.visibility_retries), github)
    assert first_wait.status == "visibility_wait"
    assert first_wait.visibility_retries == 1
    assert settled.visibility_retries == 0


def test_current_head_read_does_not_reset_the_active_batch_visibility_budget() -> None:
    """Repeated thread visibility failures terminate within the same batch."""
    github = _ReplyGitHub()
    handoff = _handoff(github.head, "3" * 32)
    retries = 0
    for expected in (1, 2):
        receipt = attempt_reply_handoff(_request(handoff, retries), github)
        assert receipt.status == "visibility_wait"
        assert receipt.visibility_retries == expected
        assert receipt.remaining_handoff is not None
        retries = receipt.visibility_retries
    final = attempt_reply_handoff(_request(handoff, retries), github)
    assert final.status == "stale"
    assert final.remaining_handoff is None
    assert final.visibility_retries == 0
    assert github.posts == 3
