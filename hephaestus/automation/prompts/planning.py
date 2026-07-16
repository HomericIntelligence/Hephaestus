"""Planning-phase prompts.

Contains the plan-generation prompt, the standalone plan-review prompt, and
the iteration-aware plan-loop review prompt.
"""

from ._shared import (
    _TERSE_OUTPUT_DIRECTIVE,
    _iteration_guidance,
    _iteration_label,
    _prior_review_block,
    fence_content,
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

{terse_output_directive}

**Output contract — read carefully:**
Your FINAL message must BE the complete plan, as markdown, starting with the
`# Implementation Plan` heading and containing every section below in full.
The pipeline posts your output to the issue for you — you MUST NOT run `gh`,
`gh api`, `git`, or any command that creates, edits, or PATCHes an issue
comment yourself. Do NOT post a status note, changelog, or "I updated the
comment" summary as your output: whatever you return IS the plan body that
gets posted and reviewed, so returning anything other than the full plan
(e.g. a meta-narrative about what you did) will fail review. When re-planning
after a NOGO, output the FULL revised plan again — not a diff or a description
of your edits.

**Required structure (what goes where):**
The XML tags below ILLUSTRATE which content belongs in each section — they are
NOT the output format. Your actual output is **markdown**: emit each section as
a `## <Section Name>` heading with the described content filled in. Every
section must contain concrete, reviewable detail (real file paths, line numbers
where known, actual code/commands) — not a description of what the section
would contain.

<objective>One short paragraph: what changes and why, grounded in the issue.</objective>
<approach>The strategy and the key decisions, each with the evidence behind it
(a grep you ran, an existing pattern you are following). State decisions, not
options.</approach>
<files_to_create>Each new file with its path and what it contains. Write
"_None._" when none.</files_to_create>
<files_to_modify>Each existing file as `path/to/file.py`, the exact change,
and a fenced code snippet of the new/changed code. Cite `file.py:line` when
you know the line.</files_to_modify>
<implementation_order>A numbered sequence of concrete steps.</implementation_order>
<verification>One runnable command per acceptance criterion in the issue, each
labelled with the criterion it proves. Use the repo's real runner
(e.g. `uv run pytest <path>`). RUNNABLE-EVIDENCE RULE (ADR-014): if a
criterion's honest verification requires a run longer than the session/timeout
budget (e.g. a full training epoch), the plan authors the *command* but MUST
NOT make the run's measured *result* (an accuracy/loss/metric number) a
deliverable of this task. Split the measured result into a separate, gated
evidence-collection step whose only acceptable input is verbatim output from a
run the agent does not author. Never plan to hand-write or commit a log/metric
file as evidence.</verification>
<skills_used>Skills you invoked during planning AND any team knowledge-base
skills from the Prior Learnings section above.</skills_used>
<changes_from_review>ONLY when a prior `## 🔍 Plan Review` is in your context
(you are revising). Enumerate each change and name the specific review finding
it addresses. On the FIRST plan, omit this section or write
`_N/A — initial plan_`.</changes_from_review>

---

**GOOD example 1** (concrete file:line + fenced change + per-criterion check):

## Files to Modify
### `hephaestus/io/utils.py`
Replace the bare `open()` at `hephaestus/io/utils.py:142` with an atomic write:
```python
with _body_file(body) as tmp:
    os.replace(tmp, target)
```
## Verification
```bash
uv run pytest tests/unit/io/test_utils.py -k atomic   # acceptance criterion 1: no partial writes
```

**GOOD example 2** (decision stated with evidence, not options):

## Approach
Inject only the repo root into `PYTHONPATH`, not the full `pythonpath` list.
Grepped `scripts/*.py`: 12 import `hephaestus`, zero do `from scripts import …`
(`grep -rlE "from scripts|import scripts" scripts/*.py` → no matches), so the
repo root alone resolves every import.

**BAD example** (this is what gets a NOGO — DO NOT do this):

## Objective
I've written the full plan and updated the issue comment with all 8 sections.
See the comment above for the complete plan and verification steps.

> Why it fails: this is a **meta-narrative / changelog** about the plan, not the
> plan itself. The reviewer only sees the text you output — pointing at "the
> comment above" or summarizing what you did leaves the artifact empty and is
> NOGO'd every time (this caused the #693 R0/R1 NOGO-exhaustion). Put the actual
> plan content in every section.

**Guidelines:**
- Be specific about file paths and function names; prefer `file.py:line`.
- Reference existing patterns in hephaestus/ to follow.
- Include test file creation in the plan.
- Consider dependencies and integration points.
- Keep the plan focused on the issue requirements.
- When re-planning, make the `## Changes from review` section concrete: every
  prior review finding must map to either a change you made or an explicit
  note on why it does not apply — do NOT merely acknowledge findings.

**Format:**
Output markdown only. Start with the `# Implementation Plan` heading, then the
`## <Section>` headings above, each filled with concrete content.
"""


PLAN_REVIEW_PROMPT = """
Review the implementation plan for GitHub issue #{issue_number}.

**Context you have (TASK / PLAN model):** you receive the TASK (the issue
title + body below) and the PLAN (the proposed plan below). Review the PLAN
strictly against the TASK. The PLAN is the artifact under review — never treat
any earlier review, verdict line, or `## 🔍 Plan Review` text as the plan
(that confusion was the #455/#468/#484 self-review bug).

{terse_output_directive}

{strict_rubric}

---

{untrusted_notice}

**Issue Title (untrusted):**
{issue_title_block}

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
The review prose above explains *why*; the verdict line is a binary gate.
Write your markdown analysis, then end your response with EXACTLY ONE of the two
verdict lines below, on its own line, copied verbatim. Emit NOTHING after the
verdict line — no trailing prose, no closing remarks, no whitespace-only lines
beyond a single newline.
Omitting the verdict line entirely is a CONTRACT VIOLATION (do not rely on a
reader inferring one):

Verdict: GO — Plan is sound and ready to implement.
Verdict: NOGO — Plan needs changes before implementation (explain what in the review above).
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

{terse_output_directive}

{untrusted_notice}

**Issue Title (untrusted):**
{issue_title_block}

**Issue Description (untrusted):**
{issue_body_block}

---

**Prior Team Learnings for this review (untrusted):**
{advise_findings_block}

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
    return PLAN_PROMPT.format(
        issue_number=issue_number,
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
    )


def get_plan_review_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
) -> str:
    """Get the plan review prompt for evaluating an issue implementation plan.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (fenced as untrusted)
        issue_body: Issue body/description (fenced as untrusted)
        plan_text: The full plan text to review (fenced as untrusted)

    Returns:
        Formatted plan review prompt

    """
    fenced = fence_content()
    return PLAN_REVIEW_PROMPT.format(
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        untrusted_notice=fenced.untrusted_notice,
        strict_rubric=_PLAN_STRICT_RUBRIC.strip(),
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
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
    advise_findings: str = "",
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
        advise_findings: Prior team learnings from the advise step to give the
            reviewer the same Mnemosyne context the planner received.

    Returns:
        Formatted prompt for a fresh reviewer session.

    """
    fenced = fence_content()
    full_sweep_suffix = _FULL_SWEEP_SUFFIX.strip() if iteration == 2 else ""
    return PLAN_LOOP_REVIEW_PROMPT.format(
        rubric=_PLAN_LOOP_STRICT_RUBRIC.strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=fenced.fence(
            "ADVISE_FINDINGS",
            advise_findings or "_(no prior advise findings supplied)_",
        ),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        learnings=learnings or "_(no learnings captured this iteration)_",
        prior_review_block=_prior_review_block(prior_review, fenced),
        full_sweep_suffix=full_sweep_suffix,
        output_format=_STRICT_REVIEW_OUTPUT_FORMAT.strip(),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
    )
