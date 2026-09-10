"""Build current queue prompts from shared Jinja templates.

Prompt builders fence untrusted inputs before they render the template.
The shared catalog applies the project writing policy.
"""

from __future__ import annotations

from ._review_rubric import (
    _FULL_SWEEP_SUFFIX as _FULL_SWEEP_SUFFIX,
    _IMPL_LOOP_REVIEW_RUBRIC as _IMPL_LOOP_REVIEW_RUBRIC,
    _PLAN_LOOP_REVIEW_RUBRIC as _PLAN_LOOP_REVIEW_RUBRIC,
    _PLAN_REVIEW_RUBRIC as _PLAN_REVIEW_RUBRIC,
    _PR_REVIEW_RUBRIC as _PR_REVIEW_RUBRIC,
    _PR_REVIEW_RUBRIC_DIMENSIONS as _PR_REVIEW_RUBRIC_DIMENSIONS,
    _REVIEW_GRADING_AND_ANTI_INFLATION as _REVIEW_GRADING_AND_ANTI_INFLATION,
    _REVIEW_OUTPUT_FORMAT as _REVIEW_OUTPUT_FORMAT,
    _REVIEW_RUBRIC as _REVIEW_RUBRIC,
    _SEVEN_PRINCIPLES_DIMENSIONS as _SEVEN_PRINCIPLES_DIMENSIONS,
)

# Share prompt helpers with current internal callers and tests.
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
from .address_review import (
    build_scope_retraction_directive,
    build_unaddressed_directive,
    get_address_review_prompt,
    get_remediation_reply_recovery_prompt,
)
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
    MAX_PR_REVIEW_RENDERED_CHARS,
    PrReviewPromptSizeError,
    build_bounded_pr_review_analysis_prompt,
    build_bounded_review_validation_prompt,
    get_pr_description,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)

__all__ = [
    "MAX_PR_REVIEW_RENDERED_CHARS",
    "FencedContent",
    "PrReviewPromptSizeError",
    "build_bounded_pr_review_analysis_prompt",
    "build_bounded_review_validation_prompt",
    "build_scope_retraction_directive",
    "build_unaddressed_directive",
    "fence_content",
    "get_address_review_prompt",
    "get_dirty_reused_worktree_decision_prompt",
    "get_dirty_reused_worktree_prompt",
    "get_impl_loop_review_prompt",
    "get_impl_resume_feedback_prompt",
    "get_implementation_prompt",
    "get_plan_loop_review_prompt",
    "get_plan_prompt",
    "get_plan_review_prompt",
    "get_pr_description",
    "get_pr_review_analysis_prompt",
    "get_remediation_reply_recovery_prompt",
    "get_review_validation_prompt",
    "get_terse_output_directive",
    "get_untrusted_notice",
]
