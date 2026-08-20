# ADR-0030: Autonomous issue requirements recovery precedes planning

- Status: Accepted
- Date: 2026-08-20
- Tracks: #2795, #2659

## Context

The issue body is the planner's requirements input, but historical automation
runs sometimes copied a canonical plan, plan review, or plan-history artifact
into that body. Treating that derived text as original requirements causes
successive plans to amplify implementation detail and diverge from the user's
intent. The same restart path also routed any issue with an open pull request
past planning, even when no reviewed plan authorized that implementation.

The automation loop is intended to operate without an interactive source
selection or approval step. Recovery therefore needs durable, restart-safe
evidence and an independent quality gate, while preserving GitHub as the
authoritative journal.

## Decision

The existing planning stage owns requirements recovery as explicit substates;
no additional pipeline queue is introduced. At entry it recognizes only exact
canonical automation markers on the first non-whitespace body line. A planner
reconstructs requirements from nonce-fenced issue, GitHub, and repository
evidence, and a separately configured reviewer independently returns a
structured verdict. Tracker and obsolete dispositions require agreement from
both decisions before automation applies `state:skip`.

A recovered requirements artifact is an actor-owned comment carrying a hidden
versioned provenance marker with SHA-256 digests of its source, evidence
binding, and reconstructed requirements. The source issue body is never
rewritten: GitHub exposes no server-enforced compare-and-swap for issue edits,
so a read-then-write body update could overwrite a concurrent human edit.
Restart reads only an authenticated actor-owned artifact whose source digest
matches the live body; a changed body therefore fails closed into fresh
recovery. Successful comment publication starts a fresh plan and plan-review
epoch rather than reusing old comments or sessions. The replacement canonical
plan carries the matching recovery-source digest as hidden epoch metadata, so
a restart discards only a plan that predates the recovery artifact while a
pending or rejected post-recovery plan resumes normally. Independently
confirmed obsolete issues likewise upsert exactly one actor-owned explanation
before receiving `state:skip`.

ADR-0031 defines how these recovery roles coexist with the canonical plan and
review roles, including their ownership and compaction rules.

An open pull request is not proof of a valid plan. Issues without an exclusive
`state:plan-go` state enter planning even when a PR already exists. `--force`
propagates into the stage configuration and explicitly starts a fresh planning
epoch.

Requirements-recovery outcomes and ordinary plan-review verdicts remain binary:
`state:plan-go` or `state:plan-no-go`. Exceptional plan-review session loss or
no-progress conditions may retain the pre-existing `state:plan-blocked` latch.
Bounded NOGO exhaustion, unavailable
evidence, and repeated provider or GitHub publication rejection end the issue
for the current run with `state:plan-no-go` intact. Recovery never applies the
legacy `state:plan-blocked` latch. Prompt guidance asks for concise plans and
minimal repeated snippets, while the existing journal validator continues to
reject raw diffs. No custom body-size, line-count, or code-block limit is
introduced.

## Alternatives considered

- Require an operator to choose or rewrite the source requirements. Rejected
  because it breaks the loop's autonomous operating contract.
- Add a ninth recovery queue. Rejected because recovery is part of planning
  admission and does not need an independently schedulable lifecycle.
- Trust a single reconstruction model. Rejected because destructive body and
  skip mutations need an independent semantic check.
- Rewrite the issue body after a best-effort digest check. Rejected because
  GitHub does not expose atomic compare-and-swap and a concurrent human edit
  must never be overwritten.
- Introduce a third ordinary plan state such as `plan-blocked`. Rejected;
  ordinary review remains the binary GO/NOGO contract.

## Consequences

Contaminated issues can recover without human intervention, and every
actor-owned recovery artifact is attributable and replay-safe. Planning may spend two additional
read-only agent calls when recovery or semantic disposition review is needed.
Open-PR issues can return to planning, so implementations that predate an
approved plan may be redone. Operators receive bounded summary counters for
recovery and skip actions rather than repeated warning noise. Obsolete reasons
retain one canonical actor-owned explanation alongside their label.
