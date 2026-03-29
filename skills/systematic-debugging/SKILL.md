---
name: systematic-debugging
description: Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes — requires root cause investigation before solutions
argument-hint: <description of the bug or failure>
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob, Agent]
---

# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

**Violating the letter of this process is violating the spirit of debugging.**

## Before Starting

Run `/hephaestus:advise` with the error description to search ProjectMnemosyne for prior debugging sessions on similar errors. Prior learnings may immediately identify the root cause or rule out dead ends.

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose fixes.

## When to Use

Use for ANY technical issue:
- Test failures
- Bugs in production
- Unexpected behavior
- Performance problems
- Build failures
- Integration issues

**Use this ESPECIALLY when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- Previous fix didn't work
- You don't fully understand the issue

## The Four Phases

You MUST complete each phase before proceeding to the next.

### Phase 1: Root Cause Investigation

**BEFORE attempting ANY fix:**

1. **Read Error Messages Carefully**
   - Don't skip past errors or warnings
   - They often contain the exact solution
   - Read stack traces completely
   - Note line numbers, file paths, error codes

2. **Reproduce Consistently**
   - Can you trigger it reliably?
   - What are the exact steps?
   - Does it happen every time?
   - If not reproducible → gather more data, don't guess

3. **Check Recent Changes**
   - What changed that could cause this?
   - `git diff`, recent commits
   - New dependencies, config changes
   - Environmental differences

4. **Gather Evidence in Multi-Component Systems**

   **WHEN system has multiple components:**

   **BEFORE proposing fixes, add diagnostic instrumentation:**
   ```
   For EACH component boundary:
     - Log what data enters component
     - Log what data exits component
     - Verify environment/config propagation
     - Check state at each layer

   Run once to gather evidence showing WHERE it breaks
   THEN analyze evidence to identify failing component
   THEN investigate that specific component
   ```

5. **Trace Data Flow**

   When error is deep in call stack:
   - Where does the bad value originate?
   - What called this with the bad value?
   - Keep tracing up until you find the source
   - Fix at source, not at symptom

### Phase 2: Pattern Analysis

**Find the pattern before fixing:**

1. Find working examples of similar code in the same codebase
2. Read reference implementations completely — don't skim
3. List every difference between working and broken code
4. Identify all dependencies, config, environment assumptions

### Phase 3: Hypothesis and Testing

**Scientific method:**

1. **Form single hypothesis**: "I think X is the root cause because Y"
2. **Test minimally**: Make the SMALLEST possible change to test the hypothesis
3. **One variable at a time**: Don't fix multiple things at once
4. **Verify before continuing**: If it worked → Phase 4. Didn't work → new hypothesis
5. **When stuck**: Say "I don't understand X" — don't pretend to know

### Phase 4: Implementation

**Fix the root cause, not the symptom:**

1. **Create failing test case** using `/hephaestus:test-driven-development` — must exist BEFORE fixing
2. **Implement single fix** addressing the root cause
3. **Verify fix**: Test passes? No other tests broken? Issue actually resolved?

4. **If fix doesn't work:**
   - STOP
   - Count: How many fixes have you tried?
   - If < 3: Return to Phase 1 with new information
   - **If ≥ 3: STOP and question the architecture**

5. **If 3+ fixes failed — Question Architecture:**

   Pattern indicating architectural problem:
   - Each fix reveals new shared state/coupling/problem elsewhere
   - Fixes require massive refactoring to implement
   - Each fix creates new symptoms elsewhere

   STOP and discuss with user before attempting more fixes.
   This is not a failed hypothesis — this is a wrong architecture.

## Red Flags — STOP and Follow Process

- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Add multiple changes, run tests"
- "It's probably X, let me fix that"
- "I don't fully understand but this might work"
- "One more fix attempt" (when already tried 2+)
- Each fix reveals a new problem in a different place

**ALL of these mean: STOP. Return to Phase 1.**

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too. |
| "Emergency, no time for process" | Systematic debugging is FASTER than guess-and-check. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right from the start. |
| "Multiple fixes at once saves time" | Can't isolate what worked. Causes new bugs. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Don't fix again. |

## Hephaestus Tooling

```bash
# Run failing tests with full output
pixi run pytest tests/ -v --tb=long

# Check recent changes
git diff HEAD~3
git log --oneline -10

# Find similar patterns in codebase
grep -r "pattern" hephaestus/ --include="*.py"

# Run type checker for type-related bugs
pixi run mypy hephaestus/
```

## After Resolution

Run `/hephaestus:verification` before claiming the bug is fixed.

Run `/hephaestus:learn` to capture the debugging session in ProjectMnemosyne — especially:
- Root cause category and symptoms
- What diagnostic steps revealed it
- The fix pattern
- Any architectural issues uncovered

This prevents the same debugging session from being repeated by another agent.

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
