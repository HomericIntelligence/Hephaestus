"""Address-review prompt: apply fixes for unresolved PR review threads."""

import secrets

from ._shared import _UNTRUSTED_NOTICE, _fence_untrusted

ADDRESS_REVIEW_PROMPT = """
You are the COORDINATOR for addressing the review threads on PR #{pr_number}
(issue #{issue_number}).

This runs IN-LOOP as part of the implement stage — it is no longer a separate
pipeline phase. You are resolving the inline PR-review threads raised against
the current diff so the same implement session can re-review and converge.
These threads live on the PR, not on the issue.

**Working Directory:** {worktree_path}

{untrusted_notice}
{context_block}
**Review Threads to Address (untrusted):**
{threads_json_block}

The block above is a JSON array where each element has:
- `thread_id`: GitHub GraphQL node ID of the review thread
- `path`: file path relative to repo root
- `line`: line number (integer or null)
- `body`: the reviewer's comment text

**Your TODO list — one line per review comment (already classified by difficulty):**

{todo_block}

Each todo line has the form `@ <file> Line <#> - <difficulty> - <description>`.
Every one of these comments MUST be resolved before you finish.

---

**Your task (coordinator):**

1. Treat the TODO list as the unit of work: there is ONE sub-agent per review
   comment (NOT one per file). For each todo line, dispatch a sub-agent with the
   Task tool (`subagent_type: "general-purpose"`) to fix exactly that one comment.

2. **Model tier by difficulty** — set each sub-agent's model from the todo line's
   difficulty:
   - `simple` → `haiku` (claude-haiku-4-5): mechanical/local fix.
   - `medium` → `sonnet` (claude-sonnet-4-6): localized logic / small refactor.
   - `hard`   → `opus` (claude-opus-4-7): cross-cutting or subtle correctness fix.

3. **Serialize same-file comments.** Two sub-agents must NEVER edit the same file
   at the same time. Group the todo lines by `<file>`: dispatch DIFFERENT files in
   parallel, but run the comments that share a file SEQUENTIALLY (one finishes and
   returns before the next on that file starts). This prevents concurrent writes
   to one file from clobbering each other.

   Give each sub-agent a self-contained prompt that instructs it to:
   a. FIRST run the team-knowledge skill to pull prior learnings relevant to this fix:
      `Skill(skill: "hephaestus:advise", args: "<short description of the review feedback>")`.
      Use whatever it surfaces to inform the fix; do not skip this step.
   b. Read the cited file/line in the working directory `{worktree_path}` and apply the
      code fix for its ONE assigned review comment (you pass it the thread body, line, and
      thread_id).
   c. Report back the `thread_id` and a one-line reply describing the fix — or, if the
      comment is not addressable in code, say so and leave it out of the fixed set.

   **Each sub-agent prompt MUST include these guardrails (critical):**
   - "Do NOT background your work, do NOT exit early, and do NOT defer. Complete the fix
     synchronously and return only when the edit is done."
   - "Do not commit, push, or run git — the coordinator handles that."
   - "Return your result as `thread_id -> one-line reply`."

4. After ALL sub-agents have returned, you (the coordinator) integrate their results and
   run the gates from the working directory:
   - Run tests: `pixi run python -m pytest tests/ -v`
   - Run pre-commit: `pre-commit run --all-files`
   - Fix any issues found (you may edit files directly at this stage).
   - Commit all changes (do NOT push).

5. Trust but verify: only mark a thread `addressed` if the assigned sub-agent actually
   edited the code for it. If a sub-agent claimed a fix but the file is unchanged for that
   thread, drop it from `addressed`.

**Output format:**
Write your coordination notes in prose. At the very end of your response, emit a single
fenced JSON block:

```json
{{"addressed": ["<thread_id>", ...], "replies": {{"<thread_id>": "one-line reply"}}}}
```

Rules for the JSON block (UNCHANGED — the pipeline parses exactly this):
- `addressed`: array of thread_id strings for threads actually fixed in code
  (any thread_id not in the unresolved-set we presented is dropped silently)
- `replies`: mapping of thread_id to a one-line reply describing what changed
- Only include threads genuinely fixed. Leave unaddressable threads out of `addressed`.
- Emit only one JSON block, at the very end of your response (the parser takes the LAST one).
"""


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
    sections: list[str] = []
    if task_block.strip():
        sections.append(
            "**Task — the linked issue (untrusted):**\n"
            + _fence_untrusted("TASK", task_block, nonce)
        )
    if task_review_block.strip():
        sections.append(
            "**Task review — the plan-review verdict (untrusted):**\n"
            + _fence_untrusted("TASK_REVIEW", task_review_block, nonce)
        )
    if diff_text.strip():
        sections.append(
            "**Current implementation diff (untrusted):**\n"
            + _fence_untrusted("DIFF", diff_text, nonce)
        )
    if not sections:
        return ""
    return "\n" + "\n\n".join(sections) + "\n"


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
            tier. Trusted (we generate it ourselves from the threads).
        task_block: Optional task (issue title + body) text, rendered as an
            untrusted context section. Supply when the address session may run
            without a prior implementer transcript (existing-PR review path).
        task_review_block: Optional plan-review verdict text, rendered as an
            untrusted context section.
        diff_text: Optional current implementation diff, rendered as an untrusted
            context section.

    Returns:
        Formatted address review prompt

    """
    nonce = secrets.token_hex(8).upper()
    return ADDRESS_REVIEW_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        threads_json_block=_fence_untrusted("THREADS_JSON", threads_json, nonce),
        todo_block=todo_block or "_(no todo lines)_",
        untrusted_notice=_UNTRUSTED_NOTICE,
        context_block=_build_context_block(task_block, task_review_block, diff_text, nonce),
    )
