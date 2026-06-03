"""PR review-phase prompts.

Contains the PR review analysis prompt (inline-comment generator) and the
plain PR description template.
"""

import json
import secrets
from typing import Any

from ._shared import _UNTRUSTED_NOTICE, _fence_untrusted
from ._strict_rubric import _PR_STRICT_RUBRIC

PR_REVIEW_ANALYSIS_PROMPT = """
Analyze PR #{pr_number} linked to issue #{issue_number}.

{untrusted_notice}

**Issue Description (untrusted):**
{issue_body_block}

**PR Description (untrusted):**
{pr_description_block}

**CI Status (untrusted):**
{ci_status_block}

**PR Diff (untrusted):**
{pr_diff_block}

**Auto-merge State (untrusted):**
{auto_merge_state_block}

**Commit Signing State (untrusted):**
{commits_signing_block}

---

{strict_rubric}

---

**Policy checks (MANDATORY — run these BEFORE any code-quality review):**

This repository enforces three non-negotiable PR properties. If ANY check fails,
your summary MUST begin with `POLICY VIOLATION:` and your final verdict line
MUST be `Verdict: NOGO`. Inline code-quality findings can be reported in
addition, but the NOGO verdict cannot be overridden by them.

1. **Closes #N:** the PR Description above must contain a line matching the
   regex `^Closes #\\d+\\s*$` (case-sensitive `Closes`, hash + number, on its
   own line). `Fixes`, `Resolves`, `closes`, `Closes:` do NOT satisfy the
   policy. If absent, NOGO and quote the relevant lines of the description.
2. **Auto-merge enabled:** the Auto-merge State block above contains a single
   line. If it reads `auto_merge_enabled=true`, the check passes. If it reads
   `auto_merge_enabled=false`, NOGO with a note explaining auto-merge must be
   turned on via `gh pr merge <N> --auto --squash`.
3. **Signed commits:** the Commit Signing State block above is a JSON array
   where each element is `{{"oid": "<sha>", "signature_valid": <bool>,
   "signer": "<login or null>"}}`. EVERY element must have
   `signature_valid: true`. If any commit has `signature_valid: false` or the
   array is empty, NOGO and list the offending OIDs.

If all three checks pass, proceed to code-quality review below.

---

**Code-quality review (only if policy checks pass):**

Review the PR for correctness, completeness, and code quality. Identify any issues that should
be addressed as inline review comments.

**Output format (verdict contract — MANDATORY):**
The review prose + inline comments explain *why*; the verdict line is a binary
gate. Write your analysis in prose, then end your response with exactly one of
the two verdict lines below (the parser takes the LAST matching line):

Verdict: GO — Policy passes and code is acceptable.
Verdict: NOGO — Policy violation OR fundamental code problem (explain in the review).

After the verdict line, emit a single fenced JSON block:

```json
{{"comments": [{{"path": "...", "line": 1, "side": "RIGHT", "body": "..."}}], "summary": "..."}}
```

Rules for the JSON block:
- `comments`: array of inline comment objects. Each must have:
  - `path`: file path relative to repo root (string)
  - `line`: line number in the file (integer, must be a changed line in the diff)
  - `side`: always `"RIGHT"` for new code
  - `body`: the review comment text (string)
- `summary`: overall review verdict, max 200 characters. If any policy check
  failed, this MUST start with `POLICY VIOLATION:` followed by the failing
  check name(s) (e.g. `POLICY VIOLATION: Closes, signed-commits`).
- If there are no inline comments AND all policy checks pass, emit:
  `{{"comments": [], "summary": "LGTM"}}`
- Emit only one JSON block, at the very end of your response (the parser takes the LAST one).
"""


def get_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    ci_status: str = "",
    pr_description: str = "",
    auto_merge_enabled: bool = False,
    commits_signing_state: list[dict[str, Any]] | None = None,
) -> str:
    """Get the PR review analysis prompt for generating inline review comments.

    All free-text fields are fenced as untrusted (see module docstring).

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        pr_diff: PR diff output
        issue_body: Issue body/description
        ci_status: CI check status summary
        pr_description: PR description body
        auto_merge_enabled: Whether GitHub auto-merge is currently enabled on
            the PR. Callers MUST pass the real value; the default ``False``
            exists only to keep the signature backward-compatible and will
            cause the reviewer to emit a NOGO verdict.
        commits_signing_state: List of per-commit signing summaries. Each
            element must be a dict with keys ``oid`` (str), ``signature_valid``
            (bool), and ``signer`` (str or None). Defaults to an empty list,
            which the reviewer treats as a policy failure.

    Returns:
        Formatted PR review analysis prompt

    """
    nonce = secrets.token_hex(8).upper()
    auto_merge_state = f"auto_merge_enabled={'true' if auto_merge_enabled else 'false'}"
    signing_state_json = json.dumps(commits_signing_state or [])
    return PR_REVIEW_ANALYSIS_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff_block=_fence_untrusted("PR_DIFF", pr_diff, nonce),
        issue_body_block=_fence_untrusted("ISSUE_BODY", issue_body, nonce),
        ci_status_block=_fence_untrusted("CI_STATUS", ci_status, nonce),
        pr_description_block=_fence_untrusted("PR_DESCRIPTION", pr_description, nonce),
        auto_merge_state_block=_fence_untrusted("AUTO_MERGE_STATE", auto_merge_state, nonce),
        commits_signing_block=_fence_untrusted("COMMITS_SIGNING_STATE", signing_state_json, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
        strict_rubric=_PR_STRICT_RUBRIC.strip(),
    )


def get_pr_description(
    issue_number: int,
    summary: str,
    changes: str,
    testing: str,
    generated_by: str = "ProjectHephaestus automation",
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
    # Use f-string construction instead of .format() to avoid KeyError on curly braces in content
    return f"""## Summary
{summary}

## Changes
{changes}

## Testing
{testing}

## Closes
Closes #{issue_number}

Generated by {generated_by}
"""
