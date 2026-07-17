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

__all__ = [
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
