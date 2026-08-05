"""Focused tests for the PR-review approval gate collaborator."""

from __future__ import annotations

from typing import Any

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
