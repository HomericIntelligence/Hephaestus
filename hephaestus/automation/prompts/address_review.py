"""Address-review prompt: apply fixes for unresolved PR review threads."""

import secrets

from ._shared import _UNTRUSTED_NOTICE, _fence_untrusted

ADDRESS_REVIEW_PROMPT = """
Address the review threads for PR #{pr_number} (issue #{issue_number}).

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

**Your task:**
For each thread, read the file at `path` in the working directory and apply the necessary
code fix. After fixing all addressable threads:

1. Run tests: `pixi run python -m pytest tests/ -v`
2. Run pre-commit: `pre-commit run --all-files`
3. Fix any issues found
4. Commit all changes (do NOT push)

**Output format:**
Write your fix notes in prose. At the very end of your response, emit a single fenced JSON block:

```json
{{"addressed": ["<thread_id>", ...], "replies": {{"<thread_id>": "one-line reply"}}}}
```

Rules for the JSON block:
- `addressed`: array of thread_id strings for threads you actually fixed in code
  (any thread_id not in the unresolved-set we presented is dropped silently)
- `replies`: mapping of thread_id to a one-line reply describing what you changed
- Only include threads you genuinely fixed. Leave unaddressable threads out of `addressed`.
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
