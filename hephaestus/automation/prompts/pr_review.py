"""PR review-phase prompts.

Contains the PR review analysis prompt (inline-comment generator) and the
plain PR description template.
"""

from ._review_rubric import get_pr_review_rubric
from ._shared import fence_content, get_terse_output_directive
from .catalog import PromptCatalog

#: Severities that BLOCK a GO when their automation thread is unresolved (#1856).
#: ``minor``/``nitpick`` are advisory — a GO the reviewer returned must not
#: deadlock to state:skip over a nit it declined to block on. An unmarked or
#: unknown severity is treated as BLOCKING (fail-safe), which reproduces the
#: pre-#1856 all-blocking behavior when severity is not yet seeded.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "major"})
VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "major", "minor", "nitpick"})

#: HTML-comment marker prepended to each posted review-thread body so the GO
#: gate can recover the reviewer's severity after the GitHub round-trip.
#: Anchored as a full marker line — never matched by a free substring scan.
SEVERITY_MARKER_PREFIX = "<!-- hephaestus-severity:"


def get_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    pr_description: str = "",
    advise_findings: str = "",
    include_nitpicks: bool = False,
    review_context_kind: str = "issue",
) -> str:
    """Get the `$athena:pr-review` analysis prompt for inline review comments.

    All free-text fields are fenced as untrusted (see module docstring).

    This is the loop's only automated review gate. When the Athena skill is
    available, the prompt directs the reviewer to run its default profile;
    otherwise the inline-review contract below is the fallback.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        pr_diff: PR diff output
        issue_body: Issue body/description
        pr_description: PR description body
        advise_findings: Prior team learnings from Mnemosyne to give the
            reviewer continuity with the advise-first implementation turn.
        include_nitpicks: When False (default), the reviewer is told to OMIT
            ``nitpick``-severity comments entirely. When True (``--nitpick``),
            nitpick comments are re-enabled. Either way every emitted comment
            carries a ``severity`` tag (#1083).
        review_context_kind: Human-readable numeric context kind. Linked PRs
            use ``"issue"``; unlinked direct ``--prs`` review uses ``"PR"``
            because GitHub PRs are issue objects but not linked requirements.

    Returns:
        Formatted PR review analysis prompt

    """
    fenced = fence_content()
    nitpick_template = (
        "pr_review/nitpick_include.j2" if include_nitpicks else "pr_review/nitpick_suppress.j2"
    )
    nitpick_directive = PromptCatalog.current().render(nitpick_template).strip()
    return PromptCatalog.current().render(
        "pr_review/analysis.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        pr_diff_block=fenced.fence("PR_DIFF", pr_diff),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=fenced.fence(
            "ADVISE_FINDINGS",
            advise_findings or "_(no prior advise findings supplied)_",
        ),
        pr_description_block=fenced.fence("PR_DESCRIPTION", pr_description),
        untrusted_notice=fenced.untrusted_notice,
        review_rubric=get_pr_review_rubric().strip(),
        nitpick_directive=nitpick_directive,
        terse_output_directive=get_terse_output_directive(),
    )


def get_review_validation_prompt(
    pr_number: int,
    issue_number: int,
    prior_comments_json: str,
    diff_text: str = "",
    review_context_kind: str = "issue",
) -> str:
    """Get the prompt that validates whether prior review comments were addressed.

    Used by :mod:`hephaestus.automation.review_validator` to re-check, with a
    fresh read-only sub-agent, that the implementer's fixes actually resolved
    the previous iteration's review comments — re-opening (as new inline
    threads) any the current diff leaves unaddressed.

    Both inputs are fenced as untrusted (prior comment bodies + the diff are
    GitHub-sourced).

    Args:
        pr_number: GitHub PR number under validation.
        issue_number: Linked GitHub issue number.
        prior_comments_json: JSON array string of prior comment dicts
            (``path``/``line``/``body``).
        diff_text: The current cumulative PR diff.
        review_context_kind: Human-readable numeric context kind for the
            prompt header (``"issue"`` or direct-review ``"PR"``).

    Returns:
        Formatted review-validation prompt.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "pr_review/validation.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        prior_comments_block=fenced.fence("PRIOR_COMMENTS", prior_comments_json),
        diff_block=fenced.fence("DIFF", diff_text),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_comment_difficulty_prompt(
    issue_number: int,
    comments_json: str,
    review_context_kind: str = "issue",
) -> str:
    """Get the prompt that classifies review-comment fix difficulty (#1083).

    Used by :mod:`hephaestus.automation.comment_difficulty` to label each
    unresolved comment ``simple`` / ``medium`` / ``hard`` so the per-comment fix
    sub-agent runs at the matching model tier. The comment bodies are fenced as
    untrusted (GitHub-sourced).

    Args:
        issue_number: Linked GitHub issue number (for log/context only).
        comments_json: JSON array string of comment dicts
            (``thread_id``/``path``/``line``/``body``).
        review_context_kind: Human-readable numeric context kind for the
            prompt header (``"issue"`` or direct-review ``"PR"``).

    Returns:
        Formatted comment-difficulty classification prompt.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "pr_review/comment_difficulty.j2",
        issue_number=issue_number,
        review_context_kind=review_context_kind,
        comments_block=fenced.fence("REVIEW_COMMENTS", comments_json),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_pr_description(
    issue_number: int,
    summary: str,
    changes: str,
    testing: str,
    generated_by: str = "Hephaestus automation",
) -> str:
    """Generate a PR description.

    Args:
        issue_number: GitHub issue number
        summary: Brief summary of changes
        changes: Detailed list of changes
        testing: Testing information
        generated_by: Short description of the tool/agent that generated the PR

    Returns:
        Formatted PR description

    """
    return PromptCatalog.current().render(
        "pr_review/description.j2",
        issue_number=issue_number,
        summary=summary,
        changes=changes,
        testing=testing,
        generated_by=generated_by,
    )
