"""Implementation-phase prompts.

Contains the canonical implementation prompt, the iteration-aware impl-loop
review prompt, and the resume-after-NOGO feedback prompt.
"""

from ._shared import (
    _iteration_guidance,
    _iteration_label,
    _prior_review_block,
    _relativize_path,
    fence_content,
    get_terse_output_directive,
)
from ._strict_rubric import (
    get_full_sweep_suffix,
    get_implementation_loop_strict_rubric,
    get_strict_review_output_format,
)
from .catalog import PromptCatalog

# Prompt the implementer receives when resuming its session to address a
# NoGo review verdict. Used on iterations 1 and 2 of the impl loop.


def get_implementation_prompt(
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    branch_name: str = "",
    worktree_path: str = "",
    repo_root: str | None = None,
) -> str:
    """Get the implementation prompt for an issue.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (optional, for backward compatibility)
        issue_body: Issue body/description (optional, for backward compatibility)
        branch_name: Git branch name (optional, for backward compatibility)
        worktree_path: Working directory path (optional, for backward compatibility)
        repo_root: Absolute path to the repository root.  When provided,
            *worktree_path* is relativized to avoid leaking the operator's
            filesystem layout into the prompt.

    Returns:
        Formatted implementation prompt

    """
    safe_worktree_path = _relativize_path(worktree_path, repo_root)
    fenced = fence_content()
    return PromptCatalog.current().render(
        "implementation/implementation.j2",
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        branch_name=branch_name,
        worktree_path=safe_worktree_path,
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_impl_loop_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    diff_text: str,
    files_changed: str,
    iteration: int,
    prior_review: str | None,
) -> str:
    """Build the iteration-aware implementer-loop review prompt.

    Args:
        issue_number: GitHub issue number.
        issue_title: Issue title.
        issue_body: Full issue body.
        diff_text: ``git diff <base>..HEAD`` output.
        files_changed: Newline-separated list of changed files.
        iteration: Iteration index (0, 1, or 2).
        prior_review: Previous iteration's review text, or ``None`` on iter 0.

    Returns:
        Formatted prompt for a fresh reviewer session.

    """
    fenced = fence_content()
    full_sweep_suffix = get_full_sweep_suffix().strip() if iteration == 2 else ""
    return PromptCatalog.current().render(
        "implementation/loop_review.j2",
        rubric=get_implementation_loop_strict_rubric().strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        diff_text_block=fenced.fence("DIFF_TEXT", diff_text or "_(no diff produced)_"),
        files_changed=files_changed or "_(no files changed)_",
        prior_review_block=_prior_review_block(prior_review),
        full_sweep_suffix=full_sweep_suffix,
        output_format=get_strict_review_output_format().strip(),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_dirty_reused_worktree_decision_prompt(
    *,
    branch_name: str,
    status_text: str,
    diff_text: str,
) -> str:
    """Build the dirty-worktree ownership decision prompt.

    Args:
        branch_name: PR branch being prepared for sync.
        status_text: ``git status --porcelain`` output.
        diff_text: ``git diff HEAD`` output, already truncated by caller if desired.

    Returns:
    Fenced prompt asking for an exact final-line COMMIT/STASH decision.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "implementation/dirty_worktree.j2",
        branch_block=fenced.fence("BRANCH_NAME", branch_name),
        status_block=fenced.fence("GIT_STATUS", status_text.strip() or "_(empty)_"),
        diff_block=fenced.fence(
            "GIT_DIFF_HEAD",
            (diff_text or "")[:6000] or "_(empty)_",
        ),
        untrusted_notice=fenced.untrusted_notice,
    )


def get_dirty_reused_worktree_prompt(
    *,
    branch_name: str,
    status_text: str,
    diff_text: str,
) -> str:
    """Backward-compatible alias for the dirty-worktree decision prompt."""
    return get_dirty_reused_worktree_decision_prompt(
        branch_name=branch_name,
        status_text=status_text,
        diff_text=diff_text,
    )


def get_impl_resume_feedback_prompt(
    *, issue_number: int, prev_iteration: int, verdict: str, review_text: str
) -> str:
    """Build the prompt sent via ``claude --resume`` to iterate on impl after NoGo.

    Args:
        issue_number: GitHub issue number.
        prev_iteration: Iteration index of the review that produced *review_text*.
        verdict: ``"NOGO"`` or ``"AMBIGUOUS"``.
        review_text: Full reviewer output from the previous iteration.

    Returns:
        Prompt text to feed into the resumed implementer session.

    """
    return PromptCatalog.current().render(
        "implementation/resume_feedback.j2",
        issue_number=issue_number,
        prev_iteration=prev_iteration,
        verdict=verdict,
        review_text=review_text,
        terse_output_directive=get_terse_output_directive(),
    )
