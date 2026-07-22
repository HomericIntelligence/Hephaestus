"""Planning-phase prompts.

Contains the plan-generation prompt, the standalone plan-review prompt, and
the iteration-aware plan-loop review prompt.
"""

from ._review_rubric import (
    get_full_sweep_suffix,
    get_plan_loop_review_rubric,
    get_plan_review_output_format,
    get_plan_review_rubric,
)
from ._shared import (
    _iteration_guidance,
    _iteration_label,
    _prior_review_block,
    fence_content,
    get_terse_output_directive,
)
from .catalog import PromptCatalog


def get_plan_prompt(issue_number: int, *, catalog: PromptCatalog | None = None) -> str:
    """Get the planning prompt for an issue."""
    return (catalog or PromptCatalog.current()).render(
        "planning/plan.j2",
        issue_number=issue_number,
        terse_output_directive=get_terse_output_directive(),
    )


def get_plan_review_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
) -> str:
    """Get the plan review prompt for evaluating an issue implementation plan.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (fenced as untrusted)
        issue_body: Issue body/description (fenced as untrusted)
        plan_text: The full plan text to review (fenced as untrusted)

    Returns:
        Formatted plan review prompt

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/plan_review.j2",
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        untrusted_notice=fenced.untrusted_notice,
        review_rubric=get_plan_review_rubric().strip(),
        output_format=get_plan_review_output_format().strip(),
        terse_output_directive=get_terse_output_directive(),
    )


def get_plan_loop_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
    learnings: str,
    iteration: int,
    prior_review: str | None,
    advise_findings: str = "",
    plan_history: str = "",
) -> str:
    """Build the iteration-aware plan-loop review prompt.

    Args:
        issue_number: GitHub issue number.
        issue_title: Issue title.
        issue_body: Full issue body.
        plan_text: Plan to review.
        learnings: Learnings captured by the planner this iteration.
        iteration: Iteration index (0, 1, or 2).
        prior_review: Previous iteration's review text, or ``None`` on iter 0.
        advise_findings: Prior team learnings from the advise step to give the
            reviewer the same Mnemosyne context the planner received.

    Returns:
        Formatted prompt for a fresh reviewer session.

    """
    fenced = fence_content()
    full_sweep_suffix = get_full_sweep_suffix().strip() if iteration == 2 else ""
    return PromptCatalog.current().render(
        "planning/plan_loop_review.j2",
        rubric=get_plan_loop_review_rubric().strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=fenced.fence(
            "ADVISE_FINDINGS",
            advise_findings or "_(no prior advise findings supplied)_",
        ),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        learnings=learnings or "_(no learnings captured this iteration)_",
        prior_review_block=_prior_review_block(prior_review, fenced),
        plan_history_block=(
            fenced.fence("PLAN_HISTORY", plan_history) if plan_history else "_(first revision)_"
        ),
        full_sweep_suffix=full_sweep_suffix,
        output_format=get_plan_review_output_format().strip(),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )
