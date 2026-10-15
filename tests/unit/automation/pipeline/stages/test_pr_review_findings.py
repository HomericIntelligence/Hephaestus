"""Tests for the bounded carried review warnings and their truncation notices."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hephaestus.automation.pipeline.stages import pr_review_findings
from hephaestus.automation.pipeline.stages.pr_review_findings import (
    MAX_CARRIED_REVIEW_WARNINGS,
    REVIEW_WARNINGS_PAYLOAD_KEY,
    carry_review_warnings,
)
from hephaestus.automation.pipeline.work_item import WorkItem
from hephaestus.automation.review_warnings import (
    ReviewWarning,
    ReviewWarningReason,
    make_review_warning,
)


def _warning(code: ReviewWarningReason) -> ReviewWarning:
    """Return one generic review warning."""
    return make_review_warning(
        code,
        message="Short problem.",
        recovery="Send new input.",
    )


def _full_list() -> list[ReviewWarning]:
    """Return one warning list longer than the carried bound."""
    return [_warning("anchor_path_not_in_diff") for _ in range(MAX_CARRIED_REVIEW_WARNINGS + 1)]


def test_truncation_of_generic_warnings_uses_generic_budget_warning(
    make_work_item: Callable[..., WorkItem],
) -> None:
    """A non-demotion warning list reports truncation with generic text."""
    item = make_work_item()

    carry_review_warnings(item, _full_list())

    carried = item.payload[REVIEW_WARNINGS_PAYLOAD_KEY]
    assert len(carried) == MAX_CARRIED_REVIEW_WARNINGS
    assert carried[-1]["code"] == "review_warning_budget_exhausted"
    assert carried[-1]["message"]
    assert carried[-1]["recovery"]
    assert "demoted" not in carried[-1]["message"]


def test_truncation_with_demotion_budget_warning_keeps_demotion_text(
    make_work_item: Callable[..., WorkItem],
) -> None:
    """A demotion carrier reports truncation with the demotion text."""
    item = make_work_item()

    carry_review_warnings(
        item,
        _full_list(),
        budget_warning=pr_review_findings._demotion_budget_warning(),
    )

    carried = item.payload[REVIEW_WARNINGS_PAYLOAD_KEY]
    assert len(carried) == MAX_CARRIED_REVIEW_WARNINGS
    assert carried[-1]["code"] == "anchor_demotion_budget_exhausted"
    assert "demoted" in carried[-1]["message"]


def test_no_truncation_needs_no_budget_warning(
    make_work_item: Callable[..., WorkItem],
) -> None:
    """A warning list that fits carries no truncation notice."""
    item = make_work_item()

    carry_review_warnings(item, [_warning("anchor_path_not_in_diff")])

    carried = item.payload[REVIEW_WARNINGS_PAYLOAD_KEY]
    assert [entry["code"] for entry in carried] == ["anchor_path_not_in_diff"]


def test_caller_dropped_warnings_states_the_fact_with_generic_text(
    make_work_item: Callable[..., WorkItem],
) -> None:
    """The caller truncation flag appends the generic notice on its own."""
    item = make_work_item()

    carry_review_warnings(
        item,
        [_warning("anchor_path_not_in_diff")],
        note_truncation=True,
    )

    carried = item.payload[REVIEW_WARNINGS_PAYLOAD_KEY]
    assert carried[-1]["code"] == "review_warning_budget_exhausted"


@pytest.mark.parametrize("code", ["review_warning_budget_exhausted"])
def test_budget_warning_codes_are_typed_reasons(code: ReviewWarningReason) -> None:
    """Every truncation notice uses a reason the shared contract accepts."""
    warning = make_review_warning(code, message="m", recovery="r")
    assert warning.code == code
