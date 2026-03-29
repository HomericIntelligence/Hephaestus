---
name: verification
description: Use before claiming work is complete, fixed, or passing — requires running verification commands and confirming output before any success claims; evidence before assertions always
argument-hint: <what you are verifying>
allowed-tools: [Bash, Read]
---

# Verification Before Completion

## Overview

Claiming work is complete without verification is dishonesty, not efficiency.

**Core principle:** Evidence before claims, always.

**Violating the letter of this rule is violating the spirit of this rule.**

**Note on scope:** This skill is for individual developer use. When working inside a myrmidon-swarm, Phase 4 (Package) handles the swarm-level verification gate. Use this skill for your own work at any point, and especially before raising a PR.

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

If you haven't run the verification command in THIS message, you cannot claim it passes.

## The Gate

```
BEFORE claiming any status or expressing satisfaction:

1. IDENTIFY: What command proves this claim?
2. RUN: Execute the FULL command (fresh, complete)
3. READ: Full output, check exit code, count failures
4. VERIFY: Does output confirm the claim?
   - If NO: State actual status with evidence
   - If YES: State claim WITH evidence
5. ONLY THEN: Make the claim

Skip any step = lying, not verifying
```

## Hephaestus Verification Commands

Run ALL of the following before claiming work complete:

```bash
# 1. Unit tests
pixi run pytest tests/unit -v

# 2. Type checking
pixi run mypy hephaestus/

# 3. Linting
pixi run ruff check hephaestus/ tests/

# 4. Formatting check
pixi run ruff format --check hephaestus/ tests/

# 5. Pre-commit hooks on changed files
pre-commit run --files $(git diff --name-only HEAD)
```

All five must pass. "Tests pass" does not mean "type check passes."

## Common Claims and What They Require

| Claim | Requires | Not Sufficient |
|-------|----------|----------------|
| "Tests pass" | `pixi run pytest tests/unit` — 0 failures | Previous run, "should pass" |
| "Types check" | `pixi run mypy hephaestus/` — 0 errors | Code looks right |
| "Linter clean" | `pixi run ruff check` — 0 errors | Partial check |
| "Bug fixed" | Test for original symptom passes | Code changed, assumed fixed |
| "PR ready" | All 5 commands above pass | Tests passing alone |
| "Agent completed" | Check VCS diff shows expected changes | Agent reports "success" |

## Red Flags — STOP

- Using "should", "probably", "seems to"
- Expressing satisfaction before verification ("Great!", "Perfect!", "Done!")
- About to commit/push/PR without verification
- Trusting agent success reports without checking
- Relying on partial verification
- Thinking "just this once"

## Rationalization Prevention

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence ≠ evidence |
| "Tests passed" | Types and lint still need checking |
| "Agent said success" | Verify independently |
| "Partial check is enough" | Partial proves nothing |

## Before Committing

```bash
# Stage specific files (never git add -A)
git add hephaestus/path/to/changed/file.py tests/unit/path/to/test.py

# Verify what's staged
git diff --staged

# Run pre-commit on staged files
pre-commit run --files $(git diff --staged --name-only)
```

## Before Creating PR

1. All 5 verification commands pass
2. `git log --oneline origin/main..HEAD` — commits look correct
3. `git diff origin/main...HEAD` — diff looks right
4. PR title and description accurately describe the change

## The Bottom Line

Run the command. Read the output. THEN claim the result.

No shortcuts for verification.

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
