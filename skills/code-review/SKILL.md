---
name: code-review
description: Use when completing tasks, implementing major features, or before merging — dispatches a Sonnet code reviewer and guides reception of feedback with technical rigor
argument-hint: <what was implemented>
allowed-tools: [Read, Bash, Grep, Glob, Agent]
---

# Code Review

Two-part skill: **Requesting** a code review via a Sonnet reviewer agent, and **Receiving** feedback with technical rigor.

**Core principle:** Review early, review often. Verify before implementing. Technical correctness over social comfort.

---

## Part 1: Requesting Code Review

### When to Request Review

**Mandatory:**
- After completing a major feature or task
- Before merging to main
- After fixing a complex bug

**Optional but valuable:**
- When stuck (fresh perspective)
- Before refactoring (baseline check)
- After completing a myrmidon-swarm task wave

### How to Request

**1. Get the diff context:**

```bash
BASE_SHA=$(git merge-base HEAD origin/main)
HEAD_SHA=$(git rev-parse HEAD)
git log --oneline "$BASE_SHA".."$HEAD_SHA"
git diff "$BASE_SHA".."$HEAD_SHA" --stat
```

**2. Dispatch code reviewer as a Sonnet agent:**

```python
Agent(
    model="sonnet",
    description="Code review",
    prompt="""You are a Senior Code Reviewer for the HomericIntelligence ecosystem.

Review the implementation against the plan and coding standards.

## What Was Implemented
{WHAT_WAS_IMPLEMENTED}

## Plan / Requirements
{PLAN_OR_REQUIREMENTS}

## Git Range
BASE_SHA: {BASE_SHA}
HEAD_SHA: {HEAD_SHA}

## Review Dimensions

1. **Plan Alignment**: Compare implementation against requirements. Are there deviations?
   Are they justified improvements or problematic departures?

2. **Code Quality**: Adherence to SOLID principles, proper error handling, type safety,
   naming conventions, modularity. No unnecessary complexity (YAGNI, KISS).

3. **Test Coverage**: Does every new function/method have a test? Were tests written first (TDD)?
   Are tests testing behavior, not implementation?

4. **Type Safety**: Run `pixi run mypy hephaestus/` — are there type errors?

5. **Documentation**: Are public functions documented? Are inline comments explaining *why*?

## Issue Categorization

For each issue found:
- **Critical** (must fix before merge): broken functionality, security issues, data loss risk
- **Important** (should fix): missing tests, type errors, violates SOLID, unclear code
- **Suggestion** (nice to have): style, naming, minor improvements

## Output Format

```
## Strengths
- <what was done well>

## Issues
### Critical
- <issue>: <specific location> — <what to fix>

### Important
- <issue>: <specific location> — <what to fix>

### Suggestions
- <suggestion>: <specific location>

## Assessment
Ready to merge / Fix Critical issues first / Fix Critical + Important issues first
```
"""
)
```

**3. Act on feedback:**
- Fix Critical issues immediately
- Fix Important issues before proceeding
- Note Suggestions for later
- Push back if reviewer is wrong (with technical reasoning)

---

## Part 2: Receiving Code Review Feedback

### The Response Pattern

```
WHEN receiving code review feedback:

1. READ: Complete feedback without reacting
2. UNDERSTAND: Restate requirement in own words (or ask for clarification)
3. VERIFY: Check against codebase reality — does this actually apply?
4. EVALUATE: Is this technically sound for THIS codebase?
5. RESPOND: Technical acknowledgment or reasoned pushback
6. IMPLEMENT: One item at a time, test each
```

### Forbidden Responses

**NEVER:**
- "You're absolutely right!" (performative)
- "Great point!" / "Excellent feedback!"
- "Let me implement that now" (before verification)
- ANY expression of gratitude

**INSTEAD:**
- Restate the technical requirement
- Ask clarifying questions
- Push back with technical reasoning if wrong
- Just start working (actions > words)

### Handling Unclear Feedback

```
IF any item is unclear:
  STOP — do not implement anything yet
  ASK for clarification on unclear items

WHY: Items may be related. Partial understanding = wrong implementation.
```

### Handling External Reviewer Suggestions

```
BEFORE implementing external suggestions:
  1. Is this technically correct for THIS codebase?
  2. Does it break existing functionality?
  3. Is there a reason for the current implementation?
  4. Does the reviewer have full context?

IF suggestion seems wrong: Push back with technical reasoning.
IF can't easily verify: Say "I can't verify this without [X]. Should I [investigate/ask/proceed]?"
```

### YAGNI Check

```
IF reviewer suggests "implementing properly" with new features:
  Search codebase for actual usage

  IF unused: "This isn't called. Remove it (YAGNI)?"
  IF used: Then implement properly
```

### When to Push Back

Push back when:
- Suggestion breaks existing functionality
- Reviewer lacks full context
- Violates YAGNI (unused feature)
- Technically incorrect for this stack
- Conflicts with established architectural decisions

**How:** Use technical reasoning, not defensiveness. Reference working tests/code.

### Acknowledging Correct Feedback

```
✅ "Fixed. [Brief description of what changed]"
✅ "Good catch — [specific issue]. Fixed in [location]."
✅ [Just fix it and show the code]

❌ "You're absolutely right!"
❌ "Great point!"
❌ "Thanks for catching that!"
```

Actions speak. Just fix it.

### Implementation Order for Multi-Item Feedback

1. Clarify anything unclear FIRST
2. Then implement in this order:
   - Critical issues (blocking, security)
   - Simple fixes (imports, naming)
   - Complex fixes (logic, refactoring)
3. Test each fix individually
4. Verify no regressions with `pixi run pytest tests/unit -v`

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
