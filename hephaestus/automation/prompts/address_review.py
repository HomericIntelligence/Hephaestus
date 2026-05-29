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

**Review Threads to Address (untrusted):**
{threads_json_block}

The block above is a JSON array where each element has:
- `thread_id`: GitHub GraphQL node ID of the review thread
- `path`: file path relative to repo root
- `line`: line number (integer or null)
- `body`: the reviewer's comment text

---

**Your task (coordinator):**

1. Parse the review-threads JSON above and **group the threads by `path`** (one group
   per distinct file). Threads with a null/empty `path` (PR-level / general comments)
   form one extra group keyed as `__general__`.

2. For EACH file group, dispatch ONE sub-agent using the Task tool
   (`subagent_type: "general-purpose"`). Dispatch all groups, one sub-agent per file.
   Each sub-agent OWNS exactly one file and must not touch any other file — this
   prevents two agents editing the same file and causing merge/commit contention.

   Give each sub-agent a self-contained prompt that instructs it to:
   a. FIRST run the team-knowledge skill to pull prior learnings relevant to this fix:
      `Skill(skill: "hephaestus:advise", args: "<short description of the review feedback>")`.
      Use whatever it surfaces to inform the fix; do not skip this step.
   b. Read the owned file at `path` in the working directory `{worktree_path}` and apply
      the code fix for ALL of that file's review threads (you will pass it the thread
      bodies + line numbers + thread_ids for its file only).
   c. Report back, for each `thread_id` it handled, a one-line reply describing the fix —
      or, if a thread is not addressable in code, say so and leave it out of the fixed set.

   **Each sub-agent prompt MUST include these guardrails (critical):**
   - "Do NOT background your work, do NOT exit early, and do NOT defer. Complete the fix
     synchronously and return only when the file is fully edited."
   - "You own ONLY the file `<path>`. Do not read-modify any other file. Do not commit,
     push, or run git — the coordinator handles that."
   - "Return your result as a compact list mapping each thread_id to a one-line reply."

3. After ALL sub-agents have returned, you (the coordinator) integrate their results and
   run the gates from the working directory:
   - Run tests: `pixi run python -m pytest tests/ -v`
   - Run pre-commit: `pre-commit run --all-files`
   - Fix any issues found (you may edit files directly at this stage).
   - Commit all changes (do NOT push).

4. Trust but verify: only mark a thread `addressed` if the owning sub-agent actually
   edited the file for it. If a sub-agent claimed a fix but the file is unchanged for that
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


def get_address_review_prompt(
    pr_number: int,
    issue_number: int,
    worktree_path: str,
    threads_json: str,
) -> str:
    """Get the address review prompt for fixing inline review thread feedback.

    ``threads_json`` is fenced as untrusted (it embeds reviewer comment bodies
    sourced from GitHub).

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        worktree_path: Path to the git worktree containing the PR branch
        threads_json: JSON string of unresolved review threads (array of thread dicts)

    Returns:
        Formatted address review prompt

    """
    nonce = secrets.token_hex(8).upper()
    return ADDRESS_REVIEW_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        threads_json_block=_fence_untrusted("THREADS_JSON", threads_json, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
    )
