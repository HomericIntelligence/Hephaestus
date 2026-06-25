# Release notes (operational)

This directory holds **component-scoped operational notes** that ride along with
a specific PR or PR bundle — one-time migration warnings, behavior-change
call-outs, and "what to run after this lands" guidance for fleet operators.

> These are **not** the project's user-facing release notes. ProjectHephaestus
> generates those from commit history at tag time via
> `gh release create --generate-notes` (see [`../RELEASING.md`](../RELEASING.md)).
> There is intentionally **no `CHANGELOG.md`** in this repo.

## When to add a note here

Add a file when a change has an operational consequence a commit message cannot
convey on its own, for example:

- A behavior tightening that triggers a one-time cost or work burst across the
  fleet on first run (see `plan-reviewer-final-verdict.md`).
- A migration step operators must run manually after the PR merges.
- A correctness change whose blast radius spans repos in the
  HomericIntelligence ecosystem.

If the change needs none of the above, a conventional-commit message is enough —
do **not** add a file here.

## File format

- **Filename**: kebab-case, component-scoped — `<component>-<change-summary>.md`.
- **First line**: a single `# <Title>` H1 summarizing the operational change.
- **Header block**: the existing example (`plan-reviewer-final-verdict.md`)
  opens with `**Affected component:**`, `**Issues:**`, and `**Ships with:**`
  lines. Reuse those keys when they fit; they are a convention by example, not a
  hard schema.

## Required sections

1. `## What changed` — before/after behavior in plain language.
2. `## Operational impact` — what operators must do or expect (cost, migration,
   re-runs), including any copy-pasteable commands.

See [`plan-reviewer-final-verdict.md`](plan-reviewer-final-verdict.md) for a
worked example.
