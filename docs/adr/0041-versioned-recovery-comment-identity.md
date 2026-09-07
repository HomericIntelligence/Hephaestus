# ADR-0041: Versioned recovery-comment identity

- Status: Accepted
- Date: 2026-09-06
- Supersedes: ADR-0031 in part
- Tracks: #2960

## Context

Requirements recovery stores its evidence in one actor-owned issue comment.
The comment marker contains a version and digest values. A marker-family
prefix identifies the recovery role, but it does not identify one complete
marker line.

The shared comment API uses exact first-line markers for the plan and review
roles. Recovery must keep that strict rule and also accept valid recovery
comments from versions 1, 2, and 3. A complete bounded comment journal is
needed to detect foreign, malformed, repeated, and duplicate claims before a
write.

## Decision

The recovery marker family is the namespace identified by
`RECOVERY_PROVENANCE_PREFIX`. A recovery claim is one top-level Markdown line
that starts with this prefix. A claim is valid only when the complete marker
and its rendered body pass the recovery provenance checks.

The recovery identity selector reads the complete bounded issue-comment
journal. It returns the sole valid actor-owned recovery comment and its parsed
provenance. It fails closed when a claim has leading whitespace, is foreign,
is malformed, is repeated in one comment, or has a duplicate actor-owned
comment. It returns no comment when the journal has no recovery claim.

Both public recovery upsert paths use this selector before a mutation and
after each create or update. A create is allowed only when no recovery
comment exists. An update changes the selected comment in place. The
GitHub database ID must stay unchanged, and the complete journal must show the
exact requested body after the mutation. No recovery duplicate is deleted by
the upsert or timeline compaction paths.

Valid version 1 and version 2 comments remain identity candidates. Version 3
is the current authority for contextual recovery. A version migration keeps
the same GitHub database ID. Generic plan and review comments keep their
exact-marker identity contract and are not changed by this decision.

Selector and post-write identity failures use the existing planning retry
budget. They do not start an independent retry loop. A retry uses the durable
plan publication receipt and does not publish the same plan again.

All Hephaestus-owned English technical text for this decision follows
ASD-STE100 Simplified Technical English, Issue 9, dated January 15, 2025.
Before release, the author and reviewer record a controlled-dictionary check
in the pull-request evidence. The standard is not copied into this
repository.

## Alternatives considered

- Treat the recovery prefix as an exact marker. Rejected because rendered
  recovery markers contain version and digest fields after the prefix.
- Select the newest recovery comment. Rejected because a duplicate or foreign
  comment can hide the actor-owned record and cause an unsafe mutation.
- Delete duplicate recovery comments during compaction. Rejected because an
  ambiguous identity needs manual recovery before any destructive action.
- Add a recovery-specific retry loop. Rejected because independent retries
  can exceed the stage budget and republish a plan.

## Consequences

- Recovery comments from versions 1, 2, and 3 can migrate in place.
- Foreign, malformed, repeated, and duplicate claims stop before mutation.
- Comment IDs provide stable identity across recovery version updates.
- The planning stage keeps its existing bounded retry and receipt model.
- The plan and review exact-marker contract remains unchanged.
