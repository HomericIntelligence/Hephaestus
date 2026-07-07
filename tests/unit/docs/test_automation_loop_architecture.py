"""Regression tests for automation-loop architecture documentation."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from pathlib import Path

from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator
from hephaestus.automation.prompts.implementation import (
    get_dirty_reused_worktree_decision_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from hephaestus.automation.prompts.planning import (
    get_plan_loop_review_prompt,
    get_plan_prompt,
)

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md"

PROMPT_REFS: tuple[tuple[str, str, Callable[..., object]], ...] = (
    ("prompts/planning.py", "get_plan_prompt", get_plan_prompt),
    ("prompts/planning.py", "get_plan_loop_review_prompt", get_plan_loop_review_prompt),
    (
        "prompts/implementation.py",
        "get_dirty_reused_worktree_decision_prompt",
        get_dirty_reused_worktree_decision_prompt,
    ),
    ("prompts/implementation.py", "get_implementation_prompt", get_implementation_prompt),
    (
        "prompts/implementation.py",
        "get_impl_resume_feedback_prompt",
        get_impl_resume_feedback_prompt,
    ),
    ("ci_fix_orchestrator.py", "build_ci_fix_prompt", CIFixOrchestrator.build_ci_fix_prompt),
    (
        "ci_fix_orchestrator.py",
        "force_engagement_prompt",
        CIFixOrchestrator.force_engagement_prompt,
    ),
)


def test_ci_stage_documents_shipped_classifier() -> None:
    """The CI-stage doc must describe classify_ci_state as shipped code."""
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "classify_ci_state" in text
    assert "NEW pure function" not in text
    assert "does not exist yet" not in text
    assert "shipped pure classifier" in text
    assert "tests/unit/automation/pipeline/stages/test_classify_ci_state.py" in text


def test_issue_1929_prompt_line_refs_match_source_definitions() -> None:
    """Documented prompt-function line references stay aligned with source."""
    doc = DOC_PATH.read_text(encoding="utf-8")

    for path, function_name, function in PROMPT_REFS:
        expected_line = str(inspect.getsourcelines(function)[1])
        refs = re.findall(rf"`{re.escape(path)}:(\d+)\s+{function_name}`", doc)
        assert refs, f"missing documented reference for {path} {function_name}"
        assert set(refs) == {expected_line}
