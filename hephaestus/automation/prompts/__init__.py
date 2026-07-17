"""Prompt templates for Claude Code automation.

Contains templates for:
- Issue implementation guidance
- Planning guidance
- PR descriptions

Untrusted-input fencing
-----------------------
Several review prompts interpolate untrusted GitHub content (issue bodies, PR
diffs, reviewer comments) directly. A malicious issue could otherwise emit
fake verdict lines or fenced JSON blocks that bypass review. The helper
``_fence_untrusted()`` wraps each user-supplied field with random-nonce
delimiters and an instruction to Claude that text inside is data, not a
directive. The output parsers ignore directives outside their own emitted
block (last-fence-wins for JSON; verdict parsers should likewise prefer the
last matching line in Claude's free-form prose).

This module was split from a single 1,368-line ``prompts.py`` into a package
of phase-grouped submodules. It re-exports every public symbol so existing
importers continue to work unchanged.
"""

from __future__ import annotations

from typing import Any

from hephaestus.prompts import PromptCatalog

# Re-export private helpers/constants for tests and internal callers that
# previously did ``from hephaestus.automation.prompts import _fence_untrusted``
# or ``prompts._STRICT_GRADING_AND_ANTI_INFLATION``. The ``as`` aliases tell
# ruff these are intentional re-exports, not unused imports.
from ._shared import (
    FencedContent as FencedContent,
    _fence_untrusted as _fence_untrusted,
    _iteration_guidance as _iteration_guidance,
    _iteration_label as _iteration_label,
    _prior_review_block as _prior_review_block,
    _prompts_logger as _prompts_logger,
    _relativize_path as _relativize_path,
    fence_content as fence_content,
    get_terse_output_directive as get_terse_output_directive,
    get_untrusted_notice as get_untrusted_notice,
)
from ._strict_rubric import (
    _FULL_SWEEP_SUFFIX as _FULL_SWEEP_SUFFIX,
    _IMPL_LOOP_STRICT_RUBRIC as _IMPL_LOOP_STRICT_RUBRIC,
    _PLAN_LOOP_STRICT_RUBRIC as _PLAN_LOOP_STRICT_RUBRIC,
    _PLAN_STRICT_RUBRIC as _PLAN_STRICT_RUBRIC,
    _PR_STRICT_RUBRIC as _PR_STRICT_RUBRIC,
    _PR_STRICT_RUBRIC_DIMENSIONS as _PR_STRICT_RUBRIC_DIMENSIONS,
    _SEVEN_PRINCIPLES_DIMENSIONS as _SEVEN_PRINCIPLES_DIMENSIONS,
    _STRICT_GRADING_AND_ANTI_INFLATION as _STRICT_GRADING_AND_ANTI_INFLATION,
    _STRICT_REVIEW_OUTPUT_FORMAT as _STRICT_REVIEW_OUTPUT_FORMAT,
    _STRICT_REVIEW_RUBRIC as _STRICT_REVIEW_RUBRIC,
)
from .address_review import build_unaddressed_directive, get_address_review_prompt
from .advise import (
    get_advise_prompt,
    get_advise_prompt_builder,
    get_codex_advise_prompt,
)
from .follow_up import get_follow_up_prompt
from .implementation import (
    get_dirty_reused_worktree_decision_prompt,
    get_dirty_reused_worktree_prompt,
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from .planning import (
    get_plan_loop_review_prompt,
    get_plan_prompt,
    get_plan_review_prompt,
)
from .pr_review import (
    get_comment_difficulty_prompt,
    get_pr_description,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)

_LEGACY_PROMPT_TEMPLATES = {
    "ADDRESS_REVIEW_PROMPT": "address_review/address_review.j2",
    "ADVISE_PROMPT": "advise/advise.j2",
    "CODEX_ADVISE_PROMPT": "advise/direct.j2",
    "DIRTY_REUSED_WORKTREE_DECISION_PROMPT": "implementation/dirty_worktree.j2",
    "DIRTY_REUSED_WORKTREE_PROMPT": "implementation/dirty_worktree.j2",
    "FOLLOW_UP_PROMPT": "follow_up/follow_up.j2",
    "IMPLEMENTATION_PROMPT": "implementation/implementation.j2",
    "IMPL_LOOP_REVIEW_PROMPT": "implementation/loop_review.j2",
    "IMPL_RESUME_FEEDBACK_PROMPT": "implementation/resume_feedback.j2",
    "PLAN_LOOP_REVIEW_PROMPT": "planning/plan_loop_review.j2",
    "PLAN_PROMPT": "planning/plan.j2",
    "PLAN_REVIEW_PROMPT": "planning/plan_review.j2",
    "PR_REVIEW_ANALYSIS_PROMPT": "pr_review/analysis.j2",
}


class _LegacyPromptTemplate(str):
    """String-compatible bridge for deprecated ``*_PROMPT.format(...)`` users."""

    _template_name: str

    def __new__(cls, template_name: str) -> _LegacyPromptTemplate:
        catalog = PromptCatalog.current()
        instance = super().__new__(cls, catalog.source(template_name))
        instance._template_name = template_name
        return instance

    def format(self, *args: Any, **kwargs: Any) -> str:
        """Render through Jinja while preserving the historical ``.format`` API."""
        if args:
            return super().format(*args, **kwargs)
        return PromptCatalog.current().render(self._template_name, **kwargs)


def __getattr__(name: str) -> _LegacyPromptTemplate:
    """Lazily retain deprecated prompt-string exports without Python prose."""
    try:
        template_name = _LEGACY_PROMPT_TEMPLATES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    return _LegacyPromptTemplate(template_name)

__all__ = [
    "ADDRESS_REVIEW_PROMPT",
    "ADVISE_PROMPT",
    "CODEX_ADVISE_PROMPT",
    "DIRTY_REUSED_WORKTREE_DECISION_PROMPT",
    "DIRTY_REUSED_WORKTREE_PROMPT",
    "FOLLOW_UP_PROMPT",
    "IMPLEMENTATION_PROMPT",
    "IMPL_LOOP_REVIEW_PROMPT",
    "IMPL_RESUME_FEEDBACK_PROMPT",
    "PLAN_LOOP_REVIEW_PROMPT",
    "PLAN_PROMPT",
    "PLAN_REVIEW_PROMPT",
    "PR_REVIEW_ANALYSIS_PROMPT",
    "FencedContent",
    "build_unaddressed_directive",
    "fence_content",
    "get_address_review_prompt",
    "get_advise_prompt",
    "get_advise_prompt_builder",
    "get_codex_advise_prompt",
    "get_comment_difficulty_prompt",
    "get_dirty_reused_worktree_decision_prompt",
    "get_dirty_reused_worktree_prompt",
    "get_follow_up_prompt",
    "get_impl_loop_review_prompt",
    "get_impl_resume_feedback_prompt",
    "get_implementation_prompt",
    "get_plan_loop_review_prompt",
    "get_plan_prompt",
    "get_plan_review_prompt",
    "get_pr_description",
    "get_pr_review_analysis_prompt",
    "get_review_validation_prompt",
    "get_terse_output_directive",
    "get_untrusted_notice",
]
