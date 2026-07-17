"""Address-review prompt: apply fixes for unresolved PR review threads."""

from typing import Any

from ._shared import _fence_untrusted, fence_content, get_terse_output_directive
from .catalog import PromptCatalog


def build_unaddressed_directive(threads: list[dict[str, Any]], nonce: str) -> str:
    """Render a "Make sure to handle <finding>" directive from unresolved threads.

    Used on a retry after the previous address turn produced NO commit (the fix
    session resumed a stale transcript and self-reported success without editing
    code, #1554). The directive re-grounds the resumed session on the concrete
    findings it still has to fix, naming each by location and reviewer body.

    Each ``body`` is verbatim untrusted reviewer text (GitHub-sourced), so the
    whole block is fenced as untrusted. The thread dicts use the snapshot shape
    returned by :func:`gh_pr_list_unresolved_threads` (``id`` / ``path`` /
    ``line`` / ``body``).

    Args:
        threads: Still-unresolved review thread dicts from the prior turn.
        nonce: Per-prompt nonce used to delimit the untrusted fence (shared with
            the rest of the prompt so all fences use one nonce).

    Returns:
        The rendered directive block, or ``""`` when ``threads`` is empty.

    """
    if not threads:
        return ""
    lines: list[str] = []
    for t in threads:
        loc = t.get("path") or "<no path>"
        line_no = t.get("line")
        loc_str = f"{loc}:{line_no}" if line_no is not None else loc
        body = (t.get("body") or "").strip() or "<empty body>"
        lines.append(f"- Make sure to handle {loc_str} — {body}")
    directive = _fence_untrusted("UNADDRESSED", "\n".join(lines), nonce)
    return PromptCatalog.current().render(
        "address_review/unaddressed_directive.j2", directive=directive
    )


def _build_context_block(
    task_block: str,
    task_review_block: str,
    diff_text: str,
    nonce: str,
) -> str:
    """Render the optional TASK / TASK_REVIEW / DIFF context for the address prompt.

    These are supplied when the address session may run WITHOUT a prior
    implementer transcript to resume (the existing-PR review path): a fresh
    session has no memory of the task or the implementation, so it must read the
    task, the task-review, and the current diff to continue the work correctly.
    Each is fenced as untrusted (issue/PR text + diff are GitHub-sourced).
    Returns an empty string when none are supplied (the resume path already
    carries this context in its transcript).
    """
    if not any((task_block.strip(), task_review_block.strip(), diff_text.strip())):
        return ""
    context = PromptCatalog.current().render(
        "address_review/context_block.j2",
        task_block=_fence_untrusted("TASK", task_block, nonce) if task_block.strip() else "",
        task_review_block=(
            _fence_untrusted("TASK_REVIEW", task_review_block, nonce)
            if task_review_block.strip()
            else ""
        ),
        diff_block=_fence_untrusted("DIFF", diff_text, nonce) if diff_text.strip() else "",
    )
    return f"\n{context}\n"


def get_address_review_prompt(
    pr_number: int,
    issue_number: int,
    worktree_path: str,
    threads_json: str,
    *,
    todo_block: str = "",
    task_block: str = "",
    task_review_block: str = "",
    diff_text: str = "",
    unaddressed_findings: list[dict[str, Any]] | None = None,
) -> str:
    """Get the address review prompt for fixing inline review thread feedback.

    ``threads_json`` is fenced as untrusted (it embeds reviewer comment bodies
    sourced from GitHub).

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        worktree_path: Path to the git worktree containing the PR branch
        threads_json: JSON string of unresolved review threads (array of thread dicts)
        todo_block: Pre-rendered, difficulty-classified todo list — one line per
            comment in the form ``@ <file> Line <#> - <difficulty> - <desc>``
            (built by :mod:`hephaestus.automation.comment_difficulty`, #1083).
            Drives the one-sub-agent-per-comment dispatch and per-comment model
            tier. The path/line/difficulty are trusted, but the ``<desc>``
            excerpt is verbatim untrusted comment text, so the whole block is
            fenced as untrusted (#1085 C4).
        task_block: Optional task (issue title + body) text, rendered as an
            untrusted context section. Supply when the address session may run
            without a prior implementer transcript (existing-PR review path).
        task_review_block: Optional plan-review verdict text, rendered as an
            untrusted context section.
        diff_text: Optional current implementation diff, rendered as an untrusted
            context section.
        unaddressed_findings: Optional still-unresolved review threads from a
            prior address turn that produced NO commit (#1554). When supplied,
            a "Make sure to handle <finding>" directive is rendered above the
            thread list to re-ground a resumed session on what it failed to fix.

    Returns:
        Formatted address review prompt

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "address_review/address_review.j2",
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        threads_json_block=fenced.fence("THREADS_JSON", threads_json),
        todo_block=fenced.fence("TODO_LIST", todo_block or "_(no todo lines)_"),
        untrusted_notice=fenced.untrusted_notice,
        context_block=_build_context_block(
            task_block,
            task_review_block,
            diff_text,
            fenced.nonce,
        ),
        retry_directive_block=build_unaddressed_directive(
            unaddressed_findings or [],
            fenced.nonce,
        ),
        terse_output_directive=get_terse_output_directive(),
    )
