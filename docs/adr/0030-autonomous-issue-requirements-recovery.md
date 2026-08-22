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
binding, and reconstructed requirements. Version 3 also binds the issue title
and exact repository revision; restart recomputes the complete repository,
issue, title, body, and revision identity before accepting it. The source issue body is never
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

An Athena-finalized planning epoch is the terminal exception to recovery. The
planning stage accepts exactly one top-level
`<!-- athena:finalize-plan R=... P=... V=... F=... -->` marker only when `F`
matches SHA-256 of the complete UTF-8 body with that value replaced by the
literal `<F>`. `P` and `V` must each encode one exact comment ID plus its
canonical content digest, and the authenticated automation actor must own the
latest issue-body edit. A checksum alone is integrity evidence, not
authorization. That verified body already contains a GO-reviewed plan, so the
stage does not invoke recovery or reopen it under `--force`; it atomically
normalizes the issue to exclusive `state:plan-go` and records the independent
`athena:finalized-plan` evidence label for observability. The evidence label is
metadata, never a third plan state or implementation gate. On every restart,
seeding routes an intact finalized marker through planning's no-model editor
authentication fast path. A successful check advances without planner or
plan-review calls. If a later body rewrite removes or replaces the marker, the
stage clears stale evidence and stale GO and enters a fresh planning or
recovery epoch; malformed, duplicated, mismatched, or foreign-edited markers
enter autonomous requirements recovery.

`state:plan-blocked` remains an operator-owned latch for ordinary planning.
An authenticated Athena finalized body is the narrow terminal exception: it
proves that the external planning decision completed, so Hephaestus atomically
replaces a stale blocked latch with exclusive `state:plan-go` plus finalized
evidence. Foreign or invalid finalization leaves the blocked latch untouched.

Tracker labels and title patterns are candidates, not skip authority.
Repository discovery routes them through independent semantic planning review;
only a confirmed tracker or obsolete disposition may apply `state:skip`.

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
Verified Athena-finalized issues spend no additional planner or plan-review
model calls. Hephaestus records non-state evidence of that observation so later
body drift deterministically invalidates the shortcut across process restarts.
Open-PR issues can return to planning, so implementations that predate an
approved plan may be redone. Operators receive bounded summary counters for
recovery and skip actions rather than repeated warning noise. Obsolete reasons
retain one canonical actor-owned explanation alongside their label.
