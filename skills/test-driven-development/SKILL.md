---
name: test-driven-development
description: Use when implementing any feature or bugfix, before writing implementation code — enforces RED-GREEN-REFACTOR cycle
argument-hint: <feature or bugfix description>
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob]
---

# Test-Driven Development (TDD)

## Overview

Write the test first. Watch it fail. Write minimal code to pass.

**Core principle:** If you didn't watch the test fail, you don't know if it tests the right thing.

**Violating the letter of the rules is violating the spirit of the rules.**

## When to Use

**Always:**
- New features
- Bug fixes
- Refactoring
- Behavior changes

**Exceptions (ask your human partner):**
- Throwaway prototypes
- Generated code
- Configuration files

**Integration with myrmidon-swarm:** When invoked from a myrmidon-swarm Phase 2 (Test) agent, apply this cycle to each test sub-task. Each Sonnet specialist writes failing tests before any Haiku engineer writes implementation.

Thinking "skip TDD just this once"? Stop. That's rationalization.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Write code before the test? Delete it. Start over.

**No exceptions:**
- Don't keep it as "reference"
- Don't "adapt" it while writing tests
- Don't look at it
- Delete means delete

Implement fresh from tests. Period.

## Red-Green-Refactor

### RED — Write Failing Test

Write one minimal test showing what should happen.

**Requirements:**
- One behavior per test
- Clear descriptive name
- Test real code (no mocks unless unavoidable)

### Verify RED — Watch It Fail

**MANDATORY. Never skip.**

```bash
# Hephaestus test runner
pixi run pytest tests/ -k "<test_name>" -v
```

Confirm:
- Test fails (not errors)
- Failure message is expected
- Fails because feature is missing (not typos)

**Test passes?** You're testing existing behavior. Fix the test.

**Test errors?** Fix the error, re-run until it fails correctly.

### GREEN — Minimal Code

Write the simplest code to pass the test. No more.

Don't add features, refactor other code, or "improve" beyond what the test demands.

### Verify GREEN — Watch It Pass

**MANDATORY.**

```bash
pixi run pytest tests/ -v
```

Confirm:
- The new test passes
- All other tests still pass
- No errors or warnings

**Test fails?** Fix code, not test.

**Other tests fail?** Fix now before continuing.

### REFACTOR — Clean Up

After green only:
- Remove duplication
- Improve names
- Extract helpers

Keep tests green throughout. Don't add behavior.

### Repeat

Next failing test for next behavior.

## Hephaestus Tooling

```bash
# Run all unit tests
pixi run pytest tests/unit -v

# Run specific test file
pixi run pytest tests/unit/utils/test_general_utils.py -v

# Run with coverage
pixi run pytest tests/unit --cov=hephaestus --cov-report=html

# Type check
pixi run mypy hephaestus/

# Lint
pixi run ruff check hephaestus/ tests/
```

## Good Tests

| Quality | Good | Bad |
|---------|------|-----|
| **Minimal** | One thing. "and" in name? Split it. | `test('validates email and domain and whitespace')` |
| **Clear** | Name describes behavior | `test_1` |
| **Shows intent** | Demonstrates desired API | Obscures what code should do |

## Common Rationalizations — All Wrong

| Excuse | Reality |
|--------|---------|
| "Too simple to test" | Simple code breaks. Test takes 30 seconds. |
| "I'll test after" | Tests passing immediately prove nothing. |
| "Tests after achieve same goals" | Tests-after = "what does this do?" Tests-first = "what should this do?" |
| "Already manually tested" | Ad-hoc ≠ systematic. No record, can't re-run. |
| "Deleting X hours is wasteful" | Sunk cost fallacy. Keeping unverified code is technical debt. |
| "Keep as reference, write tests first" | You'll adapt it. That's testing after. Delete means delete. |
| "Need to explore first" | Fine. Throw away exploration, start with TDD. |

## Red Flags — STOP and Start Over

- Code written before test
- Test passes immediately without implementation
- Can't explain why test failed
- "Tests added later"
- Rationalizing "just this once"

**All of these mean: Delete code. Start over with TDD.**

## Verification Checklist

Before marking work complete:

- [ ] Every new function/method has a test
- [ ] Watched each test fail before implementing
- [ ] Each test failed for the expected reason
- [ ] Wrote minimal code to pass each test
- [ ] All tests pass (`pixi run pytest tests/unit -v`)
- [ ] Type check passes (`pixi run mypy hephaestus/`)
- [ ] Linter clean (`pixi run ruff check hephaestus/ tests/`)

Can't check all boxes? You skipped TDD. Start over.

## After Completion

Run `/hephaestus:verification` before claiming work complete.
Consider running `/hephaestus:learn` to capture novel testing patterns in ProjectMnemosyne.

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
