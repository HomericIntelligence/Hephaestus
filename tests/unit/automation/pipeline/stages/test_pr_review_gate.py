"""Focused tests for the PR-review approval gate collaborator."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import StageOutcome
from hephaestus.automation.pipeline.stages.pr_review import PrReviewStage


class _RecordingGitHub:
    """Minimal read/write double for the exact GO admission sequence."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_unresolved_review_threads(self, pr_number: int) -> list[dict[str, Any]]:
        del pr_number
        self.calls.append("threads")
        return []

    def gh_pr_state(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        self.calls.append("state")
        return {"state": "OPEN", "headRefOid": "a" * 40, "autoMergeRequest": None}

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        del pr_number
        self.calls.append("mark_go")

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        del pr_number
        self.calls.append("labels")
        return True, False


def test_write_go_has_one_read_write_readback_sequence(make_work_item: Any) -> None:
    """GO is written only after exact-head reads and followed by readback."""
    item = make_work_item(issue=2361, pr=42, state="EVAL")
    item.payload["reviewed_pr_head_sha"] = "a" * 40
    github = _RecordingGitHub()

    result = PrReviewStage().write_go(item, github)

    assert result == StageOutcome(Disposition.ADVANCE, "review audit; merge wait pending")
    assert github.calls == ["threads", "state", "mark_go", "state", "threads", "labels"]


def test_merge_during_receipt_read_finishes_without_retry(
    make_work_item: Any, make_ctx: Any
) -> None:
    """A merge during receipt collection completes the exact reviewed PR."""
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    class RacingGitHub(FakeStageGitHub):
        merged = False

        def reviewed_pr_state(self, pull_request_id: str) -> dict[str, Any]:
            assert pull_request_id == "PR_exact"
            return {
                "id": "PR_exact",
                "headRefOid": "a" * 40,
                "state": "MERGED" if self.merged else "OPEN",
                "mergedAt": "2026-09-07T05:47:12Z" if self.merged else None,
            }

        def reviewer_validation_receipts(
            self, pr_number: int, **kwargs: Any
        ) -> list[dict[str, Any]]:
            self.merged = True
            raise RuntimeError("PR merged during receipt read")

    github = RacingGitHub()
    item = make_work_item(issue=3032, pr=42, state="VALIDATE_WAIT")
    item.payload.update(reviewed_pr_head_sha="a" * 40, reviewed_pr_node_id="PR_exact")
    before = dict(item.attempts)

    result = PrReviewStage().step(item, make_ctx(github=github))

    assert result == StageOutcome(Disposition.FINISH_PASS, "merged")
    assert item.attempts == before
    assert "review_error_retries" not in item.payload
    assert github.mutation_log == []


@pytest.mark.parametrize(
    "work_state", ["VALIDATE_WAIT", "EVAL", "POST", "POST_APPLY", "GO_AUDIT_RECEIPT"]
)
@pytest.mark.parametrize(
    "state,merged_at,expected",
    [
        ("MERGED", "2026-09-07T05:47:12Z", Disposition.FINISH_PASS),
        ("CLOSED", None, Disposition.FINISH_FAIL),
    ],
)
def test_terminal_audit_does_not_consume_format_budget(
    make_work_item: Any,
    make_ctx: Any,
    state: str,
    merged_at: str | None,
    expected: Disposition,
    work_state: str,
) -> None:
    """A terminal PR does not enter audit parsing or publication."""
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    class TerminalGitHub(FakeStageGitHub):
        def reviewed_pr_state(self, pull_request_id: str) -> dict[str, Any]:
            return {
                "id": "PR_exact",
                "headRefOid": "a" * 40,
                "state": state,
                "mergedAt": merged_at,
            }

    github = TerminalGitHub()
    item = make_work_item(issue=3032, pr=42, state=work_state)
    item.payload.update(
        reviewed_pr_head_sha="a" * 40,
        reviewed_pr_node_id="PR_exact",
        review_audit_failure=True,
        review_error_retries=2,
    )
    before = dict(item.attempts)
    result = PrReviewStage().step(item, make_ctx(github=github))
    assert result == StageOutcome(expected, "merged" if merged_at else "closed")
    assert item.attempts == before
    assert item.payload["review_error_retries"] == 2
    assert github.mutation_log == []


@pytest.mark.parametrize(
    "change",
    [
        {"id": "PR_other"},
        {"headRefOid": "b" * 40},
        {"state": "OPEN"},
        {"mergedAt": None},
        {"state": "CLOSED"},
        {"read_error": True},
        {"missing_binding": True},
    ],
)
def test_unproven_terminal_state_keeps_format_failure(
    make_work_item: Any, make_ctx: Any, change: dict[str, Any]
) -> None:
    """Unmatched or incomplete records cannot turn a failed audit into a pass."""
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    class UnprovenGitHub(FakeStageGitHub):
        def reviewed_pr_state(self, pull_request_id: str) -> dict[str, Any]:
            if change.get("read_error"):
                raise RuntimeError("unavailable")
            return {
                "id": "PR_exact",
                "headRefOid": "a" * 40,
                "state": "MERGED",
                "mergedAt": "2026-09-07T05:47:12Z",
                **change,
            }

    github = UnprovenGitHub()
    item = make_work_item(issue=3032, pr=42, state="EVAL")
    item.payload.update(reviewed_pr_head_sha="a" * 40, review_audit_failure=True)
    if not change.get("missing_binding"):
        item.payload["reviewed_pr_node_id"] = "PR_exact"
    result = PrReviewStage().step(item, make_ctx(github=github))
    assert result == StageOutcome(Disposition.RETRY, "review audit format failure")
    assert item.payload["review_error_retries"] == 1
    assert github.mutation_log == []


def test_new_review_discards_previous_terminal_identity(make_work_item: Any, make_ctx: Any) -> None:
    """A fresh review cannot use a prior review's terminal proof."""
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    class MissingContextGitHub(FakeStageGitHub):
        def pr_review_context(self, pr_number: int) -> dict[str, str] | None:
            return None

    github = MissingContextGitHub()
    item = make_work_item(issue=3032, pr=42, state="REVIEW_WAIT")
    item.payload.update(reviewed_pr_head_sha="a" * 40, reviewed_pr_node_id="PR_old")
    result = PrReviewStage().step(item, make_ctx(github=github))
    assert result == StageOutcome(Disposition.FINISH_FAIL, "pr_review_context_unavailable")
    assert "reviewed_pr_head_sha" not in item.payload
    assert "reviewed_pr_node_id" not in item.payload


def test_missing_current_node_cannot_reuse_old_metadata(make_work_item: Any, make_ctx: Any) -> None:
    """A metadata read without an ID discards the previous raw node ID."""
    from hephaestus.automation.pipeline.stages import JobRequest
    from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

    github = FakeStageGitHub()
    assert github._pr_review_context is not None
    github._pr_review_context.pop("pr_node_id", None)
    item = make_work_item(issue=3032, pr=42, state="REVIEW_WAIT")
    item.payload.update(
        pr_node_id="PR_old", reviewed_pr_node_id="PR_old", reviewed_pr_head_sha="b" * 40
    )

    result = PrReviewStage().step(item, make_ctx(github=github))

    assert isinstance(result, JobRequest)
    assert "pr_node_id" not in item.payload
    assert "reviewed_pr_node_id" not in item.payload
    assert "reviewed_pr_head_sha" not in item.payload
