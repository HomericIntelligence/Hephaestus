"""Safety regressions for #2055's head-bound strict-review stage."""

from __future__ import annotations

import importlib
from typing import Any

from hephaestus.automation.pipeline.routing import StageName
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

_OLD_HEAD = "a" * 40
_NEW_HEAD = "b" * 40


class _HeadChangedGitHub(FakeStageGitHub):
    """Expose a live remote head that no longer matches the saved review."""

    def __init__(self) -> None:
        super().__init__(labels=["state:implementation-go"])

    def gh_pr_state(self, pr_number: int) -> dict[str, str]:
        assert pr_number == 601
        return {"state": "OPEN", "headRefOid": _NEW_HEAD}


def test_head_change_revokes_stale_go_before_another_review_attempt(
    make_ctx: Any, make_work_item: Any
) -> None:
    """A proof for an old SHA must clear eligibility and restart at ENTER."""
    strict_review = importlib.import_module("hephaestus.automation.pipeline.stages.strict_review")
    stage = strict_review.StrictReviewStage()
    github = _HeadChangedGitHub()
    ctx = make_ctx(github=github)
    item = make_work_item(
        stage=StageName.STRICT_REVIEW,
        pr=601,
        state="HEAD_CHECK",
        payload={"strict_review_head": _OLD_HEAD, "strict_review_verdict": "GO"},
    )

    assert stage.on_enter(item, ctx) is None
    assert item.state == strict_review.ENTER
    assert "strict_review_head" not in item.payload
    assert "strict_review_verdict" not in item.payload
    assert ("defer_auto_merge", (601,)) in github.mutation_log
    assert ("arm_auto_merge", (601,)) not in github.mutation_log
