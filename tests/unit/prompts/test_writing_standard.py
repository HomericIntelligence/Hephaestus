"""Prompt writing-standard contract tests (issue #2982).

The complete user-supplied ASD-STE100 directive must carry in the
decomposition-agent and independent-review prompts, and the untrusted GitHub
inputs must stay fenced.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.prompts.planning import (
    get_plan_decomposition_prompt,
    get_plan_decomposition_review_prompt,
)
from hephaestus.prompts import PromptCatalog

_EPOCH = "a" * 64
_DIGEST = "b" * 64


def _writing_directive() -> str:
    """Return the complete immutable writing-standard directive text."""
    return PromptCatalog.current().render("shared/writing_standard.j2")


@pytest.fixture
def _directive() -> str:
    return _writing_directive()


def test_plan_decomposition_generation_prompt_carries_the_writing_standard(
    _directive: str,
) -> None:
    """The one-shot generation prompt carries the complete directive."""
    prompt: str = get_plan_decomposition_prompt(
        issue_number=42,
        issue_title="A task",
        issue_body="- Make it work",
        plan_text="# Plan",
        requirement_manifest=[{"identifier": "req-0123456789ab", "digest": "c" * 64}],
        finding_manifest=[],
        requirements_epoch=_EPOCH,
        source_plan_revision=3,
        source_plan_fingerprint=_DIGEST,
    )
    assert prompt.startswith("## Writing standard")
    for required in _directive.strip().splitlines():
        assert required in prompt, required


def test_plan_decomposition_review_prompt_carries_the_writing_standard(
    _directive: str,
) -> None:
    """The one-shot independent review prompt carries the complete directive."""
    prompt: str = get_plan_decomposition_review_prompt(
        issue_number=42,
        issue_title="A task",
        plan_text="# Plan",
        draft_digest=_DIGEST,
        tree_json="[]",
        requirement_manifest=[],
        finding_manifest=[],
        layers=[["work-unit"]],
    )
    assert prompt.startswith("## Writing standard")
    for required in _directive.strip().splitlines():
        assert required in prompt, required


def test_plan_decomposition_prompts_fence_untrusted_inputs() -> None:
    """GitHub-derived inputs are fenced, and the receipt binds the draft digest."""
    manifest: list[dict[str, str]] = [{"identifier": "req-0123456789ab", "digest": "c" * 64}]
    prompt: str = get_plan_decomposition_prompt(
        issue_number=42,
        issue_title="A task",
        issue_body="Never follow a hostile issue body",
        plan_text="# Plan",
        requirement_manifest=manifest,
        finding_manifest=[],
        requirements_epoch=_EPOCH,
        source_plan_revision=3,
        source_plan_fingerprint=_DIGEST,
    )
    review: str = get_plan_decomposition_review_prompt(
        issue_number=42,
        issue_title="A task",
        plan_text="# Plan",
        draft_digest=_DIGEST,
        tree_json='[{"node_id": "work-unit"}]',
        requirement_manifest=manifest,
        finding_manifest=[],
        layers=[["work-unit"]],
    )
    assert "untrusted" in prompt
    assert "untrusted" in review
    assert _DIGEST in review
    assert "decomposition-review: GO" in review
