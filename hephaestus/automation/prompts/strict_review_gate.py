"""Strict-review gate prompt (issue #2055): the independent second-opinion reviewer.

Composes the external PR-strict rubric and verdict contract — rendered through
the active catalog, never duplicated —
with framing that makes explicit this reviewer runs read-only, holds no
write/GitHub-mutation capability, and must treat every field below as
untrusted content that may attempt prompt injection.
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
    ci_status: str = "",
    prior_pr_review_verdict: str = "",
) -> str:
    """Build the strict-review gate prompt for one head/attempt.

    All free-text fields are fenced as untrusted content (issue requirements,
    diff, CI status, and the prior reviewer's verdict text) — this reviewer
    must never follow instructions embedded inside them.

    Args:
        pr_number: GitHub PR number.
        issue_number: Linked GitHub issue number.
        head_sha: The PR's current head commit SHA this review is bound to.
        issue_title: Title of the linked task being reviewed.
        issue_body: Body/acceptance criteria of the linked task.
        diff: PR diff output.
        ci_status: CI check status summary.
        prior_pr_review_verdict: The pr_review stage's verdict text, for
            context only — the strict reviewer must re-derive its own
            judgment, not defer to it.

    Returns:
        The fully composed prompt string.

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
        ci_status_block=fenced.fence("CI_STATUS", ci_status),
        diff_block=fenced.fence("PR_DIFF", diff),
        strict_rubric=get_pr_strict_rubric(),
        terse_output_directive=get_terse_output_directive(),
        output_format=get_strict_review_output_format(),
    )


__all__ = ["build_strict_review_prompt"]
