"""Build the in-loop ``$athena:pr-review`` prompt for one PR head.

The catalog template supplies the skill handoff; this builder fences every
untrusted field and supplies the exact head the strict stage captured.
"""

from __future__ import annotations

from ._shared import fence_content
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
    """Build the CI-free strict-review prompt for one captured PR head."""
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
    )


__all__ = ["build_strict_review_prompt"]
