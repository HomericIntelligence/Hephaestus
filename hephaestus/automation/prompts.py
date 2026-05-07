"""Prompt templates for Claude Code automation.

Contains templates for:
- Issue implementation guidance
- Planning guidance
- PR descriptions
"""

IMPLEMENTATION_PROMPT = """
Implement GitHub issue #{issue_number}.

**Working Directory:** {worktree_path}
**Branch:** {branch_name}

**Issue Title:** {issue_title}

**Issue Description:**
{issue_body}

---

**Implementation Context:**
- Run `gh issue view {issue_number} --comments` to read the full plan and any comments
- Follow the project's Python conventions and type hint all function signatures

**Critical Requirements:**
1. Read the issue description and any existing plan carefully
2. Follow existing code patterns in hephaestus/
3. Write tests in tests/ using pytest
4. Run tests with: pixi run python -m pytest tests/ -v
5. Ensure all tests pass before finishing
6. Follow the code quality guidelines in CLAUDE.md

**Testing:**
- Write unit tests for new functionality
- Ensure existing tests still pass
- Use pytest fixtures and parametrize where appropriate

**Code Quality:**
- Type hint all function signatures
- Write docstrings for public APIs
- Follow PEP 8 style guidelines
- Keep solutions simple and focused

**File Handling:**
- DO NOT create backup files (.orig, .bak, .swp, etc.)
- DO NOT leave temporary or editor backup files
- Clean up any backup files before finishing
- Only stage actual implementation files

**Git Workflow:**
After implementation is complete and tests pass:
1. Create a git commit using the /commit-commands:commit skill
   - Use a descriptive commit message following conventional commits format
   - Include "Closes #{issue_number}" in the commit message
2. Push the changes to origin
3. Create a pull request using the /commit-commands:commit-push-pr skill
   - Link the PR to this issue
   - Include a clear summary of changes and testing done

When you're done, the PR should be created and ready for review.
"""

PLAN_PROMPT = """
Create an implementation plan for GitHub issue #{issue_number}.

**Your plan should include:**
1. **Objective** - Brief description of what needs to be done
2. **Approach** - High-level strategy and key decisions
3. **Files to Create** - New files needed with descriptions
4. **Files to Modify** - Existing files to change with specific changes
5. **Implementation Order** - Numbered sequence of steps
6. **Verification** - How to test and verify the implementation
7. **Skills Used** - List skills invoked during planning AND any team
   knowledge base skills referenced in the Prior Learnings section above

**Guidelines:**
- Be specific about file paths and function names
- Reference existing patterns in hephaestus/ to follow
- Include test file creation in the plan
- Consider dependencies and integration points
- Keep the plan focused on the issue requirements
- In the Skills Used section, include both skills you invoked directly
  and any team knowledge base skills provided in the Prior Learnings
- Document which skills you used during planning so implementers know what context was gathered

**Format:**
Use markdown with clear sections and bullet points.
"""

ADVISE_PROMPT = """
Search the team knowledge base for relevant prior learnings before planning this issue.

**Issue:** #{issue_number}: {issue_title}

{issue_body}

---

**Your task:**
1. Read the skills marketplace: {marketplace_path}
2. Search for plugins matching this issue's topic by:
   - Keywords in plugin names and descriptions
   - Tags and categories
   - Similar problem domains
3. For each relevant plugin, read its SKILL.md file to understand:
   - What worked (successful approaches)
   - What failed (common pitfalls)
   - Recommended parameters and configurations
   - Related patterns and conventions

**Output format:**
## Related Skills
| Plugin | Category | Relevance |
|--------|----------|-----------|
| plugin-name | category | Why it's relevant |

## What Worked
- Successful approach 1
- Successful approach 2

## What Failed
- Common pitfall 1 (from plugin X)
- Common pitfall 2 (from plugin Y)

## Recommended Parameters
- Parameter/configuration 1
- Parameter/configuration 2

If no relevant skills are found, output:
## Related Skills
None found

**Important:** Only return findings from the actual marketplace. Do not speculate or invent skills.
"""

FOLLOW_UP_PROMPT = """
Review your work on issue #{issue_number} and identify any follow-up tasks,
enhancements, or edge cases discovered during implementation.

**Output format:**
Return a JSON array of follow-up items (max 5). Each item must have:
- `title`: Brief, specific title (under 70 characters)
- `body`: Detailed description of the follow-up work
- `labels`: Array of relevant labels from:
  ["enhancement", "bug", "testing", "documentation", "refactor", "research"]

If there are no follow-up items, return an empty array: `[]`

**Example:**
```json
[
  {{
    "title": "Add edge case handling for empty input",
    "body": "During implementation, discovered that empty input returns
misleading error. Should add validation and specific error message.",
    "labels": ["enhancement", "bug"]
  }},
  {{
    "title": "Add integration tests for new feature",
    "body": "Current tests only cover unit level. Need integration tests
to verify end-to-end behavior with real GitHub API.",
    "labels": ["testing"]
  }}
]
```

**Guidelines:**
- Only include concrete, actionable items discovered during this implementation
- Don't include speculative future features
- Keep descriptions concise but specific enough for another developer
- Max 5 items - prioritize the most important
- Return `[]` if no follow-ups needed
"""


def get_implementation_prompt(
    issue_number: int,
    issue_title: str = "",
    issue_body: str = "",
    branch_name: str = "",
    worktree_path: str = "",
) -> str:
    """Get the implementation prompt for an issue.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (optional, for backward compatibility)
        issue_body: Issue body/description (optional, for backward compatibility)
        branch_name: Git branch name (optional, for backward compatibility)
        worktree_path: Working directory path (optional, for backward compatibility)

    Returns:
        Formatted implementation prompt

    """
    return IMPLEMENTATION_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        branch_name=branch_name,
        worktree_path=worktree_path,
    )


def get_plan_prompt(issue_number: int) -> str:
    """Get the planning prompt for an issue."""
    return PLAN_PROMPT.format(issue_number=issue_number)


def get_advise_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    marketplace_path: str,
) -> str:
    """Get the advise prompt for searching team knowledge.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        issue_body: Issue body/description
        marketplace_path: Path to marketplace.json

    Returns:
        Formatted advise prompt

    """
    return ADVISE_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=marketplace_path,
    )


def get_follow_up_prompt(issue_number: int) -> str:
    """Get the follow-up prompt for identifying future work.

    Args:
        issue_number: GitHub issue number

    Returns:
        Formatted follow-up prompt

    """
    return FOLLOW_UP_PROMPT.format(issue_number=issue_number)


REVIEW_ANALYSIS_PROMPT = """
Analyze PR #{pr_number} (linked to issue #{issue_number}) and produce a structured fix plan.

**Working Directory:** {worktree_path}

**Issue Description:**
{issue_body}

**PR Description:**
{pr_description}

**CI Status:**
{ci_status}

**CI Failure Logs:**
{ci_logs}

**Review Comments:**
{review_comments}

**PR Diff (summary):**
{pr_diff}

---

**Your task:**
Read the code in the working directory, review the information above, and produce a structured
fix plan.

**Output format (required):**

## Summary
Brief description of the overall state of the PR and what needs to be fixed.

## Problems Found
For each problem:
- **Problem:** Description of the issue
  - **Source:** Where it comes from (CI failure / review comment / code issue)
  - **Fix:** Specific steps to resolve it

## Fix Order
Numbered sequence of fixes to apply (in dependency order).

## Verification
How to verify each fix is correct (tests to run, commands to execute).

**Guidelines:**
- Be specific about file paths and line numbers
- Reference the actual code in the worktree, not just the diff
- If no problems are found, say so explicitly in Summary and leave Problems Found empty
- Focus on actionable fixes, not general advice
"""

REVIEW_FIX_PROMPT = """
Implement the fixes described in the plan below for PR #{pr_number} (issue #{issue_number}).

**Working Directory:** {worktree_path}

**Fix Plan:**
{plan}

---

**Your task:**
Implement all fixes from the plan above. After implementing:

1. Run tests: `pixi run python -m pytest tests/ -v`
2. Run pre-commit: `pre-commit run --all-files`
3. Fix any issues found by tests or pre-commit
4. Commit all changes (but do NOT push — the script will push)

**Commit message format:**
```
fix: Address review feedback for PR #{pr_number}

Closes #{issue_number}

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

**Critical requirements:**
- Only commit actual implementation files (no .env, .secret, credentials, etc.)
- Do NOT push to origin — the script handles pushing
- Ensure all tests pass before committing
- Follow existing code patterns in hephaestus/

**File handling:**
- DO NOT create backup files (.orig, .bak, .swp, etc.)
- Clean up any temporary files before committing
"""


def get_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    ci_status: str = "",
    ci_logs: str = "",
    review_comments: str = "",
    pr_description: str = "",
    worktree_path: str = "",
) -> str:
    """Get the PR review analysis prompt.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        pr_diff: PR diff output
        issue_body: Issue body/description
        ci_status: CI check status summary
        ci_logs: CI failure log output
        review_comments: PR review and inline comments
        pr_description: PR description body
        worktree_path: Working directory path

    Returns:
        Formatted analysis prompt

    """
    return REVIEW_ANALYSIS_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff=pr_diff,
        issue_body=issue_body,
        ci_status=ci_status,
        ci_logs=ci_logs,
        review_comments=review_comments,
        pr_description=pr_description,
        worktree_path=worktree_path,
    )


def get_review_fix_prompt(
    pr_number: int,
    issue_number: int,
    plan: str = "",
    worktree_path: str = "",
) -> str:
    """Get the PR fix implementation prompt.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        plan: Fix plan from analysis session
        worktree_path: Working directory path

    Returns:
        Formatted fix prompt

    """
    return REVIEW_FIX_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        plan=plan,
        worktree_path=worktree_path,
    )


def get_pr_description(
    issue_number: int,
    summary: str,
    changes: str,
    testing: str,
) -> str:
    """Generate a PR description.

    Args:
        issue_number: GitHub issue number
        summary: Brief summary of changes
        changes: Detailed list of changes
        testing: Testing information

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

Generated with [Claude Code](https://claude.com/claude-code)
"""


PLAN_REVIEW_PROMPT = """
Review the implementation plan for GitHub issue #{issue_number}.

**Issue Title:** {issue_title}

**Issue Description:**
{issue_body}

**Proposed Plan:**
{plan_text}

---

**Your task:**
Evaluate the plan above against the issue requirements. Consider:
1. Does the plan fully address the issue requirements?
2. Are the proposed changes well-scoped and safe?
3. Are there missing steps, risky approaches, or ambiguities?
4. Are the file paths and function names concrete and correct?

**Output format:**
Write a markdown review with your analysis. End your response with exactly one of the
following verdict lines (including the bold markers):

**Verdict: APPROVED** — Plan is sound and ready to implement.
**Verdict: REVISE** — Plan needs changes before implementation (explain what).
**Verdict: BLOCK** — Plan has a fundamental problem that prevents implementation (explain why).
"""

PR_REVIEW_ANALYSIS_PROMPT = """
Analyze PR #{pr_number} linked to issue #{issue_number}.

**Issue Description:**
{issue_body}

**PR Description:**
{pr_description}

**CI Status:**
{ci_status}

**PR Diff:**
{pr_diff}

---

**Your task:**
Review the PR for correctness, completeness, and code quality. Identify any issues that should
be addressed as inline review comments.

**Output format:**
Write your analysis in prose. At the very end of your response, emit a single fenced JSON block:

```json
{{"comments": [{{"path": "...", "line": 1, "side": "RIGHT", "body": "..."}}], "summary": "..."}}
```

Rules for the JSON block:
- `comments`: array of inline comment objects. Each must have:
  - `path`: file path relative to repo root (string)
  - `line`: line number in the file (integer, must be a changed line in the diff)
  - `side`: always `"RIGHT"` for new code
  - `body`: the review comment text (string)
- `summary`: overall review verdict, max 200 characters
- If there are no inline comments, emit: `{{"comments": [], "summary": "LGTM"}}`
- Emit only one JSON block, at the very end of the response.
"""

ADDRESS_REVIEW_PROMPT = """
Address the review threads for PR #{pr_number} (issue #{issue_number}).

**Working Directory:** {worktree_path}

**Review Threads to Address:**
{threads_json}

The threads_json above is a JSON array where each element has:
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
- `replies`: mapping of thread_id to a one-line reply describing what you changed
- Only include threads you genuinely fixed. Leave unaddressable threads out of `addressed`.
- Emit only one JSON block, at the very end of the response.
"""


def get_plan_review_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
) -> str:
    """Get the plan review prompt for evaluating an issue implementation plan.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        issue_body: Issue body/description
        plan_text: The full plan text to review

    Returns:
        Formatted plan review prompt

    """
    return PLAN_REVIEW_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        plan_text=plan_text,
    )


def get_pr_review_analysis_prompt(
    pr_number: int,
    issue_number: int,
    pr_diff: str = "",
    issue_body: str = "",
    ci_status: str = "",
    pr_description: str = "",
) -> str:
    """Get the PR review analysis prompt for generating inline review comments.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        pr_diff: PR diff output
        issue_body: Issue body/description
        ci_status: CI check status summary
        pr_description: PR description body

    Returns:
        Formatted PR review analysis prompt

    """
    return PR_REVIEW_ANALYSIS_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        pr_diff=pr_diff,
        issue_body=issue_body,
        ci_status=ci_status,
        pr_description=pr_description,
    )


def get_address_review_prompt(
    pr_number: int,
    issue_number: int,
    worktree_path: str,
    threads_json: str,
) -> str:
    """Get the address review prompt for fixing inline review thread feedback.

    Args:
        pr_number: GitHub PR number
        issue_number: Linked GitHub issue number
        worktree_path: Path to the git worktree containing the PR branch
        threads_json: JSON string of unresolved review threads (array of thread dicts)

    Returns:
        Formatted address review prompt

    """
    return ADDRESS_REVIEW_PROMPT.format(
        pr_number=pr_number,
        issue_number=issue_number,
        worktree_path=worktree_path,
        threads_json=threads_json,
    )


# ---------------------------------------------------------------------------
# Iteration-aware review prompts (R0/R1/R2) used by the strict review loops
# in planner.py and implementer.py.
# ---------------------------------------------------------------------------

# Rubric block embedded into every loop-review prompt. Mirrors the philosophy
# of the `review-pr-strict` skill at:
#   ~/.claude/plugins/marketplaces/ProjectHephaestus/skills/review-pr-strict/SKILL.md
# Embedded inline so the spawned reviewer process does not depend on the plugin
# being autoloaded — works in any environment that has `claude --print`.
_STRICT_REVIEW_RUBRIC = """
You are a ruthlessly thorough technical reviewer. Apply this rubric per the
`review-pr-strict` skill (rubric summarized below — refer to the full skill at
`~/.claude/plugins/marketplaces/ProjectHephaestus/skills/review-pr-strict/SKILL.md`
if available):

GRADING (every dimension starts at F; A must be EARNED with concrete evidence):
- A  (93-100%) Exemplary, RARE
- B  (80-89%)  Solid with notable gaps
- C  (70-79%)  Mediocre / multiple gaps
- D  (60-69%)  Poor / fundamental practices missing
- F  (<60%)    Failing / misaligned / dangerous

ANTI-INFLATION RULES (mandatory):
- DEFAULT IS F. Find concrete evidence to justify ANY upgrade.
- A requires ZERO critical or major findings.
- B requires ZERO critical findings and ≤1 major finding.
- "It looks done" is NOT sufficient.
- Do NOT give credit for plans, TODOs, or future follow-up issues — grade what
  THIS artifact delivers right now.
- Do NOT round up.

EVALUATE THESE DIMENSIONS:
1. Alignment with the issue's stated requirements
2. Completeness — does the artifact cover every acceptance criterion?
3. Correctness — file paths, function names, API calls actually exist
4. Risk / safety — no destructive ops, irreversible decisions, security holes
5. Scope discipline — KISS / YAGNI; no speculative work
6. Verification plan — concrete steps the reviewer can run to confirm
"""

_STRICT_REVIEW_OUTPUT_FORMAT = """
**Required output format — MUST end with these exact lines:**

```
Grade: <A|B|C|D|F>
Verdict: <GO|NOGO>
```

GO is reserved for cases where you are CONFIDENT the artifact is ready as-is.
Any major finding, missing dimension coverage, or "needs another iteration"
critique → Verdict: NOGO. When in doubt, NOGO.
"""


PLAN_LOOP_REVIEW_PROMPT = """
{rubric}

# Iteration {iteration_label}

You are reviewing the implementation plan for GitHub issue #{issue_number}.
This is iteration {iteration} of a maximum 3-iteration review loop. {iteration_guidance}

**Issue Title:** {issue_title}

**Issue Description:**
{issue_body}

---

**Current Plan:**
{plan_text}

---

**Learnings captured during planning:**
{learnings}
{prior_review_block}
---

Review the plan above against the issue requirements and the rubric. Cite
specific paragraphs of the plan or sections of the issue when justifying
findings. After your analysis, output your verdict.

{output_format}
"""


IMPL_LOOP_REVIEW_PROMPT = """
{rubric}

# Iteration {iteration_label}

You are reviewing the implementation for GitHub issue #{issue_number}.
This is iteration {iteration} of a maximum 3-iteration review loop. {iteration_guidance}

**Issue Title:** {issue_title}

**Issue Description:**
{issue_body}

---

**Diff produced by the implementer (against base branch):**
```diff
{diff_text}
```

---

**Files changed:**
{files_changed}
{prior_review_block}
---

Review the diff against the issue requirements and the rubric. Cite specific
file:line locations when justifying findings. Watch for: missing tests,
incomplete error handling, unaddressed acceptance criteria, scope creep,
and risky changes that the issue did not request.

{output_format}
"""


def _iteration_label(iteration: int) -> str:
    """Return a human-readable iteration label for review prompts."""
    return {0: "R0 (Initial review)", 1: "R1 (Re-review)", 2: "R2 (Final review)"}.get(
        iteration, f"R{iteration}"
    )


def _iteration_guidance(iteration: int) -> str:
    """Return guidance text emphasizing the iteration's role."""
    if iteration == 0:
        return "Treat this as a fresh review — no prior context."
    if iteration == 1:
        return (
            "The previous iteration was NOGO. Verify whether the issues raised then have "
            "actually been resolved in this iteration."
        )
    return (
        "This is the FINAL iteration. After this review the loop terminates. Be "
        "decisive — emit an unambiguous Grade and Verdict."
    )


def _prior_review_block(prior_review: str | None) -> str:
    """Format the prior review (if any) as a context block."""
    if not prior_review:
        return ""
    return (
        "\n---\n\n**Prior review (from previous iteration) — verify these findings "
        f"have been addressed:**\n\n{prior_review}\n"
    )


def get_plan_loop_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
    learnings: str,
    iteration: int,
    prior_review: str | None,
) -> str:
    """Build the iteration-aware plan-loop review prompt.

    Args:
        issue_number: GitHub issue number.
        issue_title: Issue title.
        issue_body: Full issue body.
        plan_text: Plan to review.
        learnings: Learnings captured by the planner this iteration.
        iteration: Iteration index (0, 1, or 2).
        prior_review: Previous iteration's review text, or ``None`` on iter 0.

    Returns:
        Formatted prompt for a fresh reviewer session.

    """
    return PLAN_LOOP_REVIEW_PROMPT.format(
        rubric=_STRICT_REVIEW_RUBRIC.strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        plan_text=plan_text,
        learnings=learnings or "_(no learnings captured this iteration)_",
        prior_review_block=_prior_review_block(prior_review),
        output_format=_STRICT_REVIEW_OUTPUT_FORMAT.strip(),
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
    return IMPL_LOOP_REVIEW_PROMPT.format(
        rubric=_STRICT_REVIEW_RUBRIC.strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        diff_text=diff_text or "_(no diff produced)_",
        files_changed=files_changed or "_(no files changed)_",
        prior_review_block=_prior_review_block(prior_review),
        output_format=_STRICT_REVIEW_OUTPUT_FORMAT.strip(),
    )


# Prompt the implementer receives when resuming its session to address a
# NoGo review verdict. Used on iterations 1 and 2 of the impl loop.
IMPL_RESUME_FEEDBACK_PROMPT = """
The independent reviewer for issue #{issue_number} returned **{verdict}** on
iteration {prev_iteration} with the following critique:

---

{review_text}

---

Address every concrete finding above. Update the code (and tests, if needed)
in this same working directory. Do NOT commit or push — those phases run
after the review loop terminates.

When done, summarize what you changed in 3-5 bullet points so the next
review can verify the fixes were applied.
"""


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
    return IMPL_RESUME_FEEDBACK_PROMPT.format(
        issue_number=issue_number,
        prev_iteration=prev_iteration,
        verdict=verdict,
        review_text=review_text,
    )
