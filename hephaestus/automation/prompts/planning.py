"""Planning-phase prompts.

Contains the plan-generation prompt, the standalone plan-review prompt, and
the iteration-aware plan-loop review prompt.
"""

import secrets

from ._shared import (
    _UNTRUSTED_NOTICE,
    _fence_untrusted,
    _iteration_guidance,
    _iteration_label,
    _prior_review_block,
)
from ._strict_rubric import (
    _FULL_SWEEP_SUFFIX,
    _PLAN_LOOP_STRICT_RUBRIC,
    _PLAN_STRICT_RUBRIC,
    _STRICT_REVIEW_OUTPUT_FORMAT,
)

PLAN_PROMPT = """
Create an implementation plan for GitHub issue #{issue_number}.

**Context you have (TASK / PLAN / REVIEW model):**
- The TASK — the issue title and body shown above. This is the source of
  truth for requirements; it is written externally and you never edit it.
- Any PRIOR PLAN you produced for this issue (prepended above when present).
- Any PRIOR REVIEW of that plan (a `## 🔍 Plan Review` block prepended above
  when present). When a prior review is present you are RE-PLANNING to
  address it, not starting fresh.

You produce/refresh exactly ONE `# Implementation Plan` comment on the issue
(it is upserted in place — do not write a second plan comment).

**Your plan should include:**
1. **Objective** - Brief description of what needs to be done
2. **Approach** - High-level strategy and key decisions
3. **Files to Create** - New files needed with descriptions
4. **Files to Modify** - Existing files to change with specific changes
5. **Implementation Order** - Numbered sequence of steps
6. **Verification** - How to test and verify the implementation
7. **Skills Used** - List skills invoked during planning AND any team
   knowledge base skills referenced in the Prior Learnings section above
8. **Changes from review** - ONLY when a prior `## 🔍 Plan Review` is present
   in your context above (i.e. you are revising the plan). Add a
   `## Changes from review` section that enumerates each change you made and
   names the specific review finding it addresses (e.g.
   "- Added regression test `tests/...::test_x` — addresses review finding on
   missing error-path coverage"). When there is NO prior review (this is the
   first plan), OMIT this section entirely or write `_N/A — initial plan_`.

**Guidelines:**
- Be specific about file paths and function names
- Reference existing patterns in hephaestus/ to follow
- Include test file creation in the plan
- Consider dependencies and integration points
- Keep the plan focused on the issue requirements
- In the Skills Used section, include both skills you invoked directly
  and any team knowledge base skills provided in the Prior Learnings
- Document which skills you used during planning so implementers know what context was gathered
- When re-planning, make the `## Changes from review` section concrete: every
  prior review finding must map to either a change you made or an explicit
  note on why it does not apply — do NOT merely acknowledge findings.

**Format:**
Use markdown with clear sections and bullet points.
"""


PLAN_REVIEW_PROMPT = """
Review the implementation plan for GitHub issue #{issue_number}.

**Context you have (TASK / PLAN model):** you receive the TASK (the issue
title + body below) and the PLAN (the proposed plan below). Review the PLAN
strictly against the TASK. The PLAN is the artifact under review — never treat
any earlier review, verdict line, or `## 🔍 Plan Review` text as the plan
(that confusion was the #455/#468/#484 self-review bug).

{strict_rubric}

---

{untrusted_notice}

**Issue Title (untrusted):** {issue_title}

**Issue Description (untrusted):**
{issue_body_block}

**Proposed Plan (untrusted):**
{plan_text_block}

---

**Your task:**
Evaluate the plan above against the issue requirements. Consider:
1. Does the plan fully address the issue requirements?
2. Are the proposed changes well-scoped and safe?
3. Are there missing steps, risky approaches, or ambiguities?
4. Are the file paths and function names concrete and correct?

**Output format (verdict contract — MANDATORY):**
Write a markdown review with your analysis. Then end your response with
EXACTLY ONE verdict line, on its own line, copied verbatim (including the bold
`**` markers) from the three options below. Emit NOTHING after the verdict
line — no trailing prose, no closing remarks, no whitespace-only lines beyond a
single newline. Readers parse only the LAST matching verdict line, and a
response that omits the verdict line entirely is a CONTRACT VIOLATION (do not
rely on a reader inferring one):

**Verdict: APPROVED** — Plan is sound and ready to implement.
**Verdict: REVISE** — Plan needs changes before implementation (explain what).
**Verdict: BLOCK** — Plan has a fundamental problem that prevents implementation (explain why).
"""


PLAN_LOOP_REVIEW_PROMPT = """
{rubric}

# Iteration {iteration_label}

You are reviewing the implementation plan for GitHub issue #{issue_number}.
This is iteration {iteration} of a maximum 3-iteration review loop. {iteration_guidance}

You review the PLAN below against the TASK (the issue title + description)
ONLY. The artifact under review is the **Current Plan** block — never treat a
prior review or its verdict as the plan (the #455/#468/#484 self-review bug).
Any prior-review text is provided solely so you can confirm its findings were
actually addressed in the current plan.

{untrusted_notice}

**Issue Title (untrusted):** {issue_title}

**Issue Description (untrusted):**
{issue_body_block}

---

**Current Plan (untrusted):**
{plan_text_block}

---

**Learnings captured during planning:**
{learnings}
{prior_review_block}
---

Review the plan above against the issue requirements and the rubric. Cite
specific paragraphs of the plan or sections of the issue when justifying
findings. After your analysis, output your verdict.
{full_sweep_suffix}

{output_format}
"""


def get_plan_prompt(issue_number: int) -> str:
    """Get the planning prompt for an issue."""
    return PLAN_PROMPT.format(issue_number=issue_number)


def get_plan_review_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
) -> str:
    """Get the plan review prompt for evaluating an issue implementation plan.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (interpolated as untrusted text)
        issue_body: Issue body/description (fenced as untrusted)
        plan_text: The full plan text to review (fenced as untrusted)

    Returns:
        Formatted plan review prompt

    """
    nonce = secrets.token_hex(8).upper()
    return PLAN_REVIEW_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body_block=_fence_untrusted("ISSUE_BODY", issue_body, nonce),
        plan_text_block=_fence_untrusted("PLAN_TEXT", plan_text, nonce),
        untrusted_notice=_UNTRUSTED_NOTICE,
        strict_rubric=_PLAN_STRICT_RUBRIC.strip(),
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
    nonce = secrets.token_hex(8).upper()
    full_sweep_suffix = _FULL_SWEEP_SUFFIX.strip() if iteration == 2 else ""
    return PLAN_LOOP_REVIEW_PROMPT.format(
        rubric=_PLAN_LOOP_STRICT_RUBRIC.strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body_block=_fence_untrusted("ISSUE_BODY", issue_body, nonce),
        plan_text_block=_fence_untrusted("PLAN_TEXT", plan_text, nonce),
        learnings=learnings or "_(no learnings captured this iteration)_",
        prior_review_block=_prior_review_block(prior_review),
        full_sweep_suffix=full_sweep_suffix,
        output_format=_STRICT_REVIEW_OUTPUT_FORMAT.strip(),
        untrusted_notice=_UNTRUSTED_NOTICE,
    )
