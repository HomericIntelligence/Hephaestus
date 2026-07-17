"""In-loop ``$athena:pr-review`` prompt for an independent second opinion.

The prompt is rendered through the active catalog and fences all untrusted
input. The strict stage owns the resulting label decision.
"""

from __future__ import annotations

from ._shared import fence_content, get_terse_output_directive
from ._strict_rubric import get_pr_strict_rubric, get_strict_review_output_format
from .catalog import PromptCatalog


def build_strict_review_prompt(
    pr_number: int,
    issue_number: int,
    head_sha: str,
    issue_title: str,
    issue_body: str,
    diff: str = "",
    prior_pr_review_verdict: str = "",
) -> str:
    """Build the strict-review prompt for one head/attempt.

    All free-text fields are fenced as untrusted content (issue requirements,
    diff, and the prior reviewer's verdict text).
    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "strict_review/gate.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        head_sha=head_sha,
        untrusted_notice=fenced.untrusted_notice,
        prior_verdict_block=fenced.fence("PRIOR_PR_REVIEW_VERDICT", prior_pr_review_verdict),
        issue_requirements_block=fenced.fence(
            "ISSUE_REQUIREMENTS", f"# {issue_title}\n\n{issue_body}"
        ),
        diff_block=fenced.fence("PR_DIFF", diff),
        strict_rubric=get_pr_strict_rubric(),
        terse_output_directive=get_terse_output_directive(),
        output_format=get_strict_review_output_format(),
    )


__all__ = ["build_strict_review_prompt"]
