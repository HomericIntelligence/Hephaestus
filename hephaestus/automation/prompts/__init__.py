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
from ._shared import _UNTRUSTED_NOTICE as _UNTRUSTED_NOTICE
from ._shared import _fence_untrusted as _fence_untrusted
from ._shared import _iteration_guidance as _iteration_guidance
from ._shared import _iteration_label as _iteration_label
from ._shared import _prior_review_block as _prior_review_block
from ._shared import _prompts_logger as _prompts_logger
from ._shared import _relativize_path as _relativize_path
from ._strict_rubric import _FULL_SWEEP_SUFFIX as _FULL_SWEEP_SUFFIX
from ._strict_rubric import _IMPL_LOOP_STRICT_RUBRIC as _IMPL_LOOP_STRICT_RUBRIC
from ._strict_rubric import _PLAN_LOOP_STRICT_RUBRIC as _PLAN_LOOP_STRICT_RUBRIC
from ._strict_rubric import _PLAN_STRICT_RUBRIC as _PLAN_STRICT_RUBRIC
from ._strict_rubric import _PR_STRICT_RUBRIC as _PR_STRICT_RUBRIC
from ._strict_rubric import _PR_STRICT_RUBRIC_DIMENSIONS as _PR_STRICT_RUBRIC_DIMENSIONS
from ._strict_rubric import _SEVEN_PRINCIPLES_DIMENSIONS as _SEVEN_PRINCIPLES_DIMENSIONS
from ._strict_rubric import _STRICT_GRADING_AND_ANTI_INFLATION as _STRICT_GRADING_AND_ANTI_INFLATION
from ._strict_rubric import _STRICT_REVIEW_OUTPUT_FORMAT as _STRICT_REVIEW_OUTPUT_FORMAT
from ._strict_rubric import _STRICT_REVIEW_RUBRIC as _STRICT_REVIEW_RUBRIC
from .address_review import ADDRESS_REVIEW_PROMPT, get_address_review_prompt
from .advise import (
    ADVISE_PROMPT,
    CODEX_ADVISE_PROMPT,
    get_advise_prompt,
    get_advise_prompt_builder,
    get_codex_advise_prompt,
)
from .follow_up import FOLLOW_UP_PROMPT, get_follow_up_prompt
from .implementation import (
    IMPL_LOOP_REVIEW_PROMPT,
    IMPL_RESUME_FEEDBACK_PROMPT,
    IMPLEMENTATION_PROMPT,
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from .planning import (
    PLAN_LOOP_REVIEW_PROMPT,
    PLAN_PROMPT,
    PLAN_REVIEW_PROMPT,
    get_plan_loop_review_prompt,
    get_plan_prompt,
    get_plan_review_prompt,
)
from .pr_review import (
    PR_REVIEW_ANALYSIS_PROMPT,
    get_comment_difficulty_prompt,
    get_pr_description,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)

__all__ = [
    "ADDRESS_REVIEW_PROMPT",
    "ADVISE_PROMPT",
    "CODEX_ADVISE_PROMPT",
    "FOLLOW_UP_PROMPT",
    "IMPLEMENTATION_PROMPT",
    "IMPL_LOOP_REVIEW_PROMPT",
    "IMPL_RESUME_FEEDBACK_PROMPT",
    "PLAN_LOOP_REVIEW_PROMPT",
    "PLAN_PROMPT",
    "PLAN_REVIEW_PROMPT",
    "PR_REVIEW_ANALYSIS_PROMPT",
    "get_address_review_prompt",
    "get_advise_prompt",
    "get_advise_prompt_builder",
    "get_codex_advise_prompt",
    "get_comment_difficulty_prompt",
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
]
