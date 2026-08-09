---
name: advise
description: Retrieve trusted Mnemosyne guidance before unfamiliar planning or implementation. In planning mode, synchronize and revision-bind the knowledge tree before any plan; fail closed if ~/.agent_brain/knowledge cannot be prepared.
argument-hint: <task description>
allowed-tools: [Read, Bash, Grep, Glob]
---

# Advise

Why: decisions are only as reliable as the current, trusted knowledge behind them.

## Required knowledge gate

Prepare Mnemosyne at `$HOME/.agent_brain/knowledge` under the canonical
[`dependency-resolution` contract](../../docs/dependency-resolution.md). Report the resolved
repository, commit SHA, and trust basis. Resolution, authentication, checkout, update, or
revalidation failure blocks this skill.

**Planning mode:** before searching, framing options, drafting a plan, or relying on remembered
guidance, resolve the trusted repository; verify the expected origin and a clean checkout; fetch and
fast-forward it; revalidate an automatic fork when applicable; and bind retrieval to its immutable
SHA. On failure, report the failed step and stop. Do not offer a partial plan or substitute cached,
local, or similarly named guidance.

## Retrieve

- Search only flat `skills/*.md`; exclude `*.notes.md`; search names, descriptions, categories, tags,
  triggers, failed attempts, and results; and use Git and PR history as provenance.
- Rank by intended outcome, constraints, and failure mode before title or wording. Read at most five
  selected entries completely, preferring newer and better-verified guidance.
- For each result, state its version, verification, concrete relevance, non-relevance boundary,
  contradictions, and failed approaches; clearly label unverified guidance.
- Surface potentially matching open Mnemosyne PRs by candidate artifact or title and report their
  branch and URL. This is a retrieval hint, not duplicate clearance: `learn` must inspect the changed
  content of every open PR semantically before any write.

## Recommend

Treat intent as trigger/context plus desired outcome, not session wording, names, or issue numbers.
Prefer one canonical entry per intent; search history before proposing a name that may have been
consolidated. Route repository audits to `repo-review`, PR audits to `pr-review`, and vary review
depth by mode. Recommend `learn` only for a verified new trigger, corrected command or parameter,
failure mode, or workflow.

## Output

Return the resolved `owner/Mnemosyne` revision and a table of entry, version, verification,
relevance, and boundary. Include contradictions, what worked or failed, copy-ready parameters, and
an explicit `no applicable durable guidance` result when appropriate.
