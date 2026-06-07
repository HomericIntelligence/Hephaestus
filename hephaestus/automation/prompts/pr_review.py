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
2. **Auto-merge deferred until implementation GO:** the Auto-merge State block
   above contains a single line. During implementation review it MUST read
   `auto_merge_enabled=false`. If it reads `auto_merge_enabled=true`, NOGO with
   a note explaining auto-merge must stay disabled until this review returns GO
   and the automation applies `state:implementation-go`.
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

**Comment severity (MANDATORY — tag every inline comment):**

Classify each inline comment with a `severity`:
- `critical` — correctness/security bug, data loss, or a policy violation.
- `major` — a real design/maintainability problem that should be fixed before merge.
- `minor` — a small but genuine improvement (naming, missing edge case, light duplication).
- `nitpick` — purely cosmetic / stylistic / subjective preference with no functional impact.

{nitpick_directive}

**Output format (verdict contract — MANDATORY):**
The review prose + inline comments explain *why*; the verdict line is a binary
gate. Write your analysis in prose, then end your response with exactly one of
the two verdict lines below (the parser takes the LAST matching line):

Verdict: GO — Policy passes, auto-merge is deferred, and code is acceptable.
Verdict: NOGO — Policy violation OR fundamental code problem (explain in the review).

After the verdict line, emit a single fenced JSON block:

```json
{{"comments": [
  {{"path": "...", "line": 1, "side": "RIGHT", "severity": "minor", "body": "..."}}
], "summary": "..."}}
```

Rules for the JSON block:
- `comments`: array of inline comment objects. Each must have:
  - `path`: file path relative to repo root (string)
  - `line`: line number in the file (integer, must be a changed line in the diff)
  - `side`: always `"RIGHT"` for new code
  - `severity`: one of `"critical"`, `"major"`, `"minor"`, `"nitpick"` (see above)
  - `body`: the review comment text (string)
- `summary`: overall review verdict, max 200 characters. If any policy check
  failed, this MUST start with `POLICY VIOLATION:` followed by the failing
  check name(s) (e.g. `POLICY VIOLATION: Closes, signed-commits`).
- If there are no inline comments AND all policy checks pass, emit:
  `{{"comments": [], "summary": "LGTM"}}`
- Emit only one JSON block, at the very end of your response (the parser takes the LAST one).
"""


#: Default (nitpick-suppressed) directive. Keeps the review focused on
#: actionable findings — matches the strict rubric's D3 "omit filler" stance.
_NITPICK_SUPPRESS = (
    "By DEFAULT, DO NOT emit `nitpick`-severity comments at all — omit them "
    "entirely. Only emit `critical`, `major`, and `minor` comments. A cosmetic "
    "preference is not worth a review thread unless explicitly requested."
)

#: Opt-in directive when ``--nitpick`` is set: nitpicks are welcome.
_NITPICK_INCLUDE = (
    "Nitpick mode is ENABLED: you MAY emit `nitpick`-severity comments in "
    "addition to the others. Still tag them `nitpick` so they can be filtered "
    "downstream."
)


def get_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    ci_status: str = "",
    pr_description: str = "",
    auto_merge_enabled: bool = False,
    commits_signing_state: list[dict[str, Any]] | None = None,
    include_nitpicks: bool = False,
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
        include_nitpicks: When False (default), the reviewer is told to OMIT
            ``nitpick``-severity comments entirely. When True (``--nitpick``),
            nitpick comments are re-enabled. Either way every emitted comment
            carries a ``severity`` tag (#1083).

    Returns:
        Formatted PR review analysis prompt

    """
    nonce = secrets.token_hex(8).upper()
    auto_merge_state = f"auto_merge_enabled={'true' if auto_merge_enabled else 'false'}"
    signing_state_json = json.dumps(commits_signing_state or [])
    nitpick_directive = _NITPICK_INCLUDE if include_nitpicks else _NITPICK_SUPPRESS
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
        nitpick_directive=nitpick_directive,
    )


REVIEW_VALIDATION_PROMPT = """
You are VALIDATING whether prior review comments on PR #{pr_number}
(issue #{issue_number}) were actually addressed by the current diff.

You are NOT performing a fresh review. Do not raise new concerns. Your ONLY
job is, for each PRIOR review comment below, to decide whether the CURRENT
diff actually resolves it.

{untrusted_notice}

**Prior review comments to validate (untrusted):**
{prior_comments_block}

The block above is a JSON array where each element has:
- `thread_id`: opaque id of the review thread — echo it back verbatim
- `path`: file path the comment was made on (may be empty for PR-level)
- `line`: line number the comment pointed at (integer or null)
- `body`: the original reviewer comment text

**Current diff (untrusted):**
{diff_block}

---

For EACH prior comment, judge against the current diff:
- ADDRESSED — the diff changes the cited code in a way that resolves the
  comment's concern. When in doubt that a change truly resolves it, treat it as
  NOT addressed (false "addressed" is worse than a redundant re-open).
- NOT ADDRESSED — the cited code is unchanged, or the change does not actually
  resolve the concern the comment raised.

**Output format:**
Write your reasoning in prose. At the very end, emit a single fenced JSON block
listing ONLY the comments that are NOT addressed:

```json
{{"unaddressed": [
  {{"thread_id": "...", "path": "...", "line": 1,
    "original_body": "...", "detail": "why still unaddressed"}}
]}}
```

Rules:
- `unaddressed`: array of the prior comments the diff does NOT resolve. Echo the
  comment's `thread_id` VERBATIM (this is how the thread is matched — it must be
  exact); include the original `path`/`line`; `original_body` is the original
  comment text (verbatim or trimmed); `detail` states concretely what is still
  missing.
- If every prior comment is addressed, emit `{{"unaddressed": []}}`.
- Emit only one JSON block, at the very end (the parser takes the LAST one).
"""


def get_review_validation_prompt(
    pr_number: int,
    issue_number: int,
    prior_comments_json: str,
    diff_text: str = "",
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

    Returns:
        Formatted review-validation prompt.

    """
    nonce = secrets.token_hex(8).upper()
    return REVIEW_VALIDATION_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        prior_comments_block=_fence_untrusted("PRIOR_COMMENTS", prior_comments_json, nonce),
        diff_block=_fence_untrusted("DIFF", diff_text, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
    )


COMMENT_DIFFICULTY_PROMPT = """
You are CLASSIFYING the difficulty of unresolved PR review comments on issue
#{issue_number}, so the right model tier can be assigned to fix each one.

You are NOT reviewing the code and NOT fixing anything. For each comment, judge
how hard the FIX is, using these tiers:

- `simple` — mechanical / local: a typo, rename, doc tweak, import, one-line
  guard, or formatting. A junior model can do it from the comment alone.
- `medium` — a localized logic change, a small refactor, handling an edge case,
  or a test addition that needs reading one or two functions.
- `hard` — cross-cutting or subtle: a design change spanning files, a tricky
  correctness/concurrency/security fix, or anything needing real reasoning about
  invariants. When genuinely unsure between two tiers, pick the HIGHER one.

{untrusted_notice}

**Review comments to classify (untrusted):**
{comments_block}

The block above is a JSON array; each element has `thread_id`, `path`, `line`,
and `body`.

**Output format:**
Write brief reasoning in prose, then end with exactly one fenced JSON block
mapping each `thread_id` to its difficulty:

```json
{{"classifications": {{"<thread_id>": "simple|medium|hard"}}}}
```

Rules:
- Include EVERY `thread_id` from the input exactly once.
- Use only the three labels `simple`, `medium`, `hard`.
- Emit only one JSON block, at the very end (the parser takes the LAST one).
"""


def get_comment_difficulty_prompt(
    issue_number: int,
    comments_json: str,
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

    Returns:
        Formatted comment-difficulty classification prompt.

    """
    nonce = secrets.token_hex(8).upper()
    return COMMENT_DIFFICULTY_PROMPT.format(
        issue_number=issue_number,
        comments_block=_fence_untrusted("REVIEW_COMMENTS", comments_json, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
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
